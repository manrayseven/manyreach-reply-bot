"""Relances (bumps) des prospects intéressés sans réponse.

Usage :
    py scripts/run_bumps.py                # dry-run (montre qui serait relancé)
    py scripts/run_bumps.py --no-dry-run   # envoie réellement les relances

Stateless : déduit l'état de relance du thread ManyReach (voir src/bumps.py).
Respecte la fenêtre d'envoi (heures ouvrées) et les créneaux Calendar.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.bumps import build_bump_body, build_promise_body, decide_bump  # noqa: E402
from src import kvstore  # noqa: E402
from src.manyreach import ManyReachClient, ManyReachError  # noqa: E402

BUMP_STATUSES = ("Interested", "MaybeLater")


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Relance les prospects intéressés sans réponse.")
    ap.add_argument("--no-dry-run", action="store_true", help="Envoie réellement les relances")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    dry_run = not args.no_dry_run

    settings = yaml.safe_load((PROJECT_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8-sig")) or {}
    fu = settings.get("followups", {}).get("interested_awaiting_booking", {})
    bump_days = fu.get("bump_after_days", [2, 5])
    send_cfg = settings.get("sending", {}) or {}

    # Calendar (optionnel) pour proposer des créneaux concrets dans la relance
    cal_cfg = settings.get("calendar", {}) or {}
    calendar_client = None
    if cal_cfg.get("enabled"):
        try:
            from src.calendar_slots import CalendarClient
            calendar_client = CalendarClient()
        except Exception as e:
            print(f"Calendar off ({e})")

    def slots_text():
        if not calendar_client:
            return None
        try:
            slots = calendar_client.find_free_slots(
                working_hours=cal_cfg.get("working_hours", {}),
                tz_name=cal_cfg.get("timezone", "Europe/Paris"),
                duration_min=int(cal_cfg.get("meeting_duration_minutes", 30)),
                days_ahead=int(cal_cfg.get("days_ahead", 5)),
                max_slots=3,
                min_lead_hours=int(cal_cfg.get("min_lead_hours", 3)),
            )
            return ", ".join(s.fr() for s in slots) if slots else None
        except Exception:
            return None

    now = datetime.now(timezone.utc)

    def send_window_open():
        hours = send_cfg.get("allowed_hours", [9, 19])
        days = send_cfg.get("allowed_weekdays", [0, 1, 2, 3, 4])
        # IMPORTANT : heure Paris, pas heure serveur (UTC sur Vercel).
        tz_name = (settings.get("calendar", {}) or {}).get("timezone", "Europe/Paris")
        try:
            from zoneinfo import ZoneInfo
            local = now.astimezone(ZoneInfo(tz_name))
        except Exception:
            local = now.astimezone()
        return local.weekday() in days and hours[0] <= local.hour < hours[1]

    print(f"\n=== Relances — {'DRY-RUN' if dry_run else 'LIVE'} — cadence J+{bump_days} ===\n")
    if not dry_run and not send_window_open():
        print("Hors fenêtre d'envoi — on ne relance pas maintenant. Réessaie en heures ouvrées.")
        return 0

    import os as _os, time as _time
    # 50 s : /api/bumps a un timeout de 60 s (vercel.json).
    budget_s = float(_os.environ.get("BUMPS_BUDGET_SECONDS", "50"))
    start_ts = _time.time()

    # PLAFOND JOURNALIER (Rudy 19/11). Journalier et non par passage : c'est ce
    # qui rend sur d'appeler /api/bumps toutes les heures. Sans lui, resorber un
    # retard de 149 relances enverrait 149 emails d'un coup.
    jour = now.date().isoformat()
    plafond = int(_os.environ.get("BUMPS_MAX_PER_DAY", "25"))
    try:
        deja_aujourdhui = kvstore.bump_sent_today(jour)
    except Exception:  # noqa: BLE001
        deja_aujourdhui = 0
    if deja_aujourdhui >= plafond and not dry_run:
        print(f"Plafond du jour atteint ({deja_aujourdhui}/{plafond}) — stop.")
        return 0

    # MEMOIRE DE PASSAGE : qui a deja ete EXAMINE recemment. Sauter une adresse
    # ici ne coute AUCUN appel API ; c'est ce qui fait avancer le balayage au
    # lieu de rabacher les vingt plus recents (voir src/kvstore.py).
    rescan_j = float(_os.environ.get("BUMP_RESCAN_DAYS", "3"))
    try:
        deja_vus = kvstore.bump_seen()
    except Exception:  # noqa: BLE001
        deja_vus = {}

    def _a_revoir_plus_tard(em: str) -> bool:
        """Vrai si on sait deja qu'il est trop tot pour ce prospect.

        La memoire ne stocke pas « vu le X » mais « a revoir le Y » : un fil
        ou le prospect a parle en dernier, ou deja relance trois fois, ne
        merite pas un appel API par jour. C'est ce qui fait converger le
        balayage — apres un premier tour, le quota ne sert plus qu'aux
        prospects reellement dus.
        """
        iso = deja_vus.get((em or "").lower().strip())
        if not iso:
            return False
        try:
            quand = datetime.fromisoformat(iso)
        except ValueError:
            return False
        if quand.tzinfo is None:
            quand = quand.replace(tzinfo=timezone.utc)
        return quand > now

    def _revoir_dans(jours: float) -> str:
        return (now + timedelta(days=jours)).isoformat()

    vus_ce_passage: dict = {}
    n_examines = 0
    seen: set[str] = set()
    n_bumped = 0
    with ManyReachClient() as mr:
        # ── RENDEZ-VOUS DONNÉS PAR LES PROSPECTS EUX-MÊMES (Rudy 16/11) ──
        # « Je pense, 1er trimestre 2027. » Ces fils finissent par une réponse
        # du prospect : `decide_bump` refuse de les relancer, par construction.
        # On les traite donc AVANT, sur leur date à eux. Un rendez-vous est
        # consommé dans tous les cas — relancé, ou retiré parce qu'il n'a plus
        # lieu d'être. Sinon l'agenda se remplit de dettes qu'on ne solde pas.
        n_promesses = 0
        try:
            dues = kvstore.relances_dues(now.date().isoformat())
        except Exception as _e:  # noqa: BLE001
            dues = []
            print(f"  !! agenda de relance illisible : {_e}")
        for rdv in dues:
            if (_time.time() - start_ts) > budget_s:
                print("  >> Budget atteint — le reste des rendez-vous au prochain run")
                break
            # Le plafond du jour vaut aussi pour les rendez-vous promis : un
            # prospect ne doit pas recevoir d'email parce qu'un compteur a ete
            # oublie sur une branche.
            if (deja_aujourdhui + n_bumped) >= plafond and not dry_run:
                print(f"  >> Plafond du jour atteint ({plafond}) — rendez-vous reportes")
                break
            email = str(rdv.get("email") or "")
            if not email:
                continue
            seen.add(email)
            prospect = mr.find_prospect_by_email(email)
            if not prospect:
                print(f"- {email} : fiche introuvable — rendez-vous retire")
                if not dry_run:
                    kvstore.drop_relance(email)
                continue
            # Statut terminal : meme lecture qu'ailleurs dans le bot (sendingStatus
            # pour Unsub/Hostile, confirmedStatus pour Won/NotInterested).
            statut = str(prospect.sending_status or "")
            confirme = str((prospect.raw or {}).get("confirmedStatus") or "")
            if statut in ("Unsub", "Hostile", "Bounced") or confirme in ("NotInterested", "Won"):
                statut = statut or confirme
                print(f"- {email} : statut {statut} — rendez-vous retire, aucune relance")
                if not dry_run:
                    kvstore.drop_relance(email)
                continue
            try:
                thread = mr.get_prospect_thread(prospect.prospect_id)
            except ManyReachError as _e:
                if getattr(_e, "status", None) == 429:
                    print("  >> Quota ManyReach atteint — rendez-vous reportes")
                    break
                continue
            # A-t-il repris contact depuis ? Alors le rendez-vous est caduc :
            # c'est une conversation en cours, pas une promesse a tenir.
            pose = str(rdv.get("pose_le") or "")
            repris = False
            for m in thread or []:
                if m.type == "Reply" and pose and m.created_at.isoformat() > pose:
                    repris = True
                    break
            if repris:
                print(f"- {email} : a repondu depuis — rendez-vous retire")
                if not dry_run:
                    kvstore.drop_relance(email)
                continue
            last_reply = next(
                (m for m in sorted(thread or [], key=lambda x: x.created_at, reverse=True)
                 if m.type == "Reply"), None)
            if not last_reply:
                if not dry_run:
                    kvstore.drop_relance(email)
                continue
            body = build_promise_body(prospect, str(rdv.get("le") or ""), str(rdv.get("dit") or ""))
            print(f"- {email} → relance promise ({rdv.get('le')} : « {str(rdv.get('dit'))[:60]} »)")
            if dry_run:
                print(f"    [DRY-RUN] enverrait : {body[:140]}...")
            else:
                mr.send_reply(message_id=last_reply.message_id, body_html=body, send_as_reply=True)
                kvstore.drop_relance(email)
                print("    [ENVOYÉ ✓] relance promise")
            n_promesses += 1
            n_bumped += 1
        if n_promesses:
            print(f"  ({n_promesses} rendez-vous donnes par les prospects eux-memes)")

        try:
          _flux = list(mr.list_replies(confirmed_statuses=BUMP_STATUSES,
                                       max_per_status=args.limit))
        except ManyReachError as _e:
            print(f"  !! liste des reponses illisible ({str(_e)[:90]}) — "
                  f"passage ecourte, trace quand meme ecrite")
            _flux = []
        for reply in _flux:
            # Budget de temps : on s'arrête avant le timeout Vercel (60s). Le reste
            # sera relancé au prochain run quotidien (la cadence J+2/J+5 tolère ça).
            if (_time.time() - start_ts) > budget_s:
                print(f"  >> Budget {budget_s}s atteint — stop, suite au prochain run quotidien")
                break
            if (deja_aujourdhui + n_bumped) >= plafond and not dry_run:
                print(f"  >> Plafond du jour atteint ({plafond}) — suite au prochain passage")
                break
            email = reply.from_email
            if email in seen:
                continue
            seen.add(email)
            # Saute SANS appel API : c'est tout l'interet de la memoire.
            if _a_revoir_plus_tard(email):
                continue
            _cle = (email or "").lower().strip()
            # Par defaut on redemande demain ; chaque issue ci-dessous ajuste.
            vus_ce_passage[_cle] = _revoir_dans(rescan_j)
            n_examines += 1
            # Un identifiant de fiche en cache = une requete economisee, donc
            # deux fois plus de prospects examines dans le meme budget.
            prospect = None
            try:
                _pid = kvstore.cached_prospect_id(email)
            except Exception:  # noqa: BLE001
                _pid = None
            if _pid:
                try:
                    prospect = mr.get_prospect(_pid)
                except Exception:  # noqa: BLE001
                    prospect = None
            if not prospect:
                try:
                    prospect = mr.find_prospect_by_email(email)
                except ManyReachError as _e:
                    if getattr(_e, "status", None) == 429:
                        print("  >> Quota ManyReach atteint — arret propre")
                        break
                    continue
                if prospect:
                    try:
                        kvstore.cache_prospect_id(email, prospect.prospect_id)
                    except Exception:  # noqa: BLE001
                        pass
            if not prospect:
                vus_ce_passage[_cle] = _revoir_dans(30)
                continue
            # LE QUOTA EST CELUI DU COMPTE, PAS DU PROCESSUS (constate le 19/11
            # en simulation). /api/cron tourne toutes les 5 minutes et puise au
            # meme seau de 60 requetes/minute : le regulateur interne ne peut
            # pas l'anticiper. Avant, un 429 remontait jusqu'en haut et TUAIT
            # le passage — on perdait tout le travail deja fait, trace comprise.
            # Desormais on s'arrete proprement : la memoire de passage garde ce
            # qui a ete examine, et le passage suivant reprend plus loin.
            try:
                thread = mr.get_prospect_thread(prospect.prospect_id)
            except ManyReachError as _e:
                if getattr(_e, "status", None) == 429:
                    print("  >> Quota ManyReach atteint (le compte est partage "
                          "avec /api/cron) — arret propre, suite au prochain passage")
                    break
                print(f"  !! {email} : fil illisible ({str(_e)[:90]})")
                continue
            decision = decide_bump(thread, now, bump_days)
            # QUAND FAUDRA-T-IL LE REGARDER A NOUVEAU ? Toute l'economie est la.
            try:
                _ordre = sorted(thread, key=lambda m: m.created_at)
                _dernier = _ordre[-1] if _ordre else None
                _trail = 0
                for _m in reversed(_ordre):
                    if _m.type in ("Sent", "SentManual"):
                        _trail += 1
                    else:
                        break
                if _dernier is None:
                    vus_ce_passage[_cle] = _revoir_dans(30)
                elif _dernier.type == "Reply":
                    # Le prospect a parle en dernier : ce n'est pas une relance
                    # qu'il attend, c'est une reponse. Rien a faire ici.
                    vus_ce_passage[_cle] = _revoir_dans(7)
                elif _trail >= 3:
                    # Trois messages sans reponse : on arrete (nurture).
                    vus_ce_passage[_cle] = _revoir_dans(90)
                elif not decision.should_bump:
                    _age = (now - _dernier.created_at).total_seconds() / 86400.0
                    _seuil = bump_days[0] if _trail <= 1 else (bump_days[1] - bump_days[0])
                    vus_ce_passage[_cle] = _revoir_dans(max(0.5, _seuil - _age))
            except Exception:  # noqa: BLE001
                pass
            if not decision.should_bump:
                continue
            body = build_bump_body(decision.bump_number, prospect, slots_text())
            # On répond au dernier message du prospect dans le thread
            last_reply = next(
                (m for m in sorted(thread, key=lambda x: x.created_at, reverse=True) if m.type == "Reply"),
                None,
            )
            if not last_reply:
                continue
            print(f"- {email} → relance #{decision.bump_number} ({decision.reason})")
            if dry_run:
                print(f"    [DRY-RUN] enverrait : {body[:120]}...")
            else:
                try:
                    mr.send_reply(message_id=last_reply.message_id,
                                  body_html=body, send_as_reply=True)
                except ManyReachError as _e:
                    print(f"  !! {email} : envoi refuse ({str(_e)[:90]})")
                    if getattr(_e, "status", None) == 429:
                        break
                    continue
                try:
                    kvstore.add_bump_sent(jour)
                except Exception:  # noqa: BLE001
                    pass
                print(f"    [ENVOYÉ ✓] relance #{decision.bump_number}")
            vus_ce_passage[_cle] = _revoir_dans(
                max(1.0, bump_days[1] - bump_days[0]) if decision.bump_number == 1 else 90)
            n_bumped += 1

    # TRACE (Rudy 19/11) : sans elle, impossible de savoir si les relances
    # tournent. C'est ce silence qui a laisse le moteur affame des semaines.
    if not dry_run:
        try:
            kvstore.mark_bump_seen_many(vus_ce_passage)
            kvstore.set_bump_run({
                "at": now.isoformat(),
                "examines": n_examines,
                "envoyees": n_bumped,
                "promesses": n_promesses,
                "jour": jour,
                "du_jour": kvstore.bump_sent_today(jour),
                "plafond": plafond,
            })
        except Exception as _e:  # noqa: BLE001
            print(f"  !! trace non ecrite : {_e}")
    print(f"{n_examines} prospect(s) examine(s), "
          f"{n_bumped} relance(s) {'simulee(s)' if dry_run else 'envoyee(s)'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
