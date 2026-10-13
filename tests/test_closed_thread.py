"""Un fil clôturé reste clôturé (Rudy 13/10, cas Coshéa Intérieurs).

Déroulé réel, 48 minutes :
  16h32  elle : « pas intéressée, bonne continuation »
  17h01  bot  : modèle de clôture — « auriez-vous en tête des contacts ? »  ✅
  17h07  elle : répond gentiment À CETTE QUESTION (« les personnes que je
         connais font appel à Mathieu Hagel… elles ont déjà quelqu'un »)
  17h25  bot  : LE MÊME MODÈLE, mot pour mot                                 ❌
         + alerte « mauvaise personne » parce qu'elle cite des noms          ❌

Le cap anti-ping-pong (PROSPECT_REPLY_CAP=2) ne mordait qu'au 3e envoi. Une fois
le modèle de clôture parti, le fil est terminé : plus d'envoi, plus d'alerte.

Lance sans pytest :  python tests/test_closed_thread.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from run_bot import _closure_already_sent, _OPEN_DOOR_RE  # noqa: E402

WC_CLOSURE = (
    "<p>Bonjour,</p><p>C'est noté, merci d'avoir pris le temps de répondre, je ne "
    "vous dérange pas plus. Auriez-vous en tête un ou des contacts qui rencontrent "
    "ces problématiques ?</p>"
)
CMACLIM_CLOSURE = (
    "<p>Bonjour,</p><p>C'est noté, merci d'avoir pris le temps de nous répondre, "
    "nous ne vous dérangeons pas plus.</p>"
)
COLD_MAIL = "<p>Bonjour, je viens de consulter votre fiche Google Maps…</p>"


@dataclass
class M:
    type: str
    body: str
    created_at: datetime
    message_id: str = "x"


def _t(h, m=0):
    return datetime(2026, 10, 13, h, m, tzinfo=timezone.utc)


def test_modele_de_cloture_detecte_dans_le_fil():
    thread = [
        M("Sent", COLD_MAIL, _t(8)),
        M("Reply", "<p>pas intéressée</p>", _t(14, 32)),
        M("SentManual", WC_CLOSURE, _t(15, 1)),
    ]
    assert _closure_already_sent(thread, _t(15, 7)) is True
    # Variante Cmaclim (« de nous répondre »).
    assert _closure_already_sent(
        [M("Sent", CMACLIM_CLOSURE, _t(15, 1))], _t(15, 7)) is True


def test_pas_de_cloture_dans_un_fil_neuf():
    thread = [
        M("Sent", COLD_MAIL, _t(8)),
        M("Reply", "<p>pas intéressée</p>", _t(14, 32)),
    ]
    assert _closure_already_sent(thread, _t(14, 40)) is False
    assert _closure_already_sent([], _t(14, 40)) is False
    assert _closure_already_sent(None, _t(14, 40)) is False


def test_une_cloture_posterieure_ne_compte_pas():
    """Seuls les envois ANTÉRIEURS à la réponse traitée ferment le fil —
    sinon on classerait « déjà clôturé » le reply qui a provoqué la clôture."""
    thread = [
        M("Sent", COLD_MAIL, _t(8)),
        M("SentManual", WC_CLOSURE, _t(15, 1)),
    ]
    assert _closure_already_sent(thread, _t(14, 32)) is False


def test_un_reply_du_prospect_contenant_le_texte_ne_ferme_pas_le_fil():
    """Le prospect qui cite notre modèle (réponse avec historique) ne doit pas
    être pris pour un envoi de notre part."""
    thread = [M("Reply", "<blockquote>" + WC_CLOSURE + "</blockquote>", _t(15, 7))]
    assert _closure_already_sent(thread, _t(15, 30)) is False


def test_la_politesse_de_cloture_nest_pas_une_porte_ouverte():
    """Son message du 17h07 : aimable, mais il n'attend aucune suite — ni envoi
    ni alerte. C'est ce que doit donner la combinaison des deux règles."""
    msg = ("Bonjour Rudy, Les personnes que je connais et qui rencontrent ces "
           "problématiques font généralement appel à Mathieu Hagel, Architecte "
           "Libre ou à l'école Home. Elles ont donc déjà quelqu'un pour les "
           "accompagner sur ces sujets, je suis désolée de ne pas pouvoir vous "
           "aider davantage. J'espère que vous trouverez ce que vous recherchez.")
    assert not _OPEN_DOOR_RE.search(msg)


def test_une_vraie_relance_rouvre_le_fil():
    """À l'inverse, si le prospect revient avec une demande, la porte s'ouvre."""
    assert _OPEN_DOOR_RE.search(
        "Finalement, pouvons-nous en discuter la semaine prochaine ?")
    assert _OPEN_DOOR_RE.search("Envoyez-moi vos tarifs, ça m'intéresse")


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
