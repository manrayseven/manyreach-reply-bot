"""Un « Non » sec est un refus, pas une alerte (Rudy 22/10).

Cas antoine@victorie-music.com : le prospect répond « Non », rien d'autre. Le
bot l'a classé « demande d'infos » (tag bot:info-requested) et remonté en
ALERTE. Deux causes cumulées :

  1. aucun marqueur de _CLEAR_NO ne couvre un « non » isolé — la liste attend
     « non merci », « pas intéressé »… ;
  2. le garde-fou anti-devinette exige un message d'au moins 4 caractères avant
     d'accepter un refus. « Non » en fait 3.

Rudy : « quand vraiment le mec dit non, ne me fais pas remonter l'alerte, fais
juste le process automatique not interested et réponse auto ».

Lance sans pytest :  python tests/test_bare_no.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from src.fixed_replies import _is_bare_no, quick_intent  # noqa: E402


def test_le_non_sec_est_reconnu():
    for msg in ("non", "Non", "NON", "non.", "Non !", "non, merci",
                "Bonjour, non. Cordialement", "no", "nan",
                "Non. Bonne journée", "Non, désolé"):
        assert _is_bare_no(msg.lower().replace("’", "'")), msg


def test_un_non_noye_dans_une_phrase_n_est_pas_un_non_sec():
    """La règle ne doit mordre que sur le message RÉDUIT à un refus : partout
    ailleurs, c'est au classifieur de trancher."""
    for msg in (
        "non je ne travaille qu'en local",
        "nous n'avons pas de besoin pour le moment mais rappelez-moi en janvier",
        "non pour la refonte, oui pour la fiche google",
        "non ?",                      # une question reste une question
        "monsieur non seulement c'est non mais en plus vous insistez",
    ):
        assert not _is_bare_no(msg), msg


def test_quick_intent_traite_le_non_sec_sans_ia():
    """Pas d'appel au modèle pour ça : refus type, envoi direct."""
    assert quick_intent("Non") == "not_interested_polite"
    assert quick_intent("Non merci") == "not_interested_polite"
    # Un vrai STOP reste une désinscription, pas un refus poli.
    assert quick_intent("STOP") == "unsubscribe"


def test_le_garde_fou_anti_devinette_laisse_passer_le_non_sec():
    """Le seuil des 4 caractères ne doit plus renvoyer « Non » en alerte."""
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    assert "_clear_no = _is_bare_no(_cl) or (" in src
    # …sans supprimer le garde-fou pour autant : les messages vides ou ambigus
    # doivent continuer de remonter plutôt que de recevoir une réponse au pif.
    assert "_has_marker or _short_confident_no" in src
    assert "ALERTE (pas de réponse auto au pif)" in src


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
