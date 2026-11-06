"""Réponse auto seulement sur un refus SANS AMBIGUÏTÉ (Rudy 06/11).

Jusqu'ici le garde-fou anti-devinette ne protégeait que `not_interested_polite`.
Les deux objections « plates » — prix, déjà équipé — partaient en réponse
automatique sur la seule foi du classifieur. C'est ce qui a envoyé la clôture
polie à dita.madrid, qui écrivait chercher quelqu'un avec les compétences de
Rudy.

Rudy : « oui auto réponds sur les refus sans ambiguïté ». Les trois intents
d'auto-envoi doivent désormais TOUS montrer un refus explicite ; sinon, alerte.

Lance sans pytest :  python tests/test_refus_sans_ambiguite.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

RUN_BOT = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")


def _tuple_literal(nom: str) -> tuple[str, ...]:
    tree = ast.parse(RUN_BOT)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == nom
                and isinstance(node.value, ast.Tuple)):
            return tuple(e.value for e in node.value.elts
                         if isinstance(e, ast.Constant))
    raise AssertionError(f"{nom} introuvable")


def test_les_trois_intents_d_auto_envoi_exigent_un_refus_explicite():
    from run_bot import _AUTOSEND_NEEDS_CLEAR_NO  # noqa: PLC0415
    assert _AUTOSEND_NEEDS_CLEAR_NO == frozenset({
        "not_interested_polite", "objection_price",
        "objection_already_have_solution"})
    assert ("if classification.intent in _AUTOSEND_NEEDS_CLEAR_NO "
            "and not _digit_choice:") in RUN_BOT


def test_les_refus_plats_restent_auto_repondus():
    """Rudy 11/08 : les objections PLATES sont auto-gérées. L'inversion ne doit
    pas les transformer en alertes, sinon le dashboard devient illisible."""
    plats = _tuple_literal("_FLAT_REFUSAL_MARKERS")
    for msg in (
        "bonjour, nous avons déjà un prestataire pour cela",
        "on s'en occupe en interne merci",
        "c'est trop cher pour nous",
        "notre service communication gère ce sujet",
    ):
        assert any(m in msg for m in plats), msg


def test_un_besoin_exprime_avec_reserve_de_prix_nest_pas_un_refus_plat():
    """Le message de dita.madrid ne contient AUCUN marqueur de refus plat : il
    ne peut donc pas déclencher la réponse auto par ce chemin."""
    plats = _tuple_literal("_FLAT_REFUSAL_MARKERS")
    clairs = _tuple_literal("_CLEAR_NO_MARKERS")
    msg = ("merci pour votre proposition qui tombe à pic, car je suis à la "
           "recherche d'une personne avec vos compétence. mon soucis comme "
           "beaucoup est le manque de trésorerie pour honorer vos honoraires.")
    assert not any(m in msg for m in plats), "pris pour un refus plat"
    assert not any(m in msg for m in clairs), "pris pour un refus net"


def test_le_non_sec_passe_toujours():
    """L'inversion ne doit pas annuler le correctif du 22/10."""
    from src.fixed_replies import _is_bare_no  # noqa: PLC0415
    assert _is_bare_no("non")
    assert "_clear_no = _is_bare_no(_cl) or (" in RUN_BOT


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
