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
    # Réponse AUTO : refus plat + objection prix + "déjà équipé" PLAT (feedback
    # Rudy 11/08). Ces objections ont une réponse standard efficace.
    for i in ("not_interested_polite", "objection_price",
              "objection_already_have_solution"):
        assert i in AUTOSEND_ELIGIBLE, i
    # Leads / RDV / demandes d'info / "plus tard" + objection ARGUMENTÉE → alerte.
    for i in ("interested_warm", "meeting_confirmed", "ask_more_info",
              "objection_timing", "objection_reasoned"):
        assert i in ALERT_ONLY, i
    # objection_price et already_have_solution NE sont PLUS en alerte.
    for i in ("objection_price", "objection_already_have_solution"):
        assert i not in ALERT_ONLY, i
    # SEULE l'objection argumentée reste en alerte parmi les objections.
    assert "objection_reasoned" in ALERT_ONLY
    assert "objection_reasoned" not in AUTOSEND_ELIGIBLE
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


def test_pas_besoin_is_a_clear_no_marker():
    # Feedback Rudy 12/08 : "nous n'avons pas besoin de vos service" et "Je n'ai
    # pas besoin. Merci." remontaient en ALERTE au lieu d'une réponse auto, parce
    # que le garde-fou anti-devinette de run_bot ne reconnaissait que "pas DE
    # besoin" (avec 'de'), pas "pas besoin" tout court. Ces refus plats "pas
    # besoin" DOIVENT matcher un marqueur de refus clair → réponse auto.
    from scripts.run_bot import _CLEAR_NO_MARKERS  # noqa: E402

    for body in (
        "Bonjour, nous n'avons pas besoin de vos service merci",
        "Bonjour, Je n'ai pas besoin. Merci beaucoup.",
        "pas besoin merci",
        # Feedback Rudy 13/08 : "Cela nous interesse pas" (verbe puis "pas", sans
        # "ne", sans accent) — l'ordre inversé n'était pas dans la whitelist.
        "Cela nous interesse pas, merci beaucoup!",
        "ça ne m'intéresse pas du tout",
    ):
        low = body.lower()
        assert any(m in low for m in _CLEAR_NO_MARKERS), body


def test_datetime_detector_flags_meeting_replies():
    # Feedback Rudy 18/08 : une réponse contenant une HEURE et/ou une DATE doit
    # être détectée comme signal RDV → alerte (jamais d'auto-réponse). Le détecteur
    # tourne sur le corps NETTOYÉ (réponse du prospect, sans la citation).
    from scripts.run_bot import _DATETIME_RE  # noqa: E402

    for body in (
        "OK pour mardi à 15h30",
        "Je peux me rendre disponible demain mardi à 15:30",
        "Oui, 14h me convient",
        "On se cale le 15/03 ?",
        "Parfait, le 3 septembre alors",
        "disponible jeudi",
        "Rappelez-moi à 9 heures",
    ):
        assert _DATETIME_RE.search(body), body

    # Négatifs : un refus/merci sans heure ni date ne matche pas.
    for body in (
        "Non merci, pas intéressé.",
        "Merci pour votre message, bonne continuation.",
        "Nous avons déjà un prestataire.",
    ):
        assert not _DATETIME_RE.search(body), body


def test_every_valid_intent_has_a_mapping():
    # Tout intent que le classifier peut produire doit avoir une action mappée
    # (sinon plan_actions le renvoie en needs_review).
    for intent in VALID_INTENTS:
        assert intent in INTENT_PROSPECT_UPDATE, f"intent sans mapping: {intent}"


def test_send_status_never_claims_a_send_that_did_not_happen():
    """Retour Rudy 26/08 : « tu as mis que tu avais repondu, en realite non ».

    Le statut etait calcule sur l'INTENTION (plan.auto_send) : un envoi bloque par
    un garde-fou anti-doublon etait quand meme logge "envoye" + macaron "Repondu"
    (cas nine.traiteur, aucun message reellement parti). Le statut doit desormais
    venir du RESULTAT REEL d'execute_plan.
    """
    from scripts.run_bot import send_status_from_results  # noqa: E402

    # Envoi reussi -> "envoye" + replied True
    st, ok = send_status_from_results(["[ENVOYE ok]".replace("ENVOYE", "ENVOYÉ")],
                                      auto_send=True)
    assert ok is True and st == "envoyé", (st, ok)

    # Bloque par l'anti-doublon MALGRE auto_send -> jamais "envoye"
    for blocker in ("[DÉJÀ RÉPONDU] m1 — skip", "[DUPLICATE-LOCK] envoi en cours"):
        st, ok = send_status_from_results([blocker], auto_send=True)
        assert ok is False, blocker
        assert "NON envoyé" in st, st

    # auto_send voulu mais aucun marqueur d'envoi -> on l'avoue
    st, ok = send_status_from_results(["[TAG] ajoute"], auto_send=True)
    assert ok is False and "NON envoyé" in st, (st, ok)

    # Garde hors fenetre -> statut dedie, pas d'envoi revendique
    st, ok = send_status_from_results([], send_held=True, auto_send=True)
    assert ok is False and "gardé" in st, (st, ok)

    # Dry-run -> jamais d'envoi revendique
    st, ok = send_status_from_results(["[DRY-RUN] ENVERRAIT ..."], auto_send=True, dry_run=True)
    assert ok is False, (st, ok)


if __name__ == "__main__":
    from tests._runner import main
    main(dict(globals()))
