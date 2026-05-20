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
import sys
import textwrap
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Make `src.*` importable when running this script directly.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.actions import execute_plan, plan_actions, plan_mailinblack_actions  # noqa: E402
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
    args = parser.parse_args()

    settings = load_settings(args.settings)
    dry_run = not args.no_dry_run
    limit = args.limit if args.limit is not None else settings.get("limit_per_run")
    campaign_id = args.campaign or settings.get("campaign_filter")
    if isinstance(campaign_id, list):
        campaign_id = campaign_id[0] if campaign_id else None

    min_conf = float(settings.get("min_autosend_confidence", 0.92))
    silent_on_not_interested = bool(settings.get("silent_on_not_interested", True))

    style_guide = load_style_guide(args.training)

    logs_dir = PROJECT_ROOT / settings.get("logs", {}).get("dir", "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_file = logs_dir / f"run_{run_ts}.jsonl"
    processed_file = logs_dir / "processed_messages.txt"
    processed_ids = set() if args.reprocess else load_processed_ids(processed_file)

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

    classifier = Classifier()
    drafter = Drafter()
    tag_cache: dict[str, int] = {}

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

    def create_meeting_event(classification, reply, prospect, dry_run: bool) -> str | None:
        """Create the Google Calendar event for a confirmed meeting.

        Title  : '14.00 Call <offre> avec <entreprise/prénom>'
        Desc   : email / téléphone / zoom / site / entreprise.
        Returns a human-readable result line (or a manual-todo note).
        """
        from datetime import datetime as _dt

        from src.calendar_slots import build_meeting_description, build_meeting_title

        iso = classification.confirmed_datetime
        who = (
            (prospect.company if prospect and prospect.company else None)
            or (prospect.first_name if prospect and prospect.first_name else None)
            or reply.from_email
        )
        offer = classification.offer_label or "RDV"

        if not calendar_client or not iso:
            return (
                f"[RDV] à créer à la main (pas de date ISO extraite ou Calendar off) — "
                f"créneau dit : {classification.key_phrase!r}"
            )
        try:
            start = _dt.fromisoformat(iso)
        except ValueError:
            return f"[RDV] date non parsable ({iso!r}) — à créer à la main"

        hhmm = start.strftime("%H.%M")
        title = f"{hhmm} {build_meeting_title(offer, who)}"
        description = build_meeting_description(
            email=reply.from_email,
            phone=classification.contact_phone,
            zoom_link=classification.zoom_link,
            website=(prospect.website if prospect else None),
            company=(prospect.company if prospect else None),
            notes=f"RDV pris automatiquement par le bot. Reply: {classification.key_phrase}",
        )
        if dry_run:
            return f"[DRY-RUN] créerait l'event '{title}' le {start.isoformat()}"
        try:
            calendar_client.create_event(
                title=title,
                start=start,
                duration_min=int(cal_cfg.get("meeting_duration_minutes", 30)),
                description=description,
                tz_name=cal_cfg.get("timezone", "Europe/Paris"),
            )
            return f"[EXEC] event créé : '{title}' le {start.isoformat()}"
        except Exception as e:
            return f"[RDV] échec création event ({e}) — à créer à la main"

    processed_count = 0
    skipped_count = 0
    error_count = 0
    mailinblack_pending: list[dict] = []

    with ManyReachClient() as mr, log_file.open("w", encoding="utf-8") as logf:
        for reply in mr.list_replies(
            campaign_id=campaign_id,
            since=since,
            confirmed_statuses=confirmed_statuses,
        ):
            if limit and processed_count >= limit:
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
                try:
                    prospect = mr.find_prospect_by_email(reply.from_email)
                except Exception as e:
                    print(f"  !! find_prospect_by_email failed: {e}")

                if prospect is not None:
                    try:
                        thread = mr.get_prospect_thread(prospect.prospect_id)
                        sent_msgs = [m for m in thread if m.type in ("Sent", "SentManual")]
                        if sent_msgs:
                            original_outreach = sent_msgs[0]
                    except Exception as e:
                        print(f"  !! get_prospect_thread failed: {e}")

                # Classify
                classification = classifier.classify(reply, original_outreach=original_outreach)
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

                # Compute concrete calendar slots for meeting-leading intents
                slot_objs, slot_strs = [], None
                if classification.intent in SLOT_INTENTS:
                    slot_objs, slot_strs = compute_slots(reply.from_email)
                    if slot_strs:
                        print(f"  SLOTS proposables : {', '.join(slot_strs)}")

                # Draft
                draft = drafter.draft(
                    reply=reply,
                    classification=classification,
                    original_outreach=original_outreach,
                    prospect=prospect,
                    style_guide=style_guide,
                    proposed_slots=slot_strs,
                    silent_on_not_interested=silent_on_not_interested,
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

                if plan.review_reason:
                    print(f"  REVIEW REASON: {plan.review_reason}")

                results = execute_plan(plan, reply, mr, dry_run=dry_run, tag_cache=tag_cache)
                for line in results:
                    print(f"    {line}")

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
                    alert_line = send_meeting_alert(
                        prospect_email=reply.from_email,
                        prospect_name=(prospect.first_name if prospect else None),
                        company=(prospect.company if prospect else None),
                        reply_snippet=_short(classification.key_phrase, 200),
                        proposed_when=(classification.confirmed_datetime or classification.key_phrase),
                        campaign_id=reply.campaign_id,
                        dry_run=dry_run,
                    )
                    print(f"    {alert_line}")

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

                if not dry_run:
                    append_processed_id(processed_file, reply.message_id)
                processed_count += 1

            except Exception as e:
                error_count += 1
                print(f"  ERROR processing reply: {e}")
                traceback.print_exc()
                log_entry["error"] = repr(e)
                logf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

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
