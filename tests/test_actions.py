"""Tests du routage intent → action (src/actions.py).

Garantit que chaque intent tombe dans EXACTEMENT une catégorie (auto-envoi /
alerte / silencieux) et que les intents clés ne changent pas de camp par
mégarde (ex. not_interested_polite doit rester en AUTO, jamais en alerte).

Lance sans pytest :  python tests/test_actions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.actions import (  # noqa: E402
    ALERT_ONLY,
    ALWAYS_SILENT,
    AUTOSEND_ELIGIBLE,
    INTENT_PROSPECT_UPDATE,
)
from src.classifier import VALID_INTENTS  # noqa: E402


def test_categories_are_disjoint():
    assert AUTOSEND_ELIGIBLE.isdisjoint(ALERT_ONLY)
    assert AUTOSEND_ELIGIBLE.isdisjoint(ALWAYS_SILENT)
    assert ALERT_ONLY.isdisjoint(ALWAYS_SILENT)


def test_key_intents_route_as_expected():
    # Réponse AUTO : refus plat + objection PRIX (pitch GrowPulser pas cher +
    # essai gratuit, feedback Rudy 11/08).
    assert "not_interested_polite" in AUTOSEND_ELIGIBLE
    assert "objection_price" in AUTOSEND_ELIGIBLE
    # Leads / RDV / demandes d'info + objections À TRAVAILLER (déjà équipé,
    # argumentée, timing) → alerte (Rudy convainc lui-même).
    for i in ("interested_warm", "meeting_confirmed", "ask_more_info",
              "objection_timing",
              "objection_already_have_solution", "objection_reasoned"):
        assert i in ALERT_ONLY, i
    # objection_price N'est PLUS en alerte (retour en auto).
    assert "objection_price" not in ALERT_ONLY
    for i in ("objection_already_have_solution", "objection_reasoned"):
        assert i not in AUTOSEND_ELIGIBLE, i
    # Silencieux.
    for i in ("unsubscribe", "hostile", "bounce_or_auto"):
        assert i in ALWAYS_SILENT, i


def test_not_interested_maps_to_terminal_status():
    status, active, _tags = INTENT_PROSPECT_UPDATE["not_interested_polite"]
    assert status == "NotInterested" and active is False


def test_meeting_confirmed_never_auto_books():
    # Le bot ne doit JAMAIS poser MeetingBooked lui-même (Rudy cale les RDV à la
    # main ; un faux positif verrouillait le prospect). Signal RDV = lead chaud.
    status, active, _tags = INTENT_PROSPECT_UPDATE["meeting_confirmed"]
    assert status != "MeetingBooked", status
    assert status == "Interested" and active is True


def test_every_valid_intent_has_a_mapping():
    # Tout intent que le classifier peut produire doit avoir une action mappée
    # (sinon plan_actions le renvoie en needs_review).
    for intent in VALID_INTENTS:
        assert intent in INTENT_PROSPECT_UPDATE, f"intent sans mapping: {intent}"


if __name__ == "__main__":
    from tests._runner import main
    main(dict(globals()))
