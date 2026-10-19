"""Aucune réponse auto hors Webmarketing Conseil (Rudy 19/10).

Olivier Poizeau, prospect de l'espace LAERIS, a reçu le modèle de Webmarketing
Conseil : growpulser, growposter, refonte de site, et « Fondateur Webmarketing
Conseil » en signature. Le garde-fou des modèles FIXES fonctionnait bien (il
renvoie None pour un espace sans modèle) — mais le relais passait alors à l'IA,
qui a ressorti le modèle maison malgré la consigne de périmètre du prompt.

Rudy : « je ne veux pas promouvoir growpulser, growposter etc sur les autres
espaces ». La règle n'est donc plus confiée au jugement du modèle : hors
Webmarketing Conseil, le bot n'envoie RIEN. Les pistes remontent toujours en
alerte, les bounces gardent leur traitement, les négatifs deviennent silencieux.

Lance sans pytest :  python tests/test_space_no_autosend.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from src.fixed_replies import _template_key  # noqa: E402


def test_aucun_espace_ne_recoit_le_modele_webmarketing_conseil():
    """Le modèle « wc » n'est atteignable que hors espace et pour le compte
    par défaut. Un espace, quel qu'il soit, ne peut jamais y accéder."""
    for space in ("laeris", "cmaclim", "LAERIS", "un-nouveau-client"):
        assert _template_key(None, space) != "wc", space
        assert _template_key({"id": "rudy", "is_default": True}, space) != "wc", space
    # Un client non-défaut du compte principal non plus.
    assert _template_key({"id": "laeris"}, None) != "wc"
    # Seul le compte par défaut (ou l'absence de client) y a droit.
    assert _template_key(None, None) == "wc"
    assert _template_key({"id": "rudy", "is_default": True}, None) == "wc"


def test_un_espace_sans_modele_ne_retombe_pas_sur_un_autre():
    """Pas de modèle pour cet espace → None (et surtout pas celui d'un voisin)."""
    assert _template_key(None, "laeris") is None
    assert _template_key(None, "espace-inconnu") is None


def test_le_garde_fou_deterministe_existe_dans_la_boucle():
    """Le relais IA ne doit plus pouvoir contourner la règle : c'est du code,
    plus une consigne de prompt."""
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    i = src.index("RÉPONSES AUTO : WEBMARKETING CONSEIL UNIQUEMENT")
    bloc = src[i:i + 1800]
    assert "_is_wc" in bloc
    # Les pistes doivent continuer de remonter…
    assert "classification.intent not in ALERT_ONLY" in bloc
    # …et les bounces garder leur propre branche (sinon un journal noyé : 94 %
    # des entrants de Laeris sont des avis de non-remise).
    assert "classification.intent not in ALWAYS_SILENT" in bloc
    # Le garde-fou doit précéder la rédaction (économise aussi les tokens).
    assert i < src.index("draft = fixed_draft(")


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
