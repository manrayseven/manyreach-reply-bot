"""Tests des fonctions pures de nettoyage du classifier.

Capture les régressions du 17-18/06 : un refus COURT suivi d'une citation Gmail
("Le ... a écrit : <pitch>") n'était pas coupé → le classifier lisait le pitch
cité et classait à tort meeting_confirmed (faux MeetingBooked + alerte).

Lance sans pytest :  python tests/test_classifier.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.classifier import _strip_html, _trim_quoted_history, is_stop_signal  # noqa: E402


def test_trim_short_refusal_with_gmail_quote():
    # Le bug clé : refus court + "Le <date> ... a écrit :" + pitch cité.
    raw = (
        "Non, votre offre ne m'intéresse pas. Bonne journée "
        "Le mer. 17 juin 2026 à 11:05, Rudy Viard <r@x.fr> a écrit : "
        "Bonjour, je propose des audits flash de 15 minutes gratuits..."
    )
    out = _trim_quoted_history(raw)
    assert out == "Non, votre offre ne m'intéresse pas. Bonne journée", out
    assert "audits flash" not in out


def test_trim_english_wrote_quote():
    raw = "No thanks, not interested. On Mon, Jun 15, 2026 at 4:28 PM Rudy wrote: Bonjour, free audits"
    out = _trim_quoted_history(raw)
    assert out == "No thanks, not interested.", out


def test_trim_preserves_message_starting_with_Le_no_date():
    # Pas de date après "Le" → ce n'est PAS un en-tête de citation → on préserve.
    raw = "Le service que vous proposez ne nous intéresse pas du tout. Bonne continuation"
    out = _trim_quoted_history(raw)
    assert out == raw, out


def test_trim_preserves_real_meeting_line():
    # Un vrai RDV doit survivre au trim (la citation/pitch après est coupée).
    raw = "Oui parfait, mardi 18 à 14h ça me va. Le 16 juin 2026 à 09:00, Rudy a écrit : blabla pitch"
    out = _trim_quoted_history(raw)
    assert out == "Oui parfait, mardi 18 à 14h ça me va.", out


def test_trim_outlook_separator():
    raw = "Pas intéressé, merci. -----Original Message----- From: Rudy ... pitch"
    out = _trim_quoted_history(raw)
    assert out.startswith("Pas intéressé, merci."), out
    assert "pitch" not in out


def test_trim_outlook_fr_header_early():
    # Refus court + citation Outlook FR "De : ... Envoyé : ... Objet : ..." tôt dans
    # le texte (cas 3gelec) : sans coupe, l'override lit le pitch cité (créneau/appel)
    # et bascule en meeting_confirmed.
    raw = ("Bonjour, Ne nous somme pas intéressé. Merci. De : Rudy Viard "
           "<r@x.fr> Envoyé : jeudi 2 juillet 2026 10:24 À : c@3gelec.fr "
           "Objet : RE: Fiche Google Bonjour, audits flash, on peut caler un créneau par téléphone")
    out = _trim_quoted_history(raw)
    assert out == "Bonjour, Ne nous somme pas intéressé. Merci.", out
    assert "créneau" not in out and "audits flash" not in out


def test_trim_outlook_german_header():
    raw = ("Bonjour, en ce moment pas un sujet pour nous. Merci. Von: Rudy Viard "
           "[mailto:x] Gesendet: Dienstag An: David Betreff: RE: Question Bonjour, appel créneau")
    out = _trim_quoted_history(raw)
    assert out == "Bonjour, en ce moment pas un sujet pour nous. Merci.", out


def test_trim_fr_message_dorigine_separator():
    # Refus court + citation FR "------ Message d'origine ------" (cas remialgis) :
    # sans coupe, l'override lit "disponible pour un appel" cité → faux meeting.
    raw = ("Bonjour, je ne suis pas intéressé. Merci et bel été. "
           "------ Message d'origine ------ De \"Rudy Viard\" <r@x.fr> À "
           "contact@remialgis.com Date 17/07/2026 Objet RE: Fiche Google "
           "Seriez-vous disponible pour un appel de 15 minutes ?")
    out = _trim_quoted_history(raw)
    assert out == "Bonjour, je ne suis pas intéressé. Merci et bel été.", out
    assert "appel" not in out and "disponible" not in out


def test_is_stop_signal_positive():
    assert is_stop_signal("stop")
    assert is_stop_signal("STOP.")
    assert is_stop_signal("Stop merci")
    assert is_stop_signal("", subject="STOP")
    assert is_stop_signal("Merci de me désinscrire de votre liste.")
    assert is_stop_signal("Ne plus me contacter svp.")
    assert is_stop_signal("Please unsubscribe me")
    assert is_stop_signal("retirez-moi de votre base")
    # Message qui COMMENCE par "stop" (macro "dites stop si..."), même long.
    assert is_stop_signal("Stop, je ne suis pas concerné par ce service, merci beaucoup.")
    assert is_stop_signal("STOP - merci de ne rien envoyer")
    # Plainte spam → désinscription (cas lescanonniers).
    assert is_stop_signal("No spam")
    assert is_stop_signal("stop spam")
    assert is_stop_signal("C'est du spam, merci d'arrêter")
    assert is_stop_signal("Pourriel")


def test_is_stop_signal_negative():
    # Un refus poli n'est PAS un stop (il ne blackliste pas).
    assert not is_stop_signal("Non merci, pas intéressé.")
    assert not is_stop_signal("Bonjour, c'est parfait, on continue.")
    # "stop" noyé dans une longue phrase ≠ demande d'arrêt.
    assert not is_stop_signal(
        "Je ne veux pas de stop and go dans notre stratégie, mais votre offre "
        "pourrait m'intéresser, pouvez-vous m'en dire plus ?"
    )


def test_strip_html_basic():
    html = "<div>Bonjour,</div><div><br></div><div>Je ne suis pas int&#233;ress&#233;e.</div>"
    out = _strip_html(html)
    assert "<div>" not in out and "<br>" not in out
    assert "Je ne suis pas" in out


def test_strip_html_removes_script_style():
    html = "<style>.x{color:red}</style>Texte<script>alert(1)</script> visible"
    out = _strip_html(html)
    assert "color:red" not in out and "alert(1)" not in out
    assert "Texte" in out and "visible" in out


if __name__ == "__main__":
    from tests._runner import main
    main(dict(globals()))
