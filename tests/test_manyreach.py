"""Tests du pré-filtre bounce/auto-reply (src/manyreach.py).

Capture les régressions du 16-17/06 :
- autoreply de voyage individuel ("currently travelling, back in the office") →
  doit être silencieux, pas une alerte objection_timing ;
- congé maternité avec CORPS VIDE (info uniquement dans le sujet) → silencieux ;
- un vrai refus / une fermeture saisonnière d'entreprise → PAS un bounce (doit
  atteindre le classifier).

Lance sans pytest :  python tests/test_manyreach.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.manyreach import Message, is_bounce_or_auto, is_mailinblack  # noqa: E402


def _msg(body="", subject="Re: Question", from_email="x@y.fr"):
    return Message(
        message_id="m1", created_at=datetime.now(timezone.utc), type="Reply",
        campaign_id="1", followup_id=None, from_email=from_email,
        to_email="r@x.fr", subject=subject, body=body,
    )


def test_ooo_travel_is_bounce():
    body = ("Dear, I am currently travelling, with uneven access to my email. "
            "I will be back in the office fully on Thursday June 18th.")
    assert is_bounce_or_auto(_msg(body=body)) is True


def test_maternity_subject_empty_body_is_bounce():
    # Corps vide, info uniquement dans le sujet (cas a.mocquet).
    assert is_bounce_or_auto(_msg(body="", subject="Congé maternite")) is True


def test_parental_and_maternity_variants():
    assert is_bounce_or_auto(_msg(subject="Maternity leave - back in Sept")) is True
    assert is_bounce_or_auto(_msg(subject="Absence congé parental")) is True


def test_real_refusal_is_not_bounce():
    body = "Bonjour, non je ne serais pas intéressé. Nous sommes actuellement fermés. Merci."
    assert is_bounce_or_auto(_msg(body=body)) is False


def test_dated_absence_autoreply_is_bounce():
    # "en séminaire/formation/déplacement/absent JUSQU'AU X" = autoreply daté
    # (cas benjamin.blanchard) → silencieux, surtout pas une alerte "plus tard".
    for body in (
        "Bonjour, Je suis en séminaire jusqu'au 17 Juillet inclus. Je reviens vers vous.",
        "Je suis absent jusqu'au 20 août.",
        "En formation jusqu'au 5 septembre.",
    ):
        assert is_bounce_or_auto(_msg(body=body)) is True, body


def test_real_timing_request_is_not_bounce():
    # Un vrai "recontactez-moi en septembre" doit atteindre le classifier.
    assert is_bounce_or_auto(_msg(body="Rappelez-moi en septembre, le sujet m'intéresse.")) is False


def test_closure_autoreply_dated_is_bounce():
    # Autoreply de fermeture datée + "nous répondrons à partir du..." (cas enault).
    body = ("Le bureau sera exceptionnellement fermé du 07/07 au 09/07 inclus. "
            "Nous répondrons à vos demandes à partir du 10/07.")
    assert is_bounce_or_auto(_msg(body=body)) is True


def test_seasonal_closure_is_not_bounce():
    # Fermeture saisonnière d'entreprise avec réouverture → doit atteindre le
    # classifier (objection_timing), donc PAS attrapée par le pré-filtre bounce.
    body = "Nous sommes fermés pour la saison, réouverture le 15 mars. Recontactez-nous."
    assert is_bounce_or_auto(_msg(body=body)) is False


def test_mailer_daemon_from_is_bounce():
    assert is_bounce_or_auto(_msg(from_email="mailer-daemon@x.com")) is True


def test_mailinblack_detection():
    assert is_mailinblack(_msg(from_email="noreply@invitations.mailinblack.com")) is True
    assert is_mailinblack(_msg(body="un clic pour délivrer votre email")) is True
    assert is_mailinblack(_msg(body="message normal")) is False


if __name__ == "__main__":
    from tests._runner import main
    main(dict(globals()))
