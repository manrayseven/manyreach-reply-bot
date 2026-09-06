"""Smoke test du rendu du dashboard (api/index.py).

Raison d'etre (incident 26/08/2026) : une edition avait laisse une variable
UTILISEE mais JAMAIS DEFINIE (_track_ho) dans _alert_row. `py_compile` passait
(la syntaxe etait valide) mais la page crashait en production avec
500 FUNCTION_INVOCATION_FAILED. Compiler ne suffit donc PAS : il faut rendre.

Ce test appelle _render() avec un kvstore simule contenant une alerte complete
(client + email de contact => passe par le bloc handoff/mise en relation, la
zone precise qui avait casse) et verifie qu'on obtient bien du HTML.

Lance sans pytest :  python tests/test_render_smoke.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))


def _fake_kv(monkey_actions):
    """kvstore minimal en memoire : aucune I/O reseau.

    __getattr__ renvoie un no-op pour tout ce qui n'est pas explicitement defini,
    afin que le test se concentre sur le RENDU (noms non definis, f-strings
    cassees) et non sur la surface exacte de kvstore.
    """
    class _KV:
        MAX_LOG_ENTRIES = 500

        def kv_available(self):
            return True

        def is_enabled(self):
            return True

        def recent_actions(self, limit=None):
            return list(monkey_actions)

        def get_clients(self):
            return [
                {"id": "cli1", "name": "Client Test", "is_default": True,
                 "contact_email": "contact@client-test.fr",
                 "sender_mailboxes": [], "campaigns": []},
                {"id": "cli2", "name": "Deuxieme Espace",
                 "contact_email": "contact@espace2.fr",
                 "sender_mailboxes": [], "campaigns": []},
                # Espace SANS aucune ligne : doit quand meme avoir son bloc,
                # sinon on ne sait pas s'il est calme ou casse.
                {"id": "cli3", "name": "Espace Silencieux",
                 "contact_email": "contact@espace3.fr",
                 "sender_mailboxes": [], "campaigns": []},
            ]

        def get_dismissed(self):
            return set()

        def get_dismissed_alerts(self):
            return set()

        def get_handoffs(self):
            return set()

        def get_triage_map(self):
            return {}

        def get_triage(self):
            return {}

        def cache_get(self, _k):
            return None

        def cache_set(self, *_a, **_k):
            return None

        def get_last_run(self):
            return datetime.now(timezone.utc).isoformat()

        def get_settings_overrides(self):
            return {}

        def __getattr__(self, _name):
            def _noop(*_a, **_k):
                return None
            return _noop

    return _KV()


def test_render_produces_html_with_a_full_alert():
    import index

    now = datetime.now(timezone.utc)
    actions = [
        {   # alerte complete -> declenche le bloc "Mettre en relation" + handoff
            "at": (now - timedelta(minutes=30)).isoformat(),
            "from": "prospect@exemple.fr",
            "prospect_email": "prospect@exemple.fr",
            "subject": "Re: Fiche Google",
            "intent": "interested_warm",
            "status": "🔔 ALERTE — à traiter dashboard",
            "reply": "Oui ça m'intéresse, rappelez-moi.",
            "response": "",
            "campaign_id": "12345",
            "client_id": "cli1",
            "client_name": "Client Test",
            "message_id": "m-1",
            "prospect_phone": "0600000000",
        },
        {   # un envoi auto -> alimente "Envois automatiques recents"
            "at": (now - timedelta(hours=2)).isoformat(),
            "from": "refus@exemple.fr",
            "subject": "Re: Question",
            "intent": "not_interested_polite",
            "status": "envoyé",
            "replied": True,
            "reply": "Non merci",
            "response": "Bonjour, c'est noté...",
            "campaign_id": "12345",
            "client_id": "cli1",
        },
        {   # 2e espace : doit apparaitre sous son propre sous-titre
            "at": (now - timedelta(minutes=45)).isoformat(),
            "from": "prospect2@exemple.fr",
            "prospect_email": "prospect2@exemple.fr",
            "subject": "Re: Question",
            "intent": "ask_more_info",
            "status": "🔔 ALERTE — à traiter dashboard",
            "reply": "Vous faites quoi exactement ?",
            "response": "",
            "campaign_id": "999",
            "client_id": "cli2",
            "client_name": "Deuxieme Espace",
            "message_id": "m-2",
        },
    ]
    fake = _fake_kv(actions)
    real_kv = index.kvstore
    index.kvstore = fake
    try:
        html_out = index._render()
    finally:
        index.kvstore = real_kv

    assert isinstance(html_out, str) and len(html_out) > 2000, len(html_out)
    assert "<html" in html_out.lower()
    # Les 3 tableaux attendus par Rudy
    assert "Alertes à traiter" in html_out
    assert "Envois automatiques récents" in html_out
    assert "Challenges antispam" in html_out
    # Le bloc transfert (zone qui avait casse) doit etre rendu
    assert "Email de transfert" in html_out
    assert "mark_handoff" in html_out
    # GROUPAGE PAR ESPACE : sans filtre et avec 2 comptes, chaque espace doit
    # avoir son sous-titre, mais tout reste sur la MEME page (demande Rudy 26/08).
    assert "space-hdr" in html_out
    assert "Client Test" in html_out
    assert "Deuxieme Espace" in html_out
    # Un espace sans aucune alerte ni envoi garde son bloc et le dit.
    assert "Espace Silencieux" in html_out
    assert "Aucune alerte pour cet espace." in html_out


if __name__ == "__main__":
    from tests._runner import main
    main(dict(globals()))
