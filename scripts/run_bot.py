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
from src.alerts import send_meeting_alert  # noqa: E402
from src.classifier import Classifier, _strip_html, _trim_quoted_history  # noqa: E402
from src.drafter import Drafter  # noqa: E402
from src.manyreach import ManyReachClient, is_bounce_or_auto, is_mailinblack  # noqa: E402
from src.slot_holds import SlotHoldStore  # noqa: E402

# Intents for which the bot may propose concrete calendar slots
SLOT_INTENTS = {"interested_warm", "interested_lukewarm", "ask_more_info"}


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
    drafter = Drafter(model=_models["drafter"]) if _models.get("drafter") else Drafter()
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
        """Don't reply instantly — a human lets a message age a bit."""
        min_age = int(send_cfg.get("min_reply_age_minutes", 0))
        jitter = int(send_cfg.get("max_reply_age_jitter_minutes", 0))
        if min_age <= 0 and jitter <= 0:
            return True
        # Deterministic per-message jitter so the threshold is stable across runs
        extra = (abs(hash(reply.message_id)) % (jitter + 1)) if jitter > 0 else 0
        return (when - reply.created_at) >= timedelta(minutes=min_age + extra)

    # --- Google Calendar (optional) ---
    cal_cfg = settings.get("calendar", {}) or {}
    calendar_client = None
    hold_store = None
    if cal_cfg.get("enabled"):
        try:
            from src.calendar_slots import CalendarClient

            calendar_client = CalendarClient()
            hold_store = SlotHoldStore(hold_days=int(cal_cfg.get("hold_days", 5)))
            print(f"Calendar: connecté ({calendar_client.whoami()})")
        except Exception as e:
            print(f"Calendar: désactivé (erreur d'init : {e})")
            calendar_client = None

    def compute_slots(prospect_email: str):
        """Return (list[Slot], list[str]) of free slots excluding others' holds."""
        if not calendar_client or not hold_store:
            return [], None
        try:
            held = hold_store.held_starts_for_others(prospect_email)
            slot_objs = calendar_client.find_free_slots(
                working_hours=cal_cfg.get("working_hours", {}),
                tz_name=cal_cfg.get("timezone", "Europe/Paris"),
                duration_min=int(cal_cfg.get("meeting_duration_minutes", 30)),
                buffer_min=int(cal_cfg.get("buffer_minutes", 15)),
                days_ahead=int(cal_cfg.get("days_ahead", 5)),
                max_slots=3,
                exclude_starts=held,
                min_lead_hours=int(cal_cfg.get("min_lead_hours", 3)),
                late_cutoff_hour=int(cal_cfg.get("late_cutoff_hour", 16)),
                next_day_min_time_if_late=cal_cfg.get("next_day_min_time_if_late", "10:30"),
            )
            return slot_objs, [s.fr() for s in slot_objs]
        except Exception as e:
            print(f"  !! find_free_slots a échoué : {e}")
            return [], None

    def _clean_name(prospect) -> str | None:
        """first_name du prospect, sauf s'il est corrompu (timestamps)."""
        if not prospect or not prospect.first_name:
            return None
        fn = str(prospect.first_name)
        # données corrompues côté ManyReach : firstName contient parfois un timestamp
        if fn[:4].isdigit() or "-" in fn[:7] or ":" in fn:
            return None
        return fn

    def create_meeting_event(classification, reply, prospect, dry_run: bool) -> str | None:
        """Crée l'event Google Agenda UNIQUEMENT si une date+heure explicite a été
        extraite. Sinon (date inventée/absente), on NE crée RIEN et on laisse Rudy
        caler à la main via l'alerte — pour ne jamais poser un faux RDV.
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        from src.calendar_slots import build_meeting_description, build_meeting_title

        iso = classification.confirmed_datetime
        # Nom lisible : nom extrait du reply > company > prénom (non corrompu) > email
        who = (
            classification.prospect_name
            or (prospect.company if prospect and prospect.company else None)
            or _clean_name(prospect)
            or reply.from_email
        )
        offer = classification.offer_label or "échange"

        # GARDE-FOU : pas de date explicite → on ne crée pas d'event (évite les faux RDV)
        if not iso:
            return (
                "[RDV] Pas de date/heure explicite dans le message → AUCUN event créé "
                f"(évite un faux RDV). À caler à la main. Message : {classification.key_phrase!r}"
            )
        if not calendar_client:
            return "[RDV] Calendar non connecté → à créer à la main"
        try:
            start = _dt.fromisoformat(iso)
            if start.tzinfo is None:
                start = start.replace(tzinfo=_tz.utc)
        except ValueError:
            return f"[RDV] date non parsable ({iso!r}) → à créer à la main"

        # GARDE-FOU : la date doit être dans le futur proche (pas passée, pas absurde)
        now = _dt.now(_tz.utc)
        if start < now - _td(hours=1):
            return f"[RDV] date extraite dans le passé ({start.isoformat()}) → ignorée, à caler à la main"
        if start > now + _td(days=60):
            return f"[RDV] date extraite trop lointaine ({start.isoformat()}) → ignorée, à caler à la main"

        hhmm = start.strftime("%H.%M")
        title = f"{hhmm} {build_meeting_title(offer, who)}"
        description = build_meeting_description(
            email=reply.from_email,
            phone=classification.contact_phone,
            zoom_link=classification.zoom_link,
            website=(prospect.website if prospect else None),
            company=(prospect.company if prospect else None),
            notes=f"RDV pris automatiquement par le bot. Raison : {offer}. Message du prospect : {classification.key_phrase}",
        )
        if dry_run:
            return f"[DRY-RUN] créerait l'event '{title}' le {start.isoformat()}"
        # IDEMPOTENCE : ne JAMAIS recréer un event déjà posé pour ce prospect à
        # cette date (le même reply peut être re-traité à plusieurs runs du cron
        # tant que l'accusé de réception n'est pas encore visible dans le thread).
        exists = calendar_client.event_exists_for(
            email=reply.from_email,
            start=start,
            tz_name=cal_cfg.get("timezone", "Europe/Paris"),
        )
        if exists is True:
            return f"[RDV] event déjà existant pour {reply.from_email} le {start.isoformat()} — pas de doublon"
        # Invite Rudy en attendee → il aura l'event dans sa boîte
        # contact@webmarketing-conseil.fr (notif + apparition dans son agenda perso).
        notify_email = os.environ.get("NOTIFY_EMAIL", "contact@webmarketing-conseil.fr")
        try:
            calendar_client.create_event(
                title=title,
                start=start,
                duration_min=int(cal_cfg.get("meeting_duration_minutes", 30)),
                description=description,
                tz_name=cal_cfg.get("timezone", "Europe/Paris"),
                attendee_emails=[notify_email] if notify_email else None,
            )
            return f"[EXEC] event créé : '{title}' le {start.isoformat()} (Rudy invité : {notify_email})"
        except Exception as e:
            return f"[RDV] échec création event ({e}) — à créer à la main"

    def _schedule_recontact_event(
        *, calendar_client, reply, prospect, classification, cal_cfg
    ) -> str | None:
        """Crée un event Google Agenda "🔁 Relance" à la date demandée par le
        prospect (objection_timing). Idempotent : si un event existe déjà pour
        cette adresse dans les 6 mois à venir, on n'en crée pas un nouveau.
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        # Date cible : recontact_datetime fourni par le classifier, sinon J+90.
        iso = classification.recontact_datetime
        try:
            start = _dt.fromisoformat(iso) if iso else (_dt.now(_tz.utc) + _td(days=90))
            if start.tzinfo is None:
                start = start.replace(tzinfo=_tz.utc)
        except ValueError:
            start = _dt.now(_tz.utc) + _td(days=90)

        # On cale à 10h Paris pour visibilité dans l'agenda
        try:
            from zoneinfo import ZoneInfo
            paris = ZoneInfo(cal_cfg.get("timezone", "Europe/Paris"))
            start = start.astimezone(paris).replace(hour=10, minute=0, second=0, microsecond=0)
        except Exception:
            pass

        # Idempotence : si un event "relance" existe déjà pour ce prospect dans une fenêtre
        # large, on n'en crée pas un autre.
        exists = calendar_client.event_exists_for(
            email=reply.from_email,
            start=start,
            tz_name=cal_cfg.get("timezone", "Europe/Paris"),
            window_hours=24 * 60,  # 60 jours
        )
        if exists is True:
            return f"[RELANCE] event de relance déjà présent pour {reply.from_email} → pas de doublon"

        who = (
            classification.prospect_name
            or (prospect.company if prospect and prospect.company else None)
            or reply.from_email
        )
        title = f"🔁 Relance : {who}"
        notes = (
            f"Raison du report (extrait du reply) : {classification.key_phrase}\n\n"
            f"Le bot peut être branché pour envoyer la relance automatiquement "
            f"(cf. moteur run_bumps) ; sinon, relance manuelle à cette date."
        )
        from src.calendar_slots import build_meeting_description
        description = build_meeting_description(
            email=reply.from_email,
            phone=classification.contact_phone,
            zoom_link=classification.zoom_link,
            website=(prospect.website if prospect else None),
            company=(prospect.company if prospect else None),
            notes=notes,
        )
        notify_email = os.environ.get("NOTIFY_EMAIL", "contact@webmarketing-conseil.fr")
        try:
            calendar_client.create_event(
                title=title,
                start=start,
                duration_min=15,
                description=description,
                tz_name=cal_cfg.get("timezone", "Europe/Paris"),
                attendee_emails=[notify_email] if notify_email else None,
            )
            return f"[RELANCE] event de relance posé : '{title}' le {start.date().isoformat()}"
        except Exception as e:
            return f"[RELANCE] échec création event relance ({e})"

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

    if args.only_email:
        print(f">>> MODE TEST CONTRÔLÉ : uniquement {args.only_email} (envoi forcé si --no-dry-run)")
        confirmed_statuses = None  # ignore status filter, target the email directly

    with ManyReachClient() as mr, log_file.open("w", encoding="utf-8") as logf:
        for reply in mr.list_replies(
            campaign_id=campaign_id,
            since=since,
            confirmed_statuses=confirmed_statuses,
            email_from=args.only_email,
        ):
            # Le quota du cron protège du timeout Vercel — il ne porte QUE sur les
            # itérations "lourdes" (draft+send Sonnet). Les itérations "cheap"
            # (defer, silent, déjà-handled) sont quasi-gratuites en temps et tokens.
            if limit and heavy_count >= limit:
                break
            # Budget de temps : si on s'approche du timeout, on s'arrête net pour
            # AU MOINS sauver last_run et finir proprement (sinon dashboard mort).
            if _time_left() < 6.0:
                print(f"  >> BUDGET TEMPS écoulé ({run_budget_s}s) — arrêt propre")
                break
            if reply.message_id in processed_ids:
                skipped_count += 1
                continue

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
                        append_processed_id(processed_file, reply.message_id)
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
                        append_processed_id(processed_file, reply.message_id)
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
                        append_processed_id(processed_file, reply.message_id)
                    processed_count += 1
                    continue

                # ORPHAN REPLY (pas de prospect dans la base ManyReach) : la thread
                # idempotence ne marche pas (besoin d'un prospect_id), donc le bot
                # re-renvoyait à chaque run (vu pour sekou : 7 envois en 30 min).
                # Sentinel KV "orphan_sent:{message_id}" : SET NX 30 jours → un
                # envoi maxi par orphan.
                if (
                    prospect is None
                    and not args.only_email
                    and not args.reprocess
                    and kvstore is not None
                    and kvstore.kv_available()
                ):
                    already_orphan = kvstore.mark_orphan_sent(reply.from_email)
                    if already_orphan:
                        print(f"  >> Orphan sender {reply.from_email} déjà traité — skip définitif")
                        if not dry_run:
                            append_processed_id(processed_file, reply.message_id)
                        processed_count += 1
                        continue
                    print(f"  >> Orphan sender {reply.from_email} — 1er traitement, marqué pour ne pas re-renvoyer")

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
                print(f"    key_phrase: {_short(classification.key_phrase, 120)}")
                if classification.redirected_to:
                    print(f"    redirected_to: {classification.redirected_to}")
                if classification.redirected_email:
                    print(f"    redirected_email: {classification.redirected_email}")

                # === RACCOURCI 0 : prospect partage SON calendrier / lien de booking ===
                # Le bot ne peut pas booker dans le calendrier du prospect.
                # → on N'envoie PAS de réponse (sinon on raconte n'importe quoi)
                # → on alerte Rudy par email pour qu'il aille booker manuellement
                # → on marque traité (sinon retraitement infini)
                if classification.prospect_offers_calendar:
                    print("  >> Prospect partage SON calendrier → alerte Rudy, pas de réponse auto")
                    alert_line = send_meeting_alert(
                        prospect_email=reply.from_email,
                        prospect_name=(classification.prospect_name or (prospect.company if prospect else None)),
                        company=(prospect.company if prospect else None),
                        reply_snippet=_short(_trim_quoted_history(_strip_html(reply.body)), 400),
                        proposed_when="Le prospect partage son propre calendrier — Rudy doit booker manuellement",
                        campaign_id=reply.campaign_id,
                        dry_run=dry_run,
                        phone=classification.contact_phone,
                        in_calendar=False,
                    )
                    print(f"    {alert_line}")
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
                            "reply": _trim_quoted_history(_strip_html(reply.body))[:700],
                            "response": "(aucune — Rudy doit aller sur le calendrier du prospect)",
                        })
                    if not dry_run:
                        append_processed_id(processed_file, reply.message_id)
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
                            "reply": _trim_quoted_history(_strip_html(reply.body))[:700],
                            "response": "(pas de réponse — silencieux)",
                        })
                    if not dry_run:
                        append_processed_id(processed_file, reply.message_id)
                        if kvstore and kvstore.kv_available():
                            kvstore.clear_held_seen(reply.message_id)
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
                                "reply": _trim_quoted_history(_strip_html(reply.body))[:700],
                                "response": "(pas drafté — sera fait à l'ouverture de la fenêtre)",
                            })
                    log_entry["intent"] = classification.intent
                    log_entry["confidence"] = classification.confidence
                    log_entry["actions"] = ["deferred (hors fenêtre)"]
                    logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    # PAS de append_processed_id → sera réessayé au prochain run
                    processed_count += 1
                    continue

                # Compute concrete calendar slots for meeting-leading intents
                slot_objs, slot_strs = [], None
                company_context = ""
                if classification.intent in SLOT_INTENTS:
                    slot_objs, slot_strs = compute_slots(reply.from_email)
                    if slot_strs:
                        print(f"  SLOTS proposables : {', '.join(slot_strs)}")
                    # Personnalisation : on scanne le SITE du prospect (uniquement pour
                    # les prospects chauds → coût tokens minime, et seulement si site connu).
                    if prospect and prospect.website:
                        try:
                            from src.web_context import fetch_company_context
                            company_context = fetch_company_context(prospect.website)
                            if company_context:
                                print(f"  CONTEXTE site récupéré ({len(company_context)} car.)")
                        except Exception as e:
                            print(f"  !! fetch site échoué : {e}")

                # Draft
                draft = drafter.draft(
                    reply=reply,
                    classification=classification,
                    original_outreach=original_outreach,
                    prospect=prospect,
                    style_guide=style_guide,
                    proposed_slots=slot_strs,
                    silent_on_not_interested=silent_on_not_interested,
                    company_context=company_context,
                )

                # If the drafter actually used the slots, reserve them (soft-hold)
                if draft.slots_used and slot_objs and hold_store and not dry_run:
                    hold_store.record(reply.from_email, [s.start for s in slot_objs])
                    print(f"  SLOTS réservés pour {reply.from_email} (soft-hold)")
                elif draft.slots_used and slot_objs and dry_run:
                    print(f"  [DRY-RUN] aurait réservé {len(slot_objs)} créneaux pour {reply.from_email}")

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
                    has_calendar_slots=bool(slot_strs),
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

                # Meeting booked → create calendar event + release holds + alert Rudy
                if classification.intent == "meeting_confirmed":
                    event_line = create_meeting_event(
                        classification=classification,
                        reply=reply,
                        prospect=prospect,
                        dry_run=dry_run,
                    )
                    if event_line:
                        print(f"    {event_line}")
                    # Release any soft-holds for this prospect (meeting is set)
                    if hold_store and not dry_run:
                        hold_store.clear(reply.from_email)
                    # Alerte email à Rudy. On NE renvoie PAS d'alerte si l'event
                    # était un doublon déjà présent (sinon spam à chaque run).
                    is_duplicate = bool(event_line) and "déjà existant" in event_line
                    in_calendar = bool(event_line) and "event créé" in event_line
                    if not is_duplicate:
                        # firstName ManyReach parfois corrompu → on préfère le nom
                        # extrait du reply, sinon la société.
                        alert_name = (
                            classification.prospect_name
                            or (prospect.company if prospect and prospect.company else None)
                        )
                        alert_line = send_meeting_alert(
                            prospect_email=reply.from_email,
                            prospect_name=alert_name,
                            company=(prospect.company if prospect else None),
                            reply_snippet=_short(classification.key_phrase, 200),
                            proposed_when=(classification.confirmed_datetime or classification.key_phrase),
                            campaign_id=reply.campaign_id,
                            dry_run=dry_run,
                            phone=classification.contact_phone,
                            in_calendar=in_calendar,
                        )
                        print(f"    {alert_line}")

                # objection_timing → on pose un event Google Agenda "🔁 Relance"
                # à la date demandée par le prospect (ou J+90 par défaut). Comme
                # ça même si le bumps engine n'est pas câblé, Rudy voit le rappel
                # dans son agenda. Le bumps engine pourra ensuite envoyer la
                # relance automatique.
                if (
                    classification.intent == "objection_timing"
                    and calendar_client is not None
                    and not dry_run
                ):
                    relance_line = _schedule_recontact_event(
                        calendar_client=calendar_client,
                        reply=reply,
                        prospect=prospect,
                        classification=classification,
                        cal_cfg=cal_cfg,
                    )
                    if relance_line:
                        print(f"    {relance_line}")

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
                        "reply": _trim_quoted_history(_strip_html(reply.body))[:700],
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
                    append_processed_id(processed_file, reply.message_id)
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
