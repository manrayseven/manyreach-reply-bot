"""Tests de l'historique complet pour l'email de transfert (src/conversation.py).

Lance sans pytest :  python tests/test_conversation.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.conversation import build_history, quoted_message  # noqa: E402
from src.manyreach import Message  # noqa: E402


def _msg(mid, when, typ, body):
    return Message(
        message_id=mid, created_at=when, type=typ, campaign_id=111235, followup_id=0,
        from_email="x@y.fr", to_email="z@y.fr", subject="Re: ...", body=body,
    )


UTC = timezone.utc

# Cas école Major (espace Cmaclim) : le fil ne contient QUE les 2 réponses ; nos
# emails n'existent que cités dedans (entités HTML comme renvoyées par l'API).
REPLY_1 = _msg(
    "r1", datetime(2026, 9, 14, 4, 37, tzinfo=UTC), "Reply",
    "Bonjour, L'&eacute;cole compte 11 classes. Cordialement Virginie Montebello "
    "Le 2026-09-10 06:55, Romain Viard a &eacute;crit : Mesdames, Messieurs, "
    "Un contingent de 10 appareils est r&eacute;serv&eacute;.",
)
REPLY_2 = _msg(
    "r2", datetime(2026, 9, 15, 14, 1, tzinfo=UTC), "Reply",
    "<p>Bonjour</p><p>J'ai oubli&eacute; plusieurs salles.</p>"
    "<p>Le 2026-09-07 09:12, Romain Viard a &eacute;crit&nbsp;:</p>"
    "<blockquote>Mesdames, Messieurs, La mise &agrave; jour de votre DUERP. --</blockquote>",
)


def test_space_history_rebuilds_our_emails_from_quotes_in_order():
    h = build_history([REPLY_2, REPLY_1])
    assert [x["who"] for x in h] == ["Vous", "Vous", "Prospect", "Prospect"], h
    assert h[0]["when"] == "07/09 09:12" and "DUERP" in h[0]["text"], h[0]
    assert h[1]["when"] == "10/09 06:55" and "contingent" in h[1]["text"], h[1]
    assert "11 classes" in h[2]["text"] and "Mesdames" not in h[2]["text"], h[2]
    assert "oublié" in h[3]["text"] and "DUERP" not in h[3]["text"], h[3]
    assert not h[0]["text"].endswith("--"), h[0]


def test_current_reply_added_if_missing_from_thread():
    h = build_history([REPLY_1], current_reply=REPLY_2)
    assert any("oublié" in x["text"] for x in h), h


def test_main_account_uses_real_sent_messages_not_quotes():
    sent = _msg("s1", datetime(2026, 9, 1, 8, 0, tzinfo=UTC), "Sent",
                "Bonjour, je propose des audits flash.")
    dup = _msg("s1", datetime(2026, 9, 1, 8, 0, tzinfo=UTC), "SentManual",
               "Bonjour, je propose des audits flash.")
    reply = _msg("r9", datetime(2026, 9, 2, 8, 0, tzinfo=UTC), "Reply",
                 "Ça m'intéresse. Le 1 sept. 2026 à 10:00, Rudy a écrit : Bonjour, je propose")
    h = build_history([reply, dup, sent])
    assert [x["who"] for x in h] == ["Vous", "Prospect"], h
    assert h[1]["text"] == "Ça m'intéresse.", h


def test_quoted_message_french_date_header():
    text, dt = quoted_message("Oui. Le lun. 7 sept. 2026 à 09:12, Romain a écrit : Mesdames")
    assert text == "Mesdames" and dt and (dt.day, dt.month, dt.hour) == (7, 9, 9), (text, dt)


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
