"""Changement d'adresse / départ : silence total (Rudy 11/11).

Deux cas réels du 11/11, tous deux remontés en alerte puis marqués « Unsub » à
la main par Rudy pour s'en débarrasser :

  williambeekman1979@gmail.com
    « Veuillez prendre note du changement d'adresse mail de la société, en effet
      Photo WilliamB a changé de propriétaire mais pas de lieu. Nous serons
      heureux de vous répondre à notre nouvelle adresse mail
      contact@faivrephotos.fr »

  ecolepsy.lr@gmail.com
    « L'adresse mail de l'A.L.I Montpellier a changé. Merci d'écrire désormais
      à ali.montpellier.ecole@gmail.com »

Ce « Unsub » posé à la main avait une conséquence invisible : Lead Finder
n'exploite jamais la nouvelle adresse d'un contact désinscrit. La nouvelle
adresse était donc perdue à chaque fois.

Le bot se tait désormais SANS toucher au statut : c'est Lead Finder qui mène la
bascule (réinscription de la nouvelle adresse, puis désinscription de
l'ancienne).

Lance sans pytest :  python tests/test_changement_adresse.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from run_bot import _ADDRESS_CHANGE_RE  # noqa: E402

WILLIAM = (
    "Bonjour, Veuillez prendre note du changement d'adresse mail de la société, "
    "en effet Photo WilliamB à changer de propriétaire mais pas de lieu. Nous "
    "serons heureux de vous répondre à notre nouvelle adresse mail "
    "contact@faivrephotos.fr pour plus de rapidité dans nos réponse nous vous "
    "invitons à directement appeler le magasin au 03 81 50 56 10."
)
ALI = (
    "Bonjour, L'adresse mail de l'A.L.I Montpellier a changé. Merci d'écrire "
    "désormais à : ali.montpellier.ecole@gmail.com. Cette adresse reste active "
    "pendant une période transitoire. Bien cordialement, Le secrétariat"
)


def test_les_deux_cas_du_11_11_sont_silencieux():
    assert _ADDRESS_CHANGE_RE.search(WILLIAM)
    assert _ADDRESS_CHANGE_RE.search(ALI)


def test_boite_morte_sans_le_mot_desormais():
    """Cas arbolibre69 (12/11) : « Cette adresse e-mail n'est plus CONSULTÉE
    depuis un certain temps. Merci de nous adresser votre demande à : … ».
    Deux manques à la fois — « consultée » absent de la liste des participes, et
    la redirection exprimée sans le mot « désormais » qu'exigeait la règle."""
    assert _ADDRESS_CHANGE_RE.search(
        "Bonjour, Cette adresse e-mail n'est plus consultée depuis un certain "
        "temps. Merci de nous adresser votre demande à : Charlotte COURTILLET "
        "- Collaboratrice bureau d'études arboricoles ARBOLIBRE "
        "contact@arbolibre.fr 07 56 47 35 92")
    for msg in (
        "Cette boîte mail n'est plus relevée.",
        "Mon adresse n'est plus suivie, merci de transmettre vos messages à Paul.",
        "Merci de faire parvenir votre demande au service commercial.",
    ):
        assert _ADDRESS_CHANGE_RE.search(msg), msg


def test_les_departs_sont_couverts():
    for msg in (
        "Monsieur Durand ne fait plus partie de nos effectifs.",
        "Votre contact n'est plus en poste, voici le nouvel interlocuteur.",
        "He no longer works at this company.",
    ):
        assert _ADDRESS_CHANGE_RE.search(msg), msg


def test_le_depart_a_la_premiere_personne():
    """Cas bp2mmerel (11/11) : la règle attendait « A quitté la société », il
    écrivait « J'AI quitté la société ». On matche le verbe, pas l'auxiliaire."""
    for msg in (
        "Bonjour, Je vous informe que j'ai quitté la société. Je vous invite à "
        "transférer votre mail à Maryline MEREL, mmerel@bp2mformation.fr",
        "J'ai quitté mes fonctions au mois de septembre.",
        "Elle a quitté l'entreprise, merci de contacter son successeur.",
        "Je quitte la structure à la fin du mois.",
    ):
        assert _ADDRESS_CHANGE_RE.search(msg), msg


def test_une_vraie_reponse_nest_pas_silenciee():
    """La règle doit rester ÉTROITE : « quand c'est évident que c'est ça »."""
    for msg in (
        "Bonjour, votre proposition m'intéresse, pouvez-vous m'envoyer vos tarifs ?",
        "Non merci, nous ne sommes pas intéressés.",
        "Je suis à la recherche d'une personne avec vos compétences.",
        "Pouvez-vous me rappeler demain à 14h sur mon portable ?",
        "Nous avons changé de prestataire l'an dernier.",
    ):
        assert not _ADDRESS_CHANGE_RE.search(msg), msg


def test_le_bot_ne_touche_pas_au_statut():
    """Le silence ne doit PAS poser d'Unsub : sinon Lead Finder refuse de
    reprendre la nouvelle adresse, et la bascule est perdue."""
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    i = src.index('">> CHANGEMENT D\'ADRESSE / DÉPART — silencieux "')
    bloc = src[i:i + 1800]
    assert "_mark_done(reply.message_id)" in bloc
    assert "update_prospect" not in bloc and "blacklist" not in bloc
    assert "execute_plan" not in bloc


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
