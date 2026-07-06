"""CLI runner for the ManyReach reply bot.

Usage examples (PowerShell, from project root):

    # Dry-run on the 5 most recent replies — no writes to ManyReach
    python scripts/run_bot.py --limit 5

    # Live run with auto-send for safe intents (after dry-run validation)
    python scripts/run_bot.py --no-dry-run --limit 20

    # Process only one specific campaign
    python scripts/run_bot.py --campaign 11572 --limit 10

Outputs:
- Console: human-readable summary of each reply, classification, draft, planned actions.
- logs/run_<timestamp>.jsonl: one JSON line per reply for audit + idempotence.

Idempotence: a reply already processed (its messageId appears in logs/processed_messages.txt)
is skipped on subsequent runs.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import textwrap
import time
import traceback
from dataclasses import replace as _dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `src.*` importable when running this script directly.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.actions import (  # noqa: E402
    ALWAYS_SILENT,
    AUTOSEND_ELIGIBLE,
    execute_plan,
    plan_actions,
    plan_mailinblack_actions,
)
from src.classifier import Classifier, _strip_html, _trim_quoted_history  # noqa: E402
from src.drafter import Drafter  # noqa: E402
from src.manyreach import ManyReachClient, is_bounce_or_auto, is_mailinblack  # noqa: E402


def load_settings(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_style_guide(training_path: Path) -> str:
    """Load training_examples.md as a single string passed to the drafter.

    The drafter parses voice, entity signatures, backup links, and training
    pairs from this content directly — no Python-side section extraction. This
    means the user can edit the doc freely without breaking anything.
    """
    if not training_path.exists():
        return ""
    return training_path.read_text(encoding="utf-8")


def _short(s: str, n: int = 180) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def load_processed_ids(processed_file: Path) -> set[str]:
    if not processed_file.exists():
        return set()
    return {
        line.strip()
        for line in processed_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def append_processed_id(processed_file: Path, message_id: str) -> None:
    processed_file.parent.mkdir(parents=True, exist_ok=True)
    with processed_file.open("a", encoding="utf-8") as f:
        f.write(f"{message_id}\n")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Process ManyReach replies: classify, draft, tag, optionally send.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__ or ""),
    )
    parser.add_argument("--limit", type=int, default=None, help="Max replies to process")
    parser.add_argument("--campaign", type=int, default=None, help="Filter to one campaign ID")
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually execute writes to ManyReach (default: dry-run only)",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=PROJECT_ROOT / "config" / "settings.yaml",
    )
    parser.add_argument(
        "--training",
        type=Path,
        default=PROJECT_ROOT / "training_examples.md",
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Ignore the processed-messages list and re-classify everything",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=14,
        help="Only consider replies received in the last N days (default: 14)",
    )
    parser.add_argument(
        "--important-only",
        action="store_true",
        help=(
            "Only process replies ManyReach tagged as important "
            "(Interested, MeetingBooked, MaybeLater, Neutral, CollegueReplied) — "
            "skips all the bounce/auto-reply/MailInBlack noise."
        ),
    )
    parser.add_argument(
        "--only-email",
        type=str,
        default=None,
        help=(
            "TEST CONTRÔLÉ : ne traite QUE les replies de cette adresse, et "
            "force l'envoi réel de la réponse (à utiliser avec --no-dry-run). "
            "Sert à tester le bot sur UN seul prospect choisi."
        ),
    )
    parser.add_argument(
        "--ignore-window",
        action="store_true",
        help=(
            "Ignore allowed_hours/allowed_weekdays — envoi immédiat quelle que "
            "soit l'heure. À utiliser pour le bouton 'Lancer maintenant' du "
            "dashboard (Rudy force un passage humain-déclenché)."
        ),
    )
    args = parser.parse_args()

    settings = load_settings(args.settings)

    # État cloud (Vercel KV) — no-op si pas de KV configurée (local).
    try:
        from src import kvstore
        if kvstore.kv_available():
            if not kvstore.is_enabled():
                print("Bot EN PAUSE (bouton dashboard) — rien à faire.")
                return 0
            # Réglages édités via le dashboard : overlay sur settings.yaml
            overrides = kvstore.get_settings_overrides()
            for k, v in overrides.items():
                if isinstance(v, dict) and isinstance(settings.get(k), dict):
                    settings[k].update(v)
                else:
                    settings[k] = v
    except Exception as _e:
        print(f"(KV non disponible : {_e})")
        kvstore = None  # type: ignore

    dry_run = not args.no_dry_run
    limit = args.limit if args.limit is not None else settings.get("limit_per_run")
    campaign_id = args.campaign or settings.get("campaign_filter")
    if isinstance(campaign_id, list):
        campaign_id = campaign_id[0] if campaign_id else None

    min_conf = float(settings.get("min_autosend_confidence", 0.92))
    silent_on_not_interested = bool(settings.get("silent_on_not_interested", True))

    style_guide = load_style_guide(args.training)

    # LOG_DIR env permet d'écrire ailleurs (ex. /tmp sur Vercel, FS read-only).
    log_dir_env = os.environ.get("LOG_DIR")
    logs_dir = Path(log_dir_env) if log_dir_env else PROJECT_ROOT / settings.get("logs", {}).get("dir", "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_file = logs_dir / f"run_{run_ts}.jsonl"
    processed_file = logs_dir / "processed_messages.txt"
    # Sur Vercel /tmp est éphémère ET partagé entre invocations d'une instance
    # warm → le cache pollue à l'infini sans persistance fiable. On le désactive
    # complètement : la SEULE source d'idempotence sur Vercel = la thread
    # ManyReach (Sent/SentManual après le Reply). Plus de pollution.
    on_vercel_tmp = str(processed_file).startswith("/tmp")
    if args.reprocess or on_vercel_tmp:
        processed_ids = set()
    else:
        processed_ids = load_processed_ids(processed_file)

    since = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    since = since.fromordinal(since.toordinal() - args.since_days).replace(tzinfo=timezone.utc)

    from src.manyreach import ManyReachClient as _MRC
    confirmed_statuses = _MRC.IMPORTANT_STATUSES if args.important_only else None

    print(f"\n=== ManyReach Reply Bot — {'DRY-RUN' if dry_run else 'LIVE'} mode ===")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Logs: {log_file}")
    print(f"Since: {since.isoformat()}  Limit: {limit}  Campaign filter: {campaign_id}")
    if confirmed_statuses:
        print(f"Filtre: IMPORTANT uniquement ({', '.join(confirmed_statuses)})")
    print(f"Already-processed message IDs: {len(processed_ids)}")
    print()

    _models = settings.get("models", {}) or {}
    classifier = Classifier(model=_models["classifier"]) if _models.get("classifier") else Classifier()
    _drafter_kwargs = {}
    if _models.get("drafter"):
        _drafter_kwargs["model"] = _models["drafter"]
    if _models.get("drafter_simple"):
        _drafter_kwargs["simple_model"] = _models["drafter_simple"]
    drafter = Drafter(**_drafter_kwargs)
    tag_cache: dict[str, int] = {}

    # --- Anti-grillage / timing guardrails ---
    send_cfg = settings.get("sending", {}) or {}
    backlog_cutoff = None
    _since = send_cfg.get("process_replies_since")
    if _since:
        try:
            backlog_cutoff = datetime.fromisoformat(_since)
        except ValueError:
            print(f"  !! process_replies_since invalide ({_since!r}), ignoré")
    sends_done = 0
    max_sends = int(send_cfg.get("max_sends_per_run", 25))

    def send_window_open(when: datetime) -> bool:
        # --ignore-window force l'ouverture (déclenchement manuel par Rudy).
        if args.ignore_window:
            return True
        hours = send_cfg.get("allowed_hours", [9, 19])
        days = send_cfg.get("allowed_weekdays", [0, 1, 2, 3, 4])
        # IMPORTANT : on raisonne en heure de PARIS, pas en heure serveur. Sur
        # Vercel le serveur tourne en UTC → .astimezone() sans tz donnerait 9-19
        # UTC = 11h-21h Paris (décalage de 2h). On force donc le fuseau configuré.
        tz_name = (settings.get("calendar", {}) or {}).get("timezone", "Europe/Paris")
        try:
            from zoneinfo import ZoneInfo
            local = when.astimezone(ZoneInfo(tz_name))
        except Exception:
            local = when.astimezone()
        return local.weekday() in days and hours[0] <= local.hour < hours[1]

    def reply_old_enough(reply, when: datetime) -> bool:
        """Don't reply instantly — a human lets a message age a bit.

        CLOCK SKEW NOTE : l'API ManyReach renvoie parfois des timestamps en
        avance de plusieurs heures par rapport au vrai UTC (constaté +6/+7h).
        Si (when - reply.created_at) est NÉGATIF, c'est ce cas ; le reply
        est en réalité déjà ancien côté monde réel. On le considère donc
        comme suffisamment vieux pour être traité — sinon TOUS les replies
        récents seraient bloqués indéfiniment ('trop récent' à chaque cron),
        ce qui paralyse le bot.
        """
        min_age = int(send_cfg.get("min_reply_age_minutes", 0))
        jitter = int(send_cfg.get("max_reply_age_jitter_minutes", 0))
        if min_age <= 0 and jitter <= 0:
            return True
        delta = (when - reply.created_at).total_seconds()
        if delta < 0:
            # Clock skew côté MR → on bypass le check d'âge minimum.
            return True
        # Deterministic per-message jitter so the threshold is stable across runs
        extra = (abs(hash(reply.message_id)) % (jitter + 1)) if jitter > 0 else 0
        return delta >= (min_age + extra) * 60

    processed_count = 0          # total iterations (pour stats)
    heavy_count = 0              # iterations "lourdes" (draft+send) — c'est ELLES qui consomment le quota
    skipped_count = 0
    error_count = 0
    mailinblack_pending: list[dict] = []

    # BUDGET DE TEMPS strict. Cron-job.org coupe à 30s, Vercel à 60s.
    # Défaut 22s pour les passages cron-job.org (laisse 8s pour le finalize
    # KV + set_last_run). Le bouton "Lancer maintenant" passe 50s via env
    # var (il a 60s côté Vercel).
    run_budget_s = float(os.environ.get("RUN_BUDGET_SECONDS", "22"))
    run_start_ts = time.time()
    def _time_left() -> float:
        return run_budget_s - (time.time() - run_start_ts)

    def _mark_done(message_id: str) -> None:
        """Marque un reply comme TERMINÉ : fichier local (dev) + KV persistant
        (prod Vercel, survit aux cold starts → plus de famine, plus de re-fetch).
        Inerte en dry-run (zéro effet de bord)."""
        if dry_run:
            return
        append_processed_id(processed_file, message_id)
        if kvstore is not None and kvstore.kv_available():
            try:
                kvstore.mark_kv_processed(message_id)
            except Exception:  # noqa: BLE001
                pass

    if args.only_email:
        print(f">>> MODE TEST CONTRÔLÉ : uniquement {args.only_email} (envoi forcé si --no-dry-run)")
        confirmed_statuses = None  # ignore status filter, target the email directly

    with ManyReachClient() as mr, log_file.open("w", encoding="utf-8") as logf:
        # FIFO ANTI-FAMINE : on matérialise les replies et on les trie du PLUS
        # ANCIEN au plus récent. Ainsi un reply arrivé hier soir (différé hors
        # fenêtre) passe AVANT les frais du matin → plus jamais de starvation.
        # (Le tri sur ~quelques centaines d'items de metadata est instantané.)
        _replies = []
        try:
            for _rep in mr.list_replies(
                campaign_id=campaign_id,
                since=since,
                confirmed_statuses=confirmed_statuses,
                email_from=args.only_email,
            ):
                _replies.append(_rep)
        except Exception as _e:  # noqa: BLE001
            # 429 ou autre pendant la pagination → on garde ce qu'on a déjà
            # récupéré et on traite quand même (le reste passera au prochain run).
            print(f"  !! Listing interrompu ({_e}) — on traite les {len(_replies)} déjà récupérés")
        _replies.sort(key=lambda r: r.created_at)  # oldest first
        # PRÉ-FILTRE GROUPÉ : un seul MGET KV pour écarter tous les déjà-traités,
        # au lieu d'un GET par reply dans la boucle (~200 round-trips = ~20s).
        if (
            not args.reprocess
            and not args.only_email
            and kvstore is not None
            and kvstore.kv_available()
            and _replies
        ):
            _done = kvstore.filter_processed([r.message_id for r in _replies])
            if _done:
                _replies = [r for r in _replies if r.message_id not in _done]
            print(f"  {len(_done)} déjà traités (skip groupé), {len(_replies)} à examiner")
        print(f"Replies en file (fenêtre {args.since_days}j) : {len(_replies)}")
        for reply in _replies:
            # Le quota du cron protège du timeout Vercel — il ne porte QUE sur les
            # itérations "lourdes" (draft+send Sonnet). Les itérations "cheap"
            # (defer, silent, déjà-handled) sont quasi-gratuites en temps et tokens.
            if limit and heavy_count >= limit:
                break
            # Budget de temps : un refus simple draft sur Haiku (~4-5s), un
            # prospect chaud sur Sonnet (~10-12s). On garde 9s de marge pour
            # démarrer encore au moins une itération Haiku. Garantit que
            # set_last_run + le finalize KV tournent toujours avant le timeout.
            if _time_left() < 9.0:
                print(f"  >> BUDGET TEMPS ({run_budget_s}s) — arrêt avant nouvelle itération")
                break
            if reply.message_id in processed_ids:
                skipped_count += 1
                continue
            # (Le pré-filtre groupé MGET ci-dessus a déjà écarté les déjà-traités
            # → plus besoin d'un GET KV par reply ici.)

            print("─" * 80)
            print(f"REPLY  from={reply.from_email}  campaign={reply.campaign_id}")
            print(f"  Subject: {_short(reply.subject)}")
            print(f"  When:    {reply.created_at.isoformat()}")
            print(f"  Snippet: {_short(reply.body, 220)}")

            log_entry: dict = {
                "messageId": reply.message_id,
                "from": reply.from_email,
                "subject": reply.subject,
                "createdAt": reply.created_at.isoformat(),
                "campaignId": reply.campaign_id,
            }

            now_utc = datetime.now(timezone.utc)

            # NOTE : l'ancien filtre "reply sans campaignId → spam" a été RETIRÉ
            # (2026-06-15). Il était DANGEREUX : si le listing ManyReach renvoie
            # un campaignId vide pour un reply LÉGITIME (ça arrive), le reply
            # était tué ET marqué traité définitivement → négatifs jamais
            # répondus, alertes jamais remontées. Le spam externe pur (ex.
            # michael.brandl, scrapé sur des variantes du nom de Rudy) est de
            # toute façon attrapé plus bas par le check ORPHAN (prospect
            # introuvable dans ManyReach) → skip silencieux sans risque.

            # PROTECTION BACKLOG : ignorer les replies antérieurs à la mise en route
            if backlog_cutoff and reply.created_at < backlog_cutoff and not args.only_email:
                print("  >> Antérieur à la date de mise en route — ignoré (backlog)")
                skipped_count += 1
                continue

            # === PRÉ-SKIP CHEAP : reply déjà 'gardé' la nuit ET fenêtre toujours
            # fermée → on saute AVANT même de payer find_prospect + classifier.
            # Économie majeure de tokens Haiku quand le backlog nocturne s'accumule.
            # Quand la fenêtre s'ouvre, send_window_open=True → cette branche est
            # ignorée → traitement complet redémarre normalement.
            if (
                not args.only_email
                and not args.reprocess
                and not send_window_open(now_utc)
                and kvstore is not None
                and kvstore.kv_available()
                and kvstore.peek_held_seen(reply.message_id)
            ):
                print("  >> Reply déjà gardé, fenêtre toujours fermée → skip (0 token)")
                processed_count += 1
                continue

            # ANTI-RÉPONSE-INSTANTANÉE : laisser le reply "vieillir" un peu
            if not args.only_email and not reply_old_enough(reply, now_utc):
                print("  >> Reply trop récent — traité à un prochain run (timing humain)")
                skipped_count += 1
                continue

            try:
                # Pre-filter 1: MailInBlack (must come BEFORE bounce check)
                if is_mailinblack(reply):
                    print("  >> Pre-filter: MailInBlack challenge — manual click needed")
                    prospect = None
                    try:
                        prospect = mr.find_prospect_by_email(reply.from_email)
                    except Exception:
                        pass  # MailInBlack 'from' is the gateway, not the prospect
                    plan = plan_mailinblack_actions(reply=reply, prospect=prospect)
                    results = execute_plan(plan, reply, mr, dry_run=dry_run, tag_cache=tag_cache)
                    for line in results:
                        print(f"    {line}")
                    mailinblack_pending.append({
                        "challenge_from": reply.from_email,
                        "destination_mailbox": reply.to_email,
                        "subject": reply.subject,
                        "createdAt": reply.created_at.isoformat(),
                    })
                    log_entry["intent"] = "mailinblack_pending"
                    log_entry["confidence"] = 1.0
                    log_entry["actions"] = results
                    log_entry["mailinblack_destination"] = reply.to_email
                    logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    if not dry_run:
                        _mark_done(reply.message_id)
                    processed_count += 1
                    continue

                # Pre-filter 2: generic bounces and auto-replies
                if is_bounce_or_auto(reply):
                    print("  >> Pre-filter: bounce or auto-reply — skipping classification")
                    log_entry["intent"] = "bounce_or_auto"
                    log_entry["confidence"] = 1.0
                    log_entry["actions"] = ["skip (pre-filtered bounce)"]
                    logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    if not dry_run:
                        _mark_done(reply.message_id)
                    processed_count += 1
                    continue

                # Try to find the prospect & original outreach for context
                prospect = None
                original_outreach = None
                thread = []
                try:
                    prospect = mr.find_prospect_by_email(reply.from_email)
                except Exception as e:
                    print(f"  !! find_prospect_by_email failed: {e}")

                # STATUT TERMINAL : si le prospect est déjà Unsub ou Hostile, on
                # ne retraite pas (le bot l'a déjà géré dans un run précédent OU
                # Rudy l'a fait à la main). Évite les doublons silencieux à
                # chaque cron — important parce que /tmp processed_ids est
                # éphémère sur Vercel.
                if (
                    prospect is not None
                    and not args.only_email
                    and not args.reprocess
                    and prospect.sending_status in ("Unsub", "Hostile")
                ):
                    print(f"  >> Statut terminal ({prospect.sending_status}) — skip silencieux")
                    if not dry_run:
                        _mark_done(reply.message_id)
                    processed_count += 1
                    continue

                # ORPHAN REPLY (pas de prospect dans la base ManyReach) : skip
                # systématique. Rudy ne peut rien faire de ces alertes (le lien
                # Unibox ne trouvera pas le prospect → aucune fiche à ouvrir).
                # On marque traité et on sort, sans classifier ni alerter.
                if (
                    prospect is None
                    and not args.only_email
                    and not args.reprocess
                ):
                    print(f"  >> Orphan sender {reply.from_email} (pas de prospect MR) — skip silencieux")
                    if kvstore is not None and kvstore.kv_available():
                        try:
                            kvstore.mark_orphan_sent(reply.from_email)
                        except Exception:  # noqa: BLE001
                            pass
                        kvstore.log_action({
                            "at": now_utc.isoformat(),
                            "from": reply.from_email,
                            "subject": reply.subject,
                            "intent": "orphan_no_prospect",
                            "status": "silencieux (pas de prospect MR)",
                            "reply": _trim_quoted_history(_strip_html(reply.body))[:300],
                            "response": "",
                        })
                    if not dry_run:
                        _mark_done(reply.message_id)
                    processed_count += 1
                    continue

                previous_sent_text = ""
                if prospect is not None:
                    try:
                        thread = mr.get_prospect_thread(prospect.prospect_id)
                        sent_msgs = [m for m in thread if m.type in ("Sent", "SentManual")]
                        if sent_msgs:
                            original_outreach = sent_msgs[0]
                            # Le DERNIER message qu'on a envoyé avant ce reply
                            # (peut contenir les créneaux proposés → résolution "ok mardi").
                            prior = [m for m in sent_msgs if m.created_at <= reply.created_at]
                            last_sent = (prior or sent_msgs)[-1]
                            previous_sent_text = _trim_quoted_history(_strip_html(last_sent.body), 1500)
                    except Exception as e:
                        print(f"  !! get_prospect_thread failed: {e}")

                # CAP ANTI-PING-PONG : si le bot a déjà répondu N fois sur ce thread
                # (en réponse à des replies du prospect), on se TAIT sur le nouveau
                # reply. Empêche le ping-pong type pharmacie-bonnatrait (7 replies du
                # prospect "mauvaise personne" → 7 réponses du bot, inutile).
                # Compte les Sent/SentManual qui suivent un Reply dans l'historique
                # avant le reply actuel.
                PROSPECT_REPLY_CAP = int(os.environ.get("PROSPECT_REPLY_CAP", "2"))
                if (
                    not args.only_email
                    and not args.reprocess
                    and thread
                    and len(thread) >= 3  # au moins 1 cold + 1 reply + 1 réponse bot
                ):
                    ordered = sorted(thread, key=lambda m: m.created_at)
                    # IMPORTANT : ManyReach renvoie chaque envoi du bot 2 FOIS dans
                    # le thread (une entrée "Sent" + une entrée "SentManual" avec le
                    # MÊME msgId). On dédoublonne par msgId pour ne compter chaque
                    # réponse du bot qu'UNE fois (sinon le cap=2 saute après 1 envoi).
                    bot_msg_ids: set[str] = set()
                    seen_first_reply = False
                    for _m in ordered:
                        if _m.created_at >= reply.created_at:
                            break
                        if _m.type == "Reply":
                            seen_first_reply = True
                        elif _m.type in ("Sent", "SentManual") and seen_first_reply:
                            bot_msg_ids.add(_m.message_id)
                    bot_replies_so_far = len(bot_msg_ids)
                    if bot_replies_so_far >= PROSPECT_REPLY_CAP:
                        print(f"  >> CAP ATTEINT ({bot_replies_so_far} réponses bot déjà sur ce thread) — silent")
                        log_entry["intent"] = "ping_pong_cap"
                        log_entry["actions"] = [f"silent (cap {PROSPECT_REPLY_CAP} réponses atteint)"]
                        logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                        if kvstore and kvstore.kv_available():
                            kvstore.log_action({
                                "at": now_utc.isoformat(),
                                "from": reply.from_email,
                                "subject": reply.subject,
                                "intent": "ack_only",
                                "status": f"silencieux (cap {PROSPECT_REPLY_CAP} réponses atteint sur ce thread)",
                                "reply": _trim_quoted_history(_strip_html(reply.body))[:500],
                                "response": "(pas de réponse — anti ping-pong)",
                            })
                        _mark_done(reply.message_id)
                        skipped_count += 1
                        continue

                # IDEMPOTENCE : si une réponse (Sent/SentManual) existe déjà APRÈS ce
                # reply, c'est que le thread a déjà été traité — par le bot OU par Rudy
                # à la main. On ne retraite pas (zéro doublon + respect du travail manuel).
                # SEUL --reprocess bypasse (debug). --only-email respecte désormais
                # l'idempotence → le bouton "Pour cet email" ne double-envoie plus,
                # mais retry si l'envoi précédent avait échoué (pas de Sent dans le thread).
                if not args.reprocess and thread:
                    already_handled = any(
                        m.type in ("Sent", "SentManual") and m.created_at > reply.created_at
                        for m in thread
                    )
                    if already_handled:
                        print("  >> DÉJÀ TRAITÉ (réponse postérieure déjà dans le thread) — skip")
                        log_entry["intent"] = "already_handled"
                        log_entry["actions"] = ["skip (déjà répondu dans le thread)"]
                        logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                        # Marqueur KV → on ne refera plus le get_thread coûteux pour
                        # ce reply (cas répondu manuellement par Rudy notamment).
                        _mark_done(reply.message_id)
                        skipped_count += 1
                        continue

                # Classify
                classification = classifier.classify(
                    reply,
                    original_outreach=original_outreach,
                    previous_message=previous_sent_text,
                )
                print(
                    f"  CLASSIFIED  intent={classification.intent}  "
                    f"conf={classification.confidence:.2f}  "
                    f"lang={classification.language}"
                )

                # GARDE-FOU POST-CLASSIFIER : acquiescement court → interested_warm.
                # Cas vu sur jean@seiche-impression.fr : Claude avait classé "OUI
                # Merci, Jean." en AUTOSEND → bot a envoyé une réponse neutre à
                # un lead chaud qui avait dit OUI pour recevoir la plaquette.
                # Heuristique : si reply court (< 150 chars hors signature) ET
                # commence par un acquiescement explicite, on force interested_warm
                # (= alerte Rudy, pas de réponse auto qui pourrait être maladroite).
                _clean_body = _trim_quoted_history(_strip_html(reply.body)).strip()
                # On enlève les signatures basiques pour évaluer la longueur réelle
                _body_first_line = _clean_body.split("\n")[0].strip()
                _body_short = _clean_body[:200].lower()
                _body_full_low = _clean_body.lower()
                _affirmatives = (
                    "oui", "yes", "ok", "okay", "d'accord", "daccord",
                    "volontiers", "avec plaisir", "bien sûr", "bien sur",
                    "carrément", "carrement", "absolument",
                )
                _starts_affirmative = any(
                    _body_short.lstrip(" \t.!?-•*>\"'").startswith(a)
                    or _body_first_line.lower().strip(" .!?-:") == a
                    for a in _affirmatives
                )
                if (
                    _starts_affirmative
                    and len(_clean_body) < 250
                    and classification.intent not in ("interested_warm", "meeting_confirmed", "ask_more_info")
                ):
                    print(f"  ⚠️ Override classifier ({classification.intent} → interested_warm) : reply court avec acquiescement explicite")
                    # Classification est un dataclass FROZEN → pas de mutation in-place
                    # ('cannot assign to field intent'). On recrée l'instance.
                    classification = _dc_replace(
                        classification,
                        intent="interested_warm",
                        confidence=max(classification.confidence, 0.85),
                    )

                # GARDE-FOU ÉLARGI (2026-06-15) — BIAIS VERS L'ALERTE.
                # Le coût d'une fausse alerte (Rudy relit un truc auto-gérable) est
                # FAIBLE. Le coût d'un faux auto-send (le bot répond maladroitement
                # à une opportunité chaude) est ÉLEVÉ et silencieux. Donc dès qu'un
                # reply contient un signal clair d'intérêt / RDV / demande d'info /
                # "plus tard", on FORCE l'intent ALERT_ONLY correspondant, même si
                # le classifier avait dit AUTOSEND. Cas vus le 15/06 : biorniz
                # ("j'aimerais des précisions"), atelier.bosc ("on peut s'appeler,
                # vos dispos ?"), stephanie ("recontactez-moi dans 6 mois") →
                # auto-répondus au lieu d'alertés.
                _meeting_signals = (
                    "quelles sont vos disponibilit", "vos disponibilit",
                    "on peut s'appeler", "on peut se voir", "on peut échanger",
                    "peut-on s'appeler", "peut on s'appeler", "rappelez-moi",
                    "rappelez moi", "me rappeler", "un créneau", "un creneau",
                    "caler un", "fixer un rendez", "prendre rendez", "un rdv",
                    "passer un appel", "un call", "par téléphone", "par telephone",
                    "mon numéro", "mon numero", "joignable au", "disponible mardi",
                    "disponible lundi", "disponible mercredi", "disponible jeudi",
                    "disponible vendredi",
                )
                _info_signals = (
                    "j'aimerais des précisions", "j'aimerais des precisions",
                    "des précisions", "des precisions", "pouvez-vous m'en dire",
                    "pouvez vous m'en dire", "en savoir plus", "plus d'informations",
                    "plus d'infos", "plus de détails", "plus de details",
                    "envoyez-moi", "envoyez moi", "votre plaquette", "une plaquette",
                    "vos tarifs", "votre tarif", "combien", "votre offre",
                    "comment ça marche", "comment ca marche",
                )
                # ⚠️ NE garder QUE les vraies demandes de recontact EXPLICITES.
                # Les soft-no ("plus tard", "pas pour le moment", "pas pour
                # l'instant", "ce n'est pas le moment") sont désormais des refus
                # polis → not_interested_polite (réponse auto), PAS des alertes
                # (feedback Rudy 16/06 : n.david, gpharmaciese, anne.dumas).
                _timing_signals = (
                    "recontactez-moi", "recontactez moi", "recontactez-nous",
                    "recontactez nous", "recontacter", "revenez vers moi",
                    "revenir vers moi", "rappelez-moi en", "rappelez-moi dans",
                    "recontactez-moi en", "recontactez-moi dans",
                    "représentez-moi", "representez-moi", "à la rentrée",
                )
                _interest_signals = (
                    "ça m'intéresse", "ca m'intéresse", "cela m'intéresse",
                    "ça nous intéresse", "ca nous interesse", "je suis intéressé",
                    "nous sommes intéressé", "ce qui m'intéresse",
                )
                _forced = None
                if any(s in _body_full_low for s in _meeting_signals):
                    _forced = "meeting_confirmed"
                elif any(s in _body_full_low for s in _interest_signals):
                    _forced = "interested_warm"
                elif any(s in _body_full_low for s in _info_signals):
                    _forced = "ask_more_info"
                elif any(s in _body_full_low for s in _timing_signals):
                    _forced = "objection_timing"
                if (
                    _forced
                    and classification.intent in AUTOSEND_ELIGIBLE
                ):
                    print(f"  ⚠️ Override classifier ({classification.intent} → {_forced}) : signal d'opportunité détecté → ALERTE (biais sécurité)")
                    # dataclass FROZEN → recrée l'instance (pas de mutation in-place)
                    classification = _dc_replace(
                        classification,
                        intent=_forced,
                        confidence=max(classification.confidence, 0.80),
                    )
                print(f"    key_phrase: {_short(classification.key_phrase, 120)}")
                if classification.redirected_to:
                    print(f"    redirected_to: {classification.redirected_to}")
                if classification.redirected_email:
                    print(f"    redirected_email: {classification.redirected_email}")

                # === RACCOURCI 0 : prospect partage SON calendrier / lien de booking ===
                # Le bot ne peut pas booker dans le calendrier du prospect.
                # → on N'envoie PAS de réponse (sinon on raconte n'importe quoi)
                # → on remonte une ALERTE au dashboard pour que Rudy aille booker
                # → on marque traité (sinon retraitement infini)
                if classification.prospect_offers_calendar:
                    print("  >> Prospect partage SON calendrier → alerte dashboard, pas de réponse auto")
                    log_entry["intent"] = "interested_warm_calendar_shared"
                    log_entry["confidence"] = classification.confidence
                    log_entry["key_phrase"] = classification.key_phrase
                    log_entry["actions"] = ["alerte envoyée (prospect partage son calendrier)"]
                    log_entry["dry_run"] = dry_run
                    logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    if kvstore and kvstore.kv_available():
                        kvstore.log_action({
                            "at": now_utc.isoformat(),
                            "from": reply.from_email,
                            "subject": reply.subject,
                            "intent": "interested_warm",
                            "status": "⚠️ ALERTE : prospect partage son calendrier — à booker à la main",
                            "reply": _trim_quoted_history(_strip_html(reply.body))[:2000],
                            "response": "(aucune — Rudy doit aller sur le calendrier du prospect)",
                        })
                    if not dry_run:
                        _mark_done(reply.message_id)
                    processed_count += 1
                    continue

                # === RACCOURCI 1 : intents silencieux (unsub / hostile / bounce_or_auto) ===
                # Aucun email à envoyer → on n'a JAMAIS besoin de drafter (économie Sonnet)
                # et la fenêtre d'envoi n'a aucun sens ici. On exécute tout de suite
                # (blacklist + tag) et on marque traité.
                if classification.intent in ALWAYS_SILENT:
                    silent_plan = plan_actions(
                        reply=reply,
                        prospect=prospect,
                        classification=classification,
                        draft=None,
                        min_autosend_confidence=min_conf,
                        has_calendar_slots=False,
                    )
                    silent_results = execute_plan(
                        silent_plan, reply, mr, dry_run=dry_run, tag_cache=tag_cache
                    )
                    for line in silent_results:
                        print(f"    {line}")
                    log_entry["intent"] = classification.intent
                    log_entry["confidence"] = classification.confidence
                    log_entry["key_phrase"] = classification.key_phrase
                    log_entry["actions"] = silent_results
                    log_entry["dry_run"] = dry_run
                    logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    if kvstore and kvstore.kv_available():
                        kvstore.log_action({
                            "at": now_utc.isoformat(),
                            "from": reply.from_email,
                            "subject": reply.subject,
                            "intent": classification.intent,
                            "status": "exécuté (silencieux)",
                            "reply": _trim_quoted_history(_strip_html(reply.body))[:2000],
                            "response": "(pas de réponse — silencieux)",
                        })
                    if not dry_run:
                        _mark_done(reply.message_id)
                        if kvstore and kvstore.kv_available():
                            kvstore.clear_held_seen(reply.message_id)
                    processed_count += 1
                    continue

                # === RACCOURCI 1bis : ALERT_ONLY — leads chauds, RDV, plus tard ===
                # Le bot NE répond PAS. Envoie une alerte email à Rudy avec toutes
                # les infos pour qu'il gère manuellement (RDV, lead chaud, "plus
                # tard"). Met juste à jour le statut côté ManyReach + log KV.
                # NOTE : wrong_person_redirect (changement d'adresse / personne
                # partie / autoreply congés) est désormais dans ALWAYS_SILENT —
                # plus d'alerte ni de réponse auto. Rudy ne veut pas être dérangé
                # par ces cas (validé 2026-06-05).
                from src.actions import ALERT_ONLY  # local import to avoid cycles
                if classification.intent in ALERT_ONLY:
                    print(f"  >> ALERT_ONLY ({classification.intent}) — alerte Rudy, pas de réponse auto")
                    # Mise à jour du statut prospect (sans envoyer de mail)
                    alert_plan = plan_actions(
                        reply=reply,
                        prospect=prospect,
                        classification=classification,
                        draft=None,
                        min_autosend_confidence=min_conf,
                        has_calendar_slots=False,
                    )
                    # On exécute uniquement update_prospect + add_tag (pas de send)
                    alert_plan.actions = [a for a in alert_plan.actions if a.kind in ("update_prospect", "add_tag")]
                    if alert_plan.actions:
                        try:
                            execute_plan(alert_plan, reply, mr, dry_run=dry_run, tag_cache=tag_cache)
                        except Exception as _e:  # noqa: BLE001
                            print(f"  !! status update fail: {_e}")
                    # Pas d'envoi automatique — Rudy gère tout depuis le dashboard.
                    # On garde l'enregistrement KV (avec prospect_id pour le lien
                    # ManyReach) + une trace locale.
                    alert_line = f"🔔 ALERTE détectée — visible dashboard ({classification.intent})"
                    print(f"    {alert_line}")
                    # Log KV pour visibilité dans le dashboard
                    log_entry["intent"] = classification.intent
                    log_entry["confidence"] = classification.confidence
                    log_entry["key_phrase"] = classification.key_phrase
                    log_entry["actions"] = [alert_line]
                    log_entry["dry_run"] = dry_run
                    logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    if kvstore and kvstore.kv_available():
                        # ⚠️ campaign_id = celui DU REPLY (None si orphelin/hors
                        # campagne). C'est lui qui décide du lien dashboard :
                        #   - reply DANS une campagne (classique) → lien ManyReach
                        #     inbox scopé campagne = MARCHE.
                        #   - reply orphelin (campaign_id=None) → la conversation
                        #     n'est dans AUCUNE campagne côté ManyReach, donc une
                        #     URL inbox scopée campagne reste VIDE (confirmé par
                        #     Rudy) → on NE met PAS la campagne d'origine ici (sinon
                        #     lien mort), le dashboard bascule sur mailto.
                        # On garde la campagne d'origine + prospect_email à part,
                        # pour info (badge "conversation hors campagne X").
                        _origin_camp = None
                        if not reply.campaign_id and thread:
                            for _m in sorted(thread, key=lambda x: x.created_at):
                                if _m.type in ("Sent", "SentManual") and _m.campaign_id:
                                    _origin_camp = _m.campaign_id
                                    break
                        kvstore.log_action({
                            "at": now_utc.isoformat(),
                            "from": reply.from_email,
                            "subject": reply.subject,
                            "intent": classification.intent,
                            "status": "🔔 ALERTE — à traiter dashboard",
                            "reply": _trim_quoted_history(_strip_html(reply.body))[:2000],
                            "response": alert_line,
                            "prospect_id": (prospect.prospect_id if prospect else None),
                            "campaign_id": reply.campaign_id,   # None si orphelin → mailto
                            "origin_campaign_id": _origin_camp,  # info seulement (badge)
                            "prospect_email": (prospect.email if prospect else None),
                            # message_id du reply → permet de RÉPONDRE via l'API
                            # ManyReach depuis le dashboard (même pour les orphelins
                            # que l'UI ManyReach n'affiche pas). Mailbox d'origine
                            # pour envoyer depuis le bon compte.
                            "message_id": reply.message_id,
                            "sender_mailbox": (original_outreach.from_email if original_outreach else None),
                        })
                    if not dry_run:
                        _mark_done(reply.message_id)
                    processed_count += 1
                    continue

                # === RACCOURCI 2 : intent qui enverrait un mail, mais hors fenêtre ===
                # Plutôt que de brûler Sonnet à drafter à chaque run de cron (toutes les
                # 15 min !) pour un message qui ne partira que ce matin, on log UNE FOIS
                # via held_seen (atomic SET NX) et on saute drafter+plan. On garde le
                # reply NON-traité → il sera ré-évalué quand la fenêtre s'ouvrira.
                window_open_now = send_window_open(now_utc) or args.only_email
                if not window_open_now and classification.intent in AUTOSEND_ELIGIBLE:
                    already_held = (
                        kvstore.mark_held_seen(reply.message_id)
                        if (kvstore and kvstore.kv_available())
                        else False
                    )
                    if already_held:
                        print("  >> HORS FENÊTRE — déjà gardé précédemment, on saute (économie tokens)")
                    else:
                        print("  >> HORS FENÊTRE — pas de draft maintenant, gardé pour 9h-19h Paris")
                        if kvstore and kvstore.kv_available():
                            kvstore.log_action({
                                "at": now_utc.isoformat(),
                                "from": reply.from_email,
                                "subject": reply.subject,
                                "intent": classification.intent,
                                "status": "en attente (fenêtre 9h-19h Paris)",
                                "reply": _trim_quoted_history(_strip_html(reply.body))[:2000],
                                "response": "(pas drafté — sera fait à l'ouverture de la fenêtre)",
                            })
                    log_entry["intent"] = classification.intent
                    log_entry["confidence"] = classification.confidence
                    log_entry["actions"] = ["deferred (hors fenêtre)"]
                    logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    # PAS de append_processed_id → sera réessayé au prochain run
                    processed_count += 1
                    continue

                # Draft. (Plus de créneaux Calendar ni de contexte site : les
                # intents qui menaient à une proposition de RDV sont désormais
                # remontés en alerte — Rudy gère les RDV à la main.)
                draft = drafter.draft(
                    reply=reply,
                    classification=classification,
                    original_outreach=original_outreach,
                    prospect=prospect,
                    style_guide=style_guide,
                    proposed_slots=None,
                    silent_on_not_interested=silent_on_not_interested,
                    company_context="",
                )

                if draft.skip_send:
                    print("  DRAFT  [skip_send=true]  no reply will be sent")
                elif draft.body_html:
                    preview = _short(draft.body_html, 240)
                    print(f"  DRAFT  ({len(draft.body_html)} chars HTML)")
                    print(f"    {preview}")
                if draft.notes:
                    print(f"    notes: {draft.notes}")

                plan = plan_actions(
                    reply=reply,
                    prospect=prospect,
                    classification=classification,
                    draft=draft,
                    min_autosend_confidence=min_conf,
                    has_calendar_slots=False,
                )

                # Controlled test: force-send for the explicitly targeted email
                if args.only_email and draft and draft.body_html and not draft.skip_send:
                    plan.auto_send = True
                    plan.review_reason = ""
                    print("  >>> TEST: envoi forcé pour ce prospect ciblé")

                # SEND WINDOW + CAP : ne pas envoyer hors heures ouvrées, ni au-delà du plafond.
                # Si bloqué ici, on NE marque PAS comme traité → réessai au prochain run.
                send_held = False
                if plan.auto_send and not args.only_email:
                    if not send_window_open(now_utc):
                        plan.auto_send = False
                        send_held = True
                        print("  >> HORS HEURES D'ENVOI — gardé pour le prochain run en horaire ouvré")
                    elif sends_done >= max_sends:
                        plan.auto_send = False
                        send_held = True
                        print(f"  >> PLAFOND {max_sends} envois/run atteint — gardé pour le prochain run")

                if plan.review_reason:
                    print(f"  REVIEW REASON: {plan.review_reason}")

                # Small human-like jitter before a real send (Vercel-safe: a few seconds)
                will_really_send = plan.auto_send and not dry_run and any(
                    a.kind == "send_reply" for a in plan.actions
                )
                if will_really_send:
                    lo, hi = send_cfg.get("inter_send_jitter_seconds", [0, 0])
                    if hi > 0:
                        time.sleep(random.uniform(lo, hi))

                results = execute_plan(plan, reply, mr, dry_run=dry_run, tag_cache=tag_cache)
                for line in results:
                    print(f"    {line}")
                if will_really_send:
                    sends_done += 1

                # NB : meeting_confirmed et objection_timing sont désormais traités
                # en amont par la branche ALERT_ONLY (Rudy gère RDV/relances à la
                # main). Plus de création d'event Google Agenda ici.

                log_entry["reply_clean"] = _trim_quoted_history(_strip_html(reply.body))
                log_entry["original_clean"] = (
                    _trim_quoted_history(_strip_html(original_outreach.body), 1500)
                    if original_outreach
                    else ""
                )
                log_entry["intent"] = classification.intent
                log_entry["confidence"] = classification.confidence
                log_entry["key_phrase"] = classification.key_phrase
                log_entry["redirected_to"] = classification.redirected_to
                log_entry["redirected_email"] = classification.redirected_email
                log_entry["draft_body"] = draft.body_html
                log_entry["draft_notes"] = draft.notes
                log_entry["draft_skip_send"] = draft.skip_send
                log_entry["auto_send"] = plan.auto_send
                log_entry["review_reason"] = plan.review_reason
                log_entry["dry_run"] = dry_run
                log_entry["actions"] = results
                logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

                # Journal cloud pour le dashboard (no-op si pas de KV)
                if kvstore and kvstore.kv_available():
                    _resp_txt = ""
                    if draft and draft.body_html:
                        _resp_txt = re.sub(r"<br\s*/?>", "\n", draft.body_html)
                        _resp_txt = re.sub(r"</p>", "\n", _resp_txt)
                        _resp_txt = re.sub(r"<[^>]+>", "", _resp_txt).strip()
                    _status = "envoyé" if (plan.auto_send and not dry_run) else (
                        "gardé (hors heures)" if send_held else "à relire"
                    )
                    kvstore.log_action({
                        "at": now_utc.isoformat(),
                        "from": reply.from_email,
                        "subject": reply.subject,
                        "intent": classification.intent,
                        "status": _status,
                        "reply": _trim_quoted_history(_strip_html(reply.body))[:2000],
                        "response": _resp_txt[:1800] if not draft.skip_send else "(pas de réponse — silencieux)",
                    })

                # Marquer comme traité dès que le bot a TENTÉ de gérer le reply
                # (classify + plan ont tourné). Même si l'envoi a été held en
                # review pour confidence basse, on a essayé → on ne ré-essaie
                # pas indéfiniment. Sinon les prospects en review consomment le
                # quota cron et BLOQUENT les nouveaux replies (cause du retard
                # global). Seule exception : send_held (fenêtre/cap), où il faut
                # vraiment re-essayer plus tard.
                attempted = (not dry_run) and (not send_held)
                if attempted:
                    _mark_done(reply.message_id)
                    if kvstore and kvstore.kv_available():
                        kvstore.clear_held_seen(reply.message_id)
                heavy_count += 1  # itération lourde (draft+send) consommée — c'est ELLE qui compte vis-à-vis du quota cron
                processed_count += 1

            except Exception as e:
                error_count += 1
                print(f"  ERROR processing reply: {e}")
                traceback.print_exc()
                log_entry["error"] = repr(e)
                logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                # JOURNAL KV : on EXPOSE l'erreur dans le dashboard pour qu'on la
                # voie. Avant, les exceptions disparaissaient dans /tmp (éphémère
                # Vercel) → cause des "trous" silencieux du flux.
                if kvstore is not None and kvstore.kv_available():
                    try:
                        kvstore.log_action({
                            "at": datetime.now(timezone.utc).isoformat(),
                            "from": reply.from_email,
                            "subject": reply.subject,
                            "intent": "error",
                            "status": f"❌ ERREUR : {str(e)[:200]}",
                            "reply": _trim_quoted_history(_strip_html(reply.body))[:500],
                            "response": traceback.format_exc()[-800:],
                        })
                    except Exception:  # noqa: BLE001
                        pass

    if kvstore and kvstore.kv_available():
        kvstore.set_last_run(datetime.now(timezone.utc).isoformat())

    print()
    print("─" * 80)
    print(
        f"Done. processed={processed_count}  skipped_already_done={skipped_count}  "
        f"errors={error_count}"
    )
    if mailinblack_pending:
        print()
        print("─" * 80)
        print(f"⚠  {len(mailinblack_pending)} MailInBlack en attente de TON clic manuel :")
        print("─" * 80)
        for item in mailinblack_pending:
            print(f"  • Va dans la boîte  {item['destination_mailbox']}")
            print(f"    Cherche le mail de  {item['challenge_from']}")
            print(f"    Sujet               {item['subject']}")
            print(f"    Reçu le             {item['createdAt']}")
            print(f"    → Clique le bouton 'Un clic pour délivrer votre email !'")
            print()
        print("(Cette étape sera automatisée quand on aura Gmail/IMAP API en place.)")
    print(f"Full log: {log_file}")
    if dry_run:
        print("Mode: DRY-RUN — aucune écriture vers ManyReach. Relance avec --no-dry-run pour exécuter.")
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
