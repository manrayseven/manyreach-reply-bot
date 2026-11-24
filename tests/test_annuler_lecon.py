# -*- coding: utf-8 -*-
"""Annuler une lecon cliquee par erreur (Rudy, 24/11).

    « J'ai clique par erreur sur "ce n'est pas une alerte", c'est bien une
      alerte. »

Ce clic fait deux degats, pas un : il masque l'alerte, ET il enseigne au
classifieur que ce genre de reponse est un refus. Le second est le plus
grave : une lecon fausse ferme ensuite de vraies pistes, en silence. Il
n'existait aucun moyen de la retirer.

Lance sans pytest :  python tests/test_annuler_lecon.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from src import kvstore  # noqa: E402

IDX = (ROOT / "api" / "index.py").read_text(encoding="utf-8")


def _faux():
    listes: dict = {}
    ensembles: dict = {}

    def _cmd(*a):
        op = a[0]
        if op == "LPUSH":
            listes.setdefault(a[1], []).insert(0, a[2]); return 1
        if op == "LTRIM":
            return "OK"
        if op == "LRANGE":
            return list(listes.get(a[1], []))
        if op == "LREM":
            avant = listes.get(a[1], [])
            listes[a[1]] = [x for x in avant if x != a[3]]
            return len(avant) - len(listes[a[1]])
        if op == "SADD":
            ensembles.setdefault(a[1], set()).add(a[2]); return 1
        if op == "SMEMBERS":
            return list(ensembles.get(a[1], set()))
        if op == "SREM":
            ensembles.get(a[1], set()).discard(a[2]); return 1
        return None

    return _cmd


def test_la_lecon_fausse_s_efface_et_seulement_elle():
    vrai, dispo = kvstore._cmd, kvstore.kv_available
    kvstore._cmd, kvstore.kv_available = _faux(), (lambda: True)
    try:
        kvstore.add_lecon("Oui ca m'interesse, appelez-moi", "not_interested_polite", "Piste@X.fr")
        kvstore.add_lecon("Non merci, pas pour nous", "not_interested_polite", "autre@y.fr")
        assert len(kvstore.lecons()) == 2
        assert kvstore.drop_lecons_de("piste@x.fr") == 1
        restantes = kvstore.lecons()
        assert [r["from"] for r in restantes] == ["autre@y.fr"]
        # Rien a effacer : zero, pas d'erreur.
        assert kvstore.drop_lecons_de("inconnu@z.fr") == 0
    finally:
        kvstore._cmd, kvstore.kv_available = vrai, dispo


def test_l_alerte_masquee_par_le_meme_clic_est_rouverte():
    vrai, dispo = kvstore._cmd, kvstore.kv_available
    kvstore._cmd, kvstore.kv_available = _faux(), (lambda: True)
    try:
        kvstore.dismiss_alert("2026-11-24T08:00:00+00:00|piste@x.fr")
        kvstore.dismiss_alert("2026-11-20T08:00:00+00:00|autre@y.fr")
        assert kvstore.restore_alerts_de("Piste@X.fr") == 1
        assert kvstore.get_dismissed_alerts() == {"2026-11-20T08:00:00+00:00|autre@y.fr"}
    finally:
        kvstore._cmd, kvstore.kv_available = vrai, dispo


def test_le_tableau_de_bord_liste_les_lecons_et_permet_d_annuler():
    assert "Leçons apprises" in IDX
    assert 'value="drop_lecon"' in IDX
    assert 'elif action == "drop_lecon":' in IDX
    i = IDX.index('elif action == "drop_lecon":')
    bloc = IDX[i:i + 900]
    # Les DEUX degats sont repares par le meme clic.
    assert "drop_lecons_de(" in bloc and "restore_alerts_de(" in bloc


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
