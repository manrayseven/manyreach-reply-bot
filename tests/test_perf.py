"""Tests du suivi de perf 30 jours du dashboard (_perf_30d dans api/index.py).

Vérifie : dédup par prospect, fenêtre 30 j, exclusion bounce/erreurs/système,
priorité meeting_confirmed, catégorisation pos/neg.

Lance sans pytest :  python tests/test_perf.py
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("dashboard_index", ROOT / "api" / "index.py")
_idx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_idx)
_perf_30d = _idx._perf_30d

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)


def _e(days_ago, frm, intent):
    return {"at": (NOW - timedelta(days=days_ago)).isoformat(), "from": frm, "intent": intent}


def _perf(acts):
    return _perf_30d(acts, now=NOW)


def test_dedup_by_prospect():
    p = _perf([_e(1, "a@x.fr", "not_interested_polite"), _e(2, "a@x.fr", "not_interested_polite")])
    assert p["received"] == 1 and p["negative"] == 1


def test_positive_and_negative_split():
    p = _perf([
        _e(1, "a@x.fr", "interested_warm"),
        _e(1, "b@x.fr", "ask_more_info"),
        _e(1, "c@x.fr", "not_interested_polite"),
        _e(1, "d@x.fr", "objection_already_have_solution"),
    ])
    assert p["positive"] == 2 and p["negative"] == 2 and p["received"] == 4


def test_meeting_takes_priority_and_counts_as_positive():
    # meeting_confirmed dans l'historique → prime sur un autre intent, compte en RDV + positif.
    p = _perf([_e(3, "c@x.fr", "meeting_confirmed"), _e(1, "c@x.fr", "interested_warm")])
    assert p["meetings"] == 1 and p["positive"] == 1 and p["received"] == 1


def test_neutral_counts_as_received_only():
    p = _perf([_e(1, "d@x.fr", "objection_timing"), _e(1, "e@x.fr", "wrong_person_redirect")])
    assert p["received"] == 2 and p["positive"] == 0 and p["negative"] == 0


def test_excludes_bounce_errors_system_and_old():
    p = _perf([
        _e(1, "f@x.fr", "bounce_or_auto"),   # auto-reply → pas une vraie réponse
        _e(1, "g@x.fr", "error"),            # système
        _e(1, "(manuel)", "run_now"),         # système
        _e(40, "h@x.fr", "interested_warm"), # hors fenêtre 30 j
    ])
    assert p == {"received": 0, "negative": 0, "positive": 0, "meetings": 0}


if __name__ == "__main__":
    from tests._runner import main
    main(dict(globals()))
