# -*- coding: utf-8 -*-
"""« Ils attendent TA réponse » (Rudy 19/11).

Relevé du 19/11 : 45 fils Interested/MaybeLater dont le DERNIER message est une
réponse du prospect, vieille de 3 jours ou plus. 28 méritaient une réponse.

    le plus ancien — 86 jours
      « D'accord j'attends donc de vos nouvelles »
      (un audit lui avait été promis le 25 août)
    un autre — 8 jours
      « Pourriez-vous me renvoyer le premier apercu du site ? »

    (adresses volontairement absentes : ce dépôt est public)

Ce ne sont pas des relances ratées : c'est NOUS qui devons parler. Personne ne
le disait nulle part. Le moteur de relance croise pourtant ces fils à chaque
passage — il doit justement les écarter. Il les note désormais au passage, sans
une requête de plus.

Lance sans pytest :  python tests/test_attentes.py
"""
from __future__ import annotations

import html as _html
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

BUMPS = (ROOT / "scripts" / "run_bumps.py").read_text(encoding="utf-8")
IDX = (ROOT / "api" / "index.py").read_text(encoding="utf-8")


def _extrait():
    deb = BUMPS.index("_CITATION = re.compile(")
    fin = BUMPS.index("def main()", deb)
    ns = {"re": re, "_html": _html}
    exec(BUMPS[deb:fin], ns)  # noqa: S102 — code du dépôt
    return ns["_extrait_reponse"]


def test_l_extrait_coupe_la_citation_de_l_echange():
    """Sans ça, la liste affiche notre propre cold mail au lieu de ce que le
    prospect a répondu."""
    f = _extrait()
    assert f("<p>Bonjour,</p><p>Pourriez-vous me renvoyer l&#39;aper&ccedil;u ?</p>"
             "\nLe 12 nov. 2026, Rudy Viard a écrit :\n> blabla") == \
        "Bonjour, Pourriez-vous me renvoyer l'aperçu ?"
    assert f("ok merci\nEnvoyé de mon iPhone\n> citation") == "ok merci"
    assert f("<div>Je partage votre avis.</div>") == "Je partage votre avis."
    # Un message qui N'EST QUE de la citation : on garde le texte brut plutot
    # que d'afficher une ligne vide.
    assert f("\n> tout est cite ici").strip() != ""


def test_le_moteur_note_sans_depenser_un_appel_de_plus():
    """Il passe deja sur ces fils pour les ecarter. L'enregistrement se fait
    dans cette branche-la, pas dans un nouveau balayage."""
    i = BUMPS.index("LE PROSPECT A PARLE EN DERNIER")
    bloc = BUMPS[i:i + 1600]
    assert "kvstore.set_attente(" in bloc
    assert "ATTENTE_MIN_JOURS" in bloc
    # Rien en simulation.
    assert "not dry_run" in bloc


def test_l_attente_se_solde_quand_le_fil_bouge():
    """On a reparle : la ligne doit disparaitre toute seule, sinon la liste
    devient un cimetiere qu'on cesse de regarder."""
    assert BUMPS.count("kvstore.drop_attente(email)") >= 2
    i = BUMPS.index("On a reparle depuis")
    assert "drop_attente" in BUMPS[i:i + 300]


def test_le_stockage_trie_du_plus_ancien():
    from src import kvstore  # noqa: PLC0415

    base: dict = {}

    def _cmd(*a):
        if a[0] == "HSET":
            base[a[2]] = a[3]
            return 1
        if a[0] == "HGETALL":
            return dict(base)
        if a[0] == "HDEL":
            base.pop(a[2], None)
            return 1
        return None

    vrai, dispo = kvstore._cmd, kvstore.kv_available
    kvstore._cmd, kvstore.kv_available = _cmd, (lambda: True)
    try:
        kvstore.set_attente("Recent@X.FR", {"depuis": "2026-11-16T10:00:00+00:00", "jours": 3})
        kvstore.set_attente("vieux@x.fr", {"depuis": "2026-08-25T09:56:00+00:00", "jours": 86})
        a = kvstore.attentes()
        assert [x["email"] for x in a] == ["vieux@x.fr", "recent@x.fr"]
        kvstore.drop_attente("VIEUX@x.fr")
        assert len(kvstore.attentes()) == 1
        # L'extrait est borne : 300 fils x un pave = un KV illisible.
        kvstore.set_attente("l@x.fr", {"depuis": "2026-01-01", "extrait": "a" * 900})
        assert len([x for x in kvstore.attentes() if x["email"] == "l@x.fr"][0]["extrait"]) == 300
    finally:
        kvstore._cmd, kvstore.kv_available = vrai, dispo


def test_la_carte_existe_et_se_solde_d_un_clic():
    assert "Ils attendent TA réponse" in IDX
    assert 'value="drop_attente"' in IDX
    assert 'elif action == "drop_attente":' in IDX
    assert "attentes_card" in IDX
    # Le plus ancien saute aux yeux : rouge passe 14 jours.
    assert '"#d4493f" if _aj >= 14' in IDX


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
