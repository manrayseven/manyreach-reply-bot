"""Une alerte et une réponse auto sont EXCLUSIVES (Rudy 13/10).

Cas bieresicsas@gmail.com : un seul reply (« Je ne travaille qu'en local, et
j'ai déjà de nombreuses demandes à ce sujet »), un seul envoi — et pourtant le
prospect portait le statut NotInterested ET le tag bot:info-requested. Deux
intentions pour la même réponse : une passe l'a mise en alerte, l'autre lui a
envoyé le modèle de refus. Rudy : « ils n'auraient pas dû être répondus s'ils
sont en alerte ».

Deux verrous sont testés ici :
  1. `_OPEN_DOOR_RE` — le statut « Interested » de ManyReach ne suffit plus à
     créer une alerte par-dessus une réponse auto ; il faut que le prospect
     laisse une porte ouverte.
  2. la mémoire d'alerte en KV (`mark_alerted` / `was_alerted`), qui ne peut pas
     être en retard d'une passe contrairement aux tags du prospect.

Lance sans pytest :  python tests/test_alert_lock.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from run_bot import _OPEN_DOOR_RE  # noqa: E402


def test_refus_plat_ne_declenche_pas_d_alerte():
    """Le cas qui a déclenché le correctif, plus deux refus voisins."""
    assert not _OPEN_DOOR_RE.search(
        "Bonjour, Je ne travaille qu'en local, et j'ai déjà de nombreuses "
        "demandes à ce sujet.. Merci. Bonne journée.")
    assert not _OPEN_DOOR_RE.search(
        "Bonjour, nous ne sommes pas intéressés. Bonne continuation.")
    assert not _OPEN_DOOR_RE.search("Merci mais nous avons déjà un prestataire.")


def test_contre_proposition_declenche_une_alerte():
    """CSEFORMA (08/10) : refus de l'offre MAIS ouverture explicite — c'est le
    cas que le garde-fou « Interested » doit continuer d'attraper."""
    assert _OPEN_DOOR_RE.search(
        "CSEFORMA privilégie un modèle d'apport d'affaires. Si ce modèle peut "
        "vous convenir, je serais ravi d'échanger avec vous afin d'en définir "
        "les modalités.")
    assert _OPEN_DOOR_RE.search(
        "Est-ce que nous pouvons échanger sur votre démarche ?")
    assert _OPEN_DOOR_RE.search("Pouvez-vous me donner vos tarifs")
    assert _OPEN_DOOR_RE.search("Je suis intéressée par votre audit flash")


def test_le_verrou_d_alerte_est_bien_cable():
    """Analyse statique : les trois points qui créent une alerte doivent poser
    la marque, et la boucle doit la relire avant toute réponse auto."""
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    assert src.count("kvstore.mark_alerted(") >= 3, "une alerte ne pose plus le verrou"
    assert "kvstore.was_alerted(" in src, "le verrou n'est plus relu"
    # La relecture doit précéder les raccourcis (sinon un refus net part avant).
    i_lock = src.index("kvstore.was_alerted(")
    i_shortcut = src.index("=== RACCOURCI 1bis")
    assert i_lock < i_shortcut, "le verrou doit être testé AVANT les raccourcis"

    kv = (ROOT / "src" / "kvstore.py").read_text(encoding="utf-8")
    assert "def mark_alerted(" in kv and "def was_alerted(" in kv


def test_interested_manyreach_ne_suffit_plus_seul():
    """Le garde-fou « Interested » doit exiger la porte ouverte."""
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    i = src.index('_status_mids.get("Interested"')
    fenetre = src[i:i + 400]
    assert "_OPEN_DOOR_RE" in fenetre, "le statut ManyReach suffit encore à alerter"


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
