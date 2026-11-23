# -*- coding: utf-8 -*-
"""La liste « Ils attendent TA réponse » a été supprimée (Rudy, 23/11).

    « Je ne vois pas à quoi ça sert sur le dashboard.
      Soit le contact doit être relancé et tu le fais, soit c'est terminé
      ("meeting completed" sans envoi de proposition ou "not interested") et
      dans ce cas pas de relance. »

Il avait raison, et sa capture le prouvait : sur les 33 lignes, un rapport de
non-remise OVH, deux répondeurs d'absence, un « merci pour la mise en contact »
et deux confirmations de rendez-vous. Aucune n'attendait quoi que ce soit de
lui, et les deux vraies pistes — des rendez-vous jamais honorés, vieux de 36 et
49 jours — étaient noyées dedans.

La leçon : une troisième pile à trier à la main n'est pas une solution, c'est
le problème déplacé. Le système tranche, ou il se tait.

Lance sans pytest :  python tests/test_attentes.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

BUMPS = (ROOT / "scripts" / "run_bumps.py").read_text(encoding="utf-8")
IDX = (ROOT / "api" / "index.py").read_text(encoding="utf-8")


def test_la_carte_n_est_plus_rendue():
    # Le commentaire qui explique le retrait garde le nom ; c'est le RENDU qui
    # doit avoir disparu, pas la trace de la decision.
    assert "<h2>⏳ Ils attendent" not in IDX
    assert 'attentes_card = ""' in IDX
    assert "kvstore.attentes()" not in IDX


def test_plus_rien_n_alimente_la_liste():
    assert "kvstore.set_attente(" not in BUMPS
    # On vide ce qui s'y trouvait, au lieu de laisser des restes en base.
    assert "kvstore.drop_attente(email)" in BUMPS


def test_le_magasin_reste_pour_purger():
    """`drop_attente` sert encore a vider les entrees ecrites avant le 23/11."""
    from src import kvstore  # noqa: PLC0415
    assert hasattr(kvstore, "drop_attente")


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
