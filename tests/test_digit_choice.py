"""Réponses « 1 / 2 / 3 » aux cold mails à options (scripts/run_bot.py).

Rudy 29/09 : « 1 » (= pas intéressé) doit recevoir la réponse type des négatifs,
plus le silence + blacklist d'`unsubscribe`. Et un corps réduit à « 1 » ne doit
plus partir en alerte « message illisible » (il fait 1 caractère).

Lance sans pytest :  python tests/test_digit_choice.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from run_bot import _DIGIT_CHOICE_RE, _OPTIONS_CONVENTION_RE  # noqa: E402

COLD_MAIL = (
    "Bonjour, dernier email de ma part sur ce sujet. "
    "1. La prospection par email n'est pas un sujet pour vous : aucun souci. "
    "2. C'est un sujet mais pas prioritaire : répondez \"plus tard\". "
    "Répondez simplement 1, 2 ou 3."
)


def test_digit_replies_are_recognised():
    for body, expected in (("1", "1"), ("1.", "1"), ("2", "2"), ("3 !", "3"),
                           ("Réponse 2", "2"), ("réponse : 3", "3")):
        m = _DIGIT_CHOICE_RE.match(body)
        assert m and m.group(1) == expected, body


def test_other_bodies_are_not_digit_replies():
    for body in ("1 classe", "Non merci", "10", "4", "1 et 2", ""):
        assert not _DIGIT_CHOICE_RE.match(body), body


def test_convention_detected_in_cold_mail():
    assert _OPTIONS_CONVENTION_RE.search(COLD_MAIL)
    assert _OPTIONS_CONVENTION_RE.search("Répondez 1 = pas intéressé, 2 = plus tard")
    # Un cold mail SANS convention : un « 1 » isolé ne doit rien déclencher.
    assert not _OPTIONS_CONVENTION_RE.search(
        "Bonjour, je propose des audits flash de 15 minutes. Pouvons-nous en parler ?"
    )


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
