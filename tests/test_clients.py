"""Tests du routage multi-clients (src/clients.py) — logique pure, sans I/O."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.clients import (  # noqa: E402
    build_client_draft_context,
    default_client,
    learn,
    needs_llm,
    route,
    route_by_known,
    slugify,
)


RUDY = {
    "id": "rudy",
    "name": "Rudy (moi)",
    "is_default": True,
    "mailboxes": ["rviard@hiwebmarketing.fr"],
    "campaigns": ["98002"],
}
DURAND = {
    "id": "durand",
    "name": "Client Durand",
    "mailboxes": ["contact@durand-agency.com"],
    "campaigns": ["55501"],
    "description": "Agence immobilière, vend des mandats",
    "offer_context": "Tu es Durand Immobilier. Pitch: estimation gratuite.",
    "signature": "L'équipe Durand",
}


def test_slugify():
    assert slugify("Client Durand & Fils") == "client-durand-fils"
    assert slugify("") == "client"


def test_route_by_mailbox_wins():
    c, reason = route_by_known([RUDY, DURAND], "contact@durand-agency.com", "98002")
    # mailbox Durand ET campagne Rudy en conflit → la MAILBOX prime.
    assert c["id"] == "durand"
    assert reason == "mailbox"


def test_route_by_campaign_fallback():
    c, reason = route_by_known([RUDY, DURAND], "inconnu@x.com", "55501")
    assert c["id"] == "durand"
    assert reason == "campaign"


def test_route_mailbox_case_insensitive():
    c, reason = route_by_known([RUDY], "RViard@HiWebMarketing.FR", None)
    assert c["id"] == "rudy" and reason == "mailbox"


def test_route_no_match_returns_none():
    c, reason = route_by_known([RUDY, DURAND], "x@y.com", "99999")
    assert c is None and reason == ""


def test_single_client_never_triages():
    # Un seul client → tout lui revient, jamais "à trier".
    r = route([RUDY], "inconnu@x.com", "00000")
    assert r["client_id"] == "rudy"
    assert r["needs_triage"] is False
    assert not needs_llm([RUDY])


def test_two_clients_unknown_signal_triages():
    r = route([RUDY, DURAND], "nouvelle-adresse@x.com", "77777")
    assert r["needs_triage"] is True
    assert r["client_id"] == "rudy"  # défaut proposé en attendant
    assert needs_llm([RUDY, DURAND])


def test_two_clients_known_mailbox_no_triage():
    r = route([RUDY, DURAND], "contact@durand-agency.com", None)
    assert r["client_id"] == "durand"
    assert r["needs_triage"] is False


def test_learn_adds_mailbox_and_campaign_without_mutating():
    c2 = learn(DURAND, "new-sender@durand.com", "60601")
    assert "new-sender@durand.com" in c2["mailboxes"]
    assert "60601" in c2["campaigns"]
    # original non muté
    assert "new-sender@durand.com" not in DURAND["mailboxes"]


def test_learn_dedupes():
    c2 = learn(DURAND, "CONTACT@durand-agency.com", "55501")
    assert c2["mailboxes"].count("contact@durand-agency.com") <= 1
    assert [x for x in c2["campaigns"]].count("55501") == 1


def test_default_client():
    assert default_client([DURAND, RUDY])["id"] == "rudy"  # is_default gagne
    assert default_client([DURAND])["id"] == "durand"  # sinon premier
    assert default_client([]) is None


def test_draft_context_empty_for_default_rudy():
    # Rudy par défaut sans offer_context → aucun bloc (comportement historique).
    assert build_client_draft_context(RUDY) == ""


def test_draft_context_present_for_other_client():
    ctx = build_client_draft_context(DURAND)
    assert "Durand" in ctx
    assert "estimation gratuite" in ctx
    assert "IDENTITÉ CLIENT" in ctx
