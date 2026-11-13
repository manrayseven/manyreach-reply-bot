"""Revue du 13/11/2026 (1 427 réponses du 09 au 13/11) : automatiques qui
remontaient en alerte, refus nets non traités, et les deux modèles de refus.

Lance sans pytest :  python tests/test_revue_13_11.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fixed_replies import (  # noqa: E402
    WC_NEGATIVE_HTML,
    WC_NEGATIVE_MAPS_HTML,
    quick_intent,
    wc_negative_html,
)
from src.manyreach import Message, is_bounce_or_auto  # noqa: E402


def _msg(body: str, subject: str = "Re: Votre fiche Google") -> Message:
    return Message(
        message_id="x", campaign_id=110971, followup_id=0,
        from_email="p@x.fr", to_email="r@x.fr", subject=subject, body=body,
        created_at=datetime.now(timezone.utc), type="Reply",
    )


def test_automatiques_attrapes():
    for body, subject in (
        ("Nous accusons bonne réception de votre demande, elle sera traitée dans les meilleurs délais.", "Re: x"),
        ("Merci pour votre message, il est bien reçu. Nous faisons le maximum pour revenir vers vous.", "Re: x"),
        ("** THIS IS A WARNING MESSAGE ONLY ** YOU DO NOT NEED TO RESEND YOUR MESSAGE", "Warning: could not send message for past 4 hours"),
        ("Cette adresse e-mail n'est plus consultée depuis un certain temps.", "Re: x"),
        ("Je suis actuellement en déplacement à l'étranger.", "Re: x"),
        ("Suite à une blessure, je serai indisponible jusqu'au mois de décembre.", "Re: x"),
        # Le motif n'apparaît qu'après un long préambule HTML/CSS.
        ("<style>" + "p{margin:0}" * 120 + "</style><p>Les mails seront traités à notre retour.</p>", "Re: x"),
    ):
        assert is_bounce_or_auto(_msg(body, subject)), body[:60]


def test_humains_jamais_silencies():
    for body in (
        "Bonjour bien reçu Pouvez-vous me contacter au 0756110110 Merci",
        "J'accuse bonne réception de votre mail. Cependant je ne donnerai pas suite.",
        "Je pars en congé maternité d'ici peu, le sujet n'a pas avancé. Je garde vos coordonnées.",
        "Merci pour votre proposition mais j'ai décidé de ne plus avoir de site.",
        "A ok d'accord. Parce que ce n'est pas moi c'est la belle toiture bordelaise",
    ):
        assert not is_bounce_or_auto(_msg(body)), body[:60]


def test_refus_nets_sans_ia():
    for body in (
        "Bonjour Monsieur, Je vous remercie mais je ne suis pas intéressée. Bien cordialement, Justine",
        "Bonjour, Non je vous remercie beaucoup, Bonne journée,",
        "Bonjour, Je ne suis pas intéressée. Cordialement. Pauline Tel : 06 76 76 41 90 Site : https://www.x.fr/",
        "Merci pour votre gentillesse Mais je vais décliner Cordialement",
    ):
        assert quick_intent(body) == "not_interested_polite", body[:60]


def test_pistes_et_stop_restent_a_l_ia():
    for body in (
        "Ce que vous proposez est intéressant. Mais pas pour l'instant, je vous recontacterai peut-être.",
        "Oui pourquoi pas !",
        "Stop. Je ne suis pas intéressée par votre proposition.",
    ):
        assert quick_intent(body) != "not_interested_polite", body[:60]


def test_deux_modeles_de_refus():
    assert wc_negative_html(110971) is WC_NEGATIVE_MAPS_HTML      # Création de Site
    assert wc_negative_html(127319) is WC_NEGATIVE_MAPS_HTML      # sa jumelle
    assert wc_negative_html(110967, "mon site me va") is WC_NEGATIVE_HTML  # Google Maps
    assert wc_negative_html(110960, "j'ai déjà un site") is WC_NEGATIVE_MAPS_HTML
    assert wc_negative_html(110958, "pas intéressé") is WC_NEGATIVE_HTML
    assert "Google Maps" in WC_NEGATIVE_MAPS_HTML and "site-internet" not in WC_NEGATIVE_MAPS_HTML
    for html in (WC_NEGATIVE_HTML, WC_NEGATIVE_MAPS_HTML):
        assert "développements IA" in html and "growposter" in html and "growpulser" in html


if __name__ == "__main__":
    from tests._runner import run
    sys.exit(1 if run(globals()) else 0)
