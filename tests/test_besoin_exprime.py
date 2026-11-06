"""Un besoin exprimé n'est jamais une clôture (Rudy 06/11).

Cas dita.madrid@gmail.com, campagne « Votre page d'accueil, en maquette » :

    « Bonjour et merci pour votre proposition
      Qui tombe à pic, car je suis à la recherche d'une personne avec vos
      compétence.
      - pour monter en première page Google
      - créer une vitrine sur un nouveau projet de : Centre hypnose /laser
      Mon soucis comme beaucoup est le manque de trésorerie pour honorer vos
      honoraires. C'est donc une question essentielle à une future
      collaboration. »

Le bot lui a envoyé la clôture polie — « C'est noté, je ne vous dérange pas
plus ». Aucun marqueur d'opportunité ne couvrait « à la recherche », « tombe à
pic » ni « vos compétences » : le classifieur n'a retenu que la réserve de
trésorerie, donc une objection de prix, auto-répondue depuis le 11/08.

Lance sans pytest :  python tests/test_besoin_exprime.py
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

MESSAGE = (
    "bonjour et merci pour votre proposition qui tombe à pic, car je suis à la "
    "recherche d'une personne avec vos compétence. - pour monter en première "
    "page google - créer une vitrine sur un nouveau projet de : centre hypnose "
    "/laser (centre dkm), sans perdre les avis google. mon soucis comme "
    "beaucoup est le manque de trésorerie pour honorer vos honoraires. c'est "
    "donc une question essentielle à une future collaboration."
)


def _tuple_literal(nom: str) -> tuple[str, ...]:
    """Lit un tuple de marqueurs dans run_bot.py sans exécuter le module."""
    tree = ast.parse(RUN_BOT)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == nom
                and isinstance(node.value, ast.Tuple)):
            return tuple(e.value for e in node.value.elts
                         if isinstance(e, ast.Constant))
    raise AssertionError(f"{nom} introuvable dans run_bot.py")


def test_le_message_de_dita_declenche_une_alerte():
    signaux = _tuple_literal("_interest_signals") + _tuple_literal("_info_signals")
    touches = [s for s in signaux if s in MESSAGE]
    assert touches, "aucun signal d'opportunité : le message repartirait en clôture"
    # Les trois formulations qui portent le besoin doivent être couvertes.
    for attendu in ("à la recherche", "tombe à pic", "vos compétences"):
        assert attendu in signaux, attendu


def test_un_refus_de_prix_plat_reste_auto_repondu():
    """Rudy 11/08 : les objections de prix PLATES sont auto-gérées. « budget »
    et « trésorerie » ne doivent donc pas devenir des signaux d'opportunité,
    sinon chaque « je n'ai pas le budget » remonterait en alerte."""
    signaux = _tuple_literal("_interest_signals") + _tuple_literal("_info_signals")
    assert "budget" not in signaux
    assert "trésorerie" not in signaux and "tresorerie" not in signaux


def test_le_garde_fou_refus_reste_prioritaire():
    """« Je n'ai pas besoin de vos compétences » contient un marqueur d'intérêt
    ET un marqueur de refus : le refus doit l'emporter, sinon on transforme des
    refus nets en alertes."""
    refus = _tuple_literal("_refusal_markers")
    msg = "je n'ai pas besoin de vos compétences, merci"
    assert any(r in msg for r in refus), "le refus n'est plus reconnu"
    # La condition d'override doit continuer d'exiger l'absence de refus.
    assert "if _forced and _overridable and (not _has_refusal" in RUN_BOT


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
