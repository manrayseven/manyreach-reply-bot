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
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.bumps import build_bump_body, decide_bump  # noqa: E402
from src.manyreach import ManyReachClient  # noqa: E402

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
    budget_s = float(_os.environ.get("BUMPS_BUDGET_SECONDS", "45"))
    start_ts = _time.time()

    seen: set[str] = set()
    n_bumped = 0
    with ManyReachClient() as mr:
        for reply in mr.list_replies(confirmed_statuses=BUMP_STATUSES, max_per_status=args.limit):
            # Budget de temps : on s'arrête avant le timeout Vercel (60s). Le reste
            # sera relancé au prochain run quotidien (la cadence J+2/J+5 tolère ça).
            if (_time.time() - start_ts) > budget_s:
                print(f"  >> Budget {budget_s}s atteint — stop, suite au prochain run quotidien")
                break
            email = reply.from_email
            if email in seen:
                continue
            seen.add(email)
            prospect = mr.find_prospect_by_email(email)
            if not prospect:
                continue
            thread = mr.get_prospect_thread(prospect.prospect_id)
            decision = decide_bump(thread, now, bump_days)
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
                mr.send_reply(message_id=last_reply.message_id, body_html=body, send_as_reply=True)
                print(f"    [ENVOYÉ ✓] relance #{decision.bump_number}")
            n_bumped += 1

    print(f"\nTerminé. {n_bumped} relance(s) {'simulée(s)' if dry_run else 'envoyée(s)'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
