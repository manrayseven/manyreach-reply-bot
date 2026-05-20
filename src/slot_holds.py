"""Soft-hold store for proposed meeting slots.

When the bot proposes concrete slots to a prospect, we record them here with a
timestamp. Future slot proposals to OTHER prospects exclude these held slots so
the same time is never offered to two people at once.

Holds expire after `hold_days` (if the prospect never confirms, the slot frees
up again). When a meeting is confirmed, the slot becomes a real calendar event
(busy) and that prospect's other holds are cleared.

Stored as JSON at data/slot_holds.json (gitignored).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "data" / "slot_holds.json"


class SlotHoldStore:
    def __init__(self, path: Path | None = None, hold_days: int = 5):
        self.path = path or DEFAULT_PATH
        self.hold_days = hold_days
        self._holds: list[dict] = self._load()
        self._prune()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._holds, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _prune(self) -> None:
        """Drop holds older than hold_days and slots already in the past."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.hold_days)
        kept = []
        for h in self._holds:
            try:
                proposed_at = datetime.fromisoformat(h["proposed_at"])
                slot_start = datetime.fromisoformat(h["slot_start"])
            except (KeyError, ValueError):
                continue
            if proposed_at >= cutoff and slot_start >= now:
                kept.append(h)
        if len(kept) != len(self._holds):
            self._holds = kept
            self._save()

    def held_starts_for_others(self, prospect_email: str) -> set[datetime]:
        """Slot start datetimes currently held for OTHER prospects."""
        out: set[datetime] = set()
        for h in self._holds:
            if h.get("email", "").lower() == prospect_email.lower():
                continue
            try:
                out.add(datetime.fromisoformat(h["slot_start"]))
            except (KeyError, ValueError):
                continue
        return out

    def record(self, prospect_email: str, slot_starts: list[datetime]) -> None:
        """Record slots just proposed to a prospect (replaces their prior holds)."""
        # Drop this prospect's old holds first (we re-propose fresh each time)
        self._holds = [
            h for h in self._holds if h.get("email", "").lower() != prospect_email.lower()
        ]
        now_iso = datetime.now(timezone.utc).isoformat()
        for s in slot_starts:
            self._holds.append(
                {
                    "email": prospect_email,
                    "slot_start": s.isoformat(),
                    "proposed_at": now_iso,
                }
            )
        self._save()

    def clear(self, prospect_email: str) -> None:
        """Release all holds for a prospect (e.g. after they confirm or go cold)."""
        before = len(self._holds)
        self._holds = [
            h for h in self._holds if h.get("email", "").lower() != prospect_email.lower()
        ]
        if len(self._holds) != before:
            self._save()
