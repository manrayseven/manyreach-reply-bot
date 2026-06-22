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

from src.classifier import _strip_html, _trim_quoted_history  # noqa: E402


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
