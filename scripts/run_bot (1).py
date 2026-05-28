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

from src.actions import execute_plan, plan_actions  # noqa: E402
from src.classifier import Classifier  # noqa: E402
from src.drafter import Drafter  # noqa: E402
from src.manyreach import ManyReachClient, is_bounce_or_auto  # noqa: E402


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

    print(f"\n=== ManyReach Reply Bot — {'DRY-RUN' if dry_run else 'LIVE'} mode ===")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Logs: {log_file}")
    print(f"Since: {since.isoformat()}  Limit: {limit}  Campaign filter: {campaign_id}")
    print(f"Already-processed message IDs: {len(processed_ids)}")
    print()

    classifier = Classifier()
    drafter = Drafter()
    tag_cache: dict[str, int] = {}

    processed_count = 0
    skipped_count = 0
    error_count = 0

    with ManyReachClient() as mr, log_file.open("w", encoding="utf-8") as logf:
        for reply in mr.list_replies(campaign_id=campaign_id, since=since):
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

                # Draft
                draft = drafter.draft(
                    reply=reply,
                    classification=classification,
                    original_outreach=original_outreach,
                    prospect=prospect,
                    style_guide=style_guide,
                    proposed_slots=None,
                    silent_on_not_interested=silent_on_not_interested,
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

                if plan.review_reason:
                    print(f"  REVIEW REASON: {plan.review_reason}")

                results = execute_plan(plan, reply, mr, dry_run=dry_run, tag_cache=tag_cache)
                for line in results:
                    print(f"    {line}")

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
    print(f"Full log: {log_file}")
    if dry_run:
        print("Mode: DRY-RUN — aucune écriture vers ManyReach. Relance avec --no-dry-run pour exécuter.")
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
