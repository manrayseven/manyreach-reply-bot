"""Signaux déterministes qui FORCENT une alerte (scripts/run_bot.py).

Cas réels du 24/09 (espace Cmaclim) : 3 bons leads, 1 seul remonté.
  - « Notre école compte 13 classes et l'espace administratif. Votre dotation ne
    couvre donc pas l'ensemble de nos besoins. » → classé « déjà équipé » →
    clôture polie envoyée à une école ENGAGÉE.
  - « Voici le contact mail du service en charge : gu.education@mairie-chambery.fr »
    → classé « mauvais interlocuteur » → réponse type, contact perdu.

Lance sans pytest :  python tests/test_lead_signals.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from run_bot import _DETAILS_RE, _OTHER_EMAIL_RE  # noqa: E402


def _emails(body: str, sender: str, ours: str) -> list[str]:
    our_dom = ours.split("@")[-1]
    return [e for e in _OTHER_EMAIL_RE.findall(body)
            if e.lower() not in (sender.lower(), ours.lower())
            and not e.lower().endswith("@" + our_dom)]


def test_details_detected_on_real_answers():
    for body in (
        "Bonjour, Notre école compte 13 classes et l'espace administratif.",
        "Mon école comporte 13 salles de classe, un bureau, une tisanerie",
        "Etage Unique pour toutes nos crèches, 1 section par structure",
        "Notre organisation regroupe plusieurs établissements dont voici les SIRET 84462254800023",
        "Nous avons 4 bâtiments sur 2 sites",
    ):
        assert _DETAILS_RE.search(body), body


def test_details_not_triggered_by_plain_refusals():
    for body in (
        "Non merci, pas intéressé.",
        "Bonne continuation, nous ne donnerons pas suite.",
        "Merci de me désinscrire.",
    ):
        assert not _DETAILS_RE.search(body), body


def test_contact_given_is_detected_without_our_own_addresses():
    body = ("Bonjour Romain, Voici le contact mail du service en charge de cette "
            "prestation pour nos locaux : gu.education@mairie-chambery.fr Cordialement")
    found = _emails(body, sender="coordo.evs.clef@gmail.com",
                    ours="romain.viard@cemaclim.com")
    assert found == ["gu.education@mairie-chambery.fr"], found


def test_own_signature_address_is_not_a_contact_lead():
    body = "Non merci. Caroline Bellec caroline@malittlecreche.fr"
    assert _emails(body, sender="caroline@malittlecreche.fr",
                   ours="romain.viard@cemaclim.com") == []
    # notre propre adresse citée dans le fil ne compte pas non plus
    body2 = "Merci. De : Romain Viard romain.viard@cemaclim.com"
    assert _emails(body2, sender="x@y.fr", ours="viard.r@cemaclim.com") == []


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
