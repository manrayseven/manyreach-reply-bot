"""Tests du pré-tri sans IA et des réponses types fixes (src/fixed_replies.py).

Lance sans pytest :  python tests/test_fixed_replies.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fixed_replies import (  # noqa: E402
    CMACLIM_NEGATIVE_HTML,
    WC_NEGATIVE_HTML,
    fixed_draft,
    quick_classification,
    quick_intent,
)


# --- quick_intent : cas évidents ------------------------------------------------

def test_quick_clear_no():
    for body in (
        "Non merci",
        "non merci, bonne journée",
        "Bonjour, pas intéressé. Cordialement",
        "Nous ne sommes pas intéressés.",
        "Ça ne m’intéresse pas, merci",  # apostrophe typographique
        "Pas besoin, merci.",
        "Nous ne donnerons pas suite. Bonne continuation",
    ):
        assert quick_intent(body) == "not_interested_polite", body


def test_quick_stop_is_unsubscribe():
    assert quick_intent("STOP") == "unsubscribe"
    assert quick_intent("Merci de me désinscrire") == "unsubscribe"
    assert quick_intent("ok", subject="STOP") == "unsubscribe"


# --- quick_intent : le moindre doute → IA ----------------------------------------

def test_quick_ambiguous_goes_to_ai():
    for body in (
        "Non merci, mais envoyez-moi quand même votre plaquette",
        "Pas intéressé pour l'instant, recontactez-moi l'an prochain",
        "Pas besoin, voyez plutôt avec mon collègue jean@exemple.fr",
        "Pas intéressé. C'est combien ?",
        "Non merci, nous avons déjà un prestataire",
        "Pas intéressé, arrêtez de nous relancer !!",
        "Oui pas de souci, pas besoin de rappeler",
        "Merci pour votre message",  # aucun refus net
        "",
    ):
        assert quick_intent(body) is None, body


def test_quick_long_message_goes_to_ai():
    assert quick_intent("Non merci. " + "blabla " * 60) is None


# --- fixed_draft ------------------------------------------------------------------

def _cls(intent="not_interested_polite", language="fr"):
    c = quick_classification(intent, "non merci")
    from dataclasses import replace
    return replace(c, language=language)


def test_fixed_draft_wc_default_and_mono_client():
    for client in (None, {"id": "rudy", "is_default": True}):
        d = fixed_draft(_cls(), client)
        assert d is not None and not d.skip_send
        assert d.body_html == WC_NEGATIVE_HTML
    assert "site-internet" in WC_NEGATIVE_HTML and "growpulser" in WC_NEGATIVE_HTML


def test_fixed_draft_cmaclim_client_and_space():
    d1 = fixed_draft(_cls("objection_price"), {"id": "cmaclim"})
    d2 = fixed_draft(_cls(), None, space_id="cmaclim")
    for d in (d1, d2):
        assert d is not None and d.body_html == CMACLIM_NEGATIVE_HTML
    assert "{{SENDER_SIGNATURE}}" in CMACLIM_NEGATIVE_HTML
    assert "growpulser" not in CMACLIM_NEGATIVE_HTML and "Rudy" not in CMACLIM_NEGATIVE_HTML


def test_fixed_draft_falls_back_to_ai():
    # autre client sans modèle
    assert fixed_draft(_cls(), {"id": "client-durand"}) is None
    # espace sans modèle : JAMAIS le modèle WC depuis l'espace d'un client
    assert fixed_draft(_cls(), None, space_id="autre-espace") is None
    # prospect non francophone
    assert fixed_draft(_cls(language="en"), None) is None
    # intents non négatifs
    for intent in ("interested_warm", "ask_more_info", "objection_timing", "unsubscribe"):
        assert fixed_draft(_cls(intent), None) is None, intent


def test_fixed_draft_wrong_person_gets_template():
    # « Ce n'est pas nous, adressez-vous à la mairie » → réponse type (Rudy 15/09).
    assert fixed_draft(_cls("wrong_person_redirect"), None).body_html == WC_NEGATIVE_HTML
    d = fixed_draft(_cls("wrong_person_redirect"), None, space_id="cmaclim")
    assert d.body_html == CMACLIM_NEGATIVE_HTML
    # le réglage « silencieux » des refus ne le concerne pas
    assert not fixed_draft(_cls("wrong_person_redirect"), None,
                           silent_on_not_interested=True).skip_send


def test_fixed_draft_silent_setting():
    d = fixed_draft(_cls(), None, silent_on_not_interested=True)
    assert d is not None and d.skip_send and d.body_html is None
    # le réglage « silencieux » ne concerne que not_interested_polite
    d2 = fixed_draft(_cls("objection_price"), None, silent_on_not_interested=True)
    assert d2 is not None and not d2.skip_send


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
