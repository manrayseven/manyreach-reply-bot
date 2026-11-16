# -*- coding: utf-8 -*-
"""Nouvelle adresse annoncée : repérée par la FORME, pas par le vocabulaire.

Rudy, 16/11 — crearenov :

    « Notre adresse email (CONTACT) a changée ! Merci d'envoyer vos mails à :
      contact@crearenov-valence.fr
      et de supprimer : contact.crearenov@gmail.com »

Remonté en alerte alors que c'est exactement ce qu'il ne veut plus voir. La
règle `_ADDRESS_CHANGE_RE` attendait « envoyer vos DEMANDES / MESSAGES /
COURRIER » ; il a écrit « vos MAILS ». Cinquième rattrapage de vocabulaire en
une semaine : on arrête d'allonger la liste.

Le signe retenu ne contient aucun mot : le message cite SA PROPRE adresse à
côté d'une autre. On ne rappelle l'adresse d'où l'on écrit que pour dire de ne
plus s'en servir.

Lance sans pytest :  python tests/test_nouvelle_adresse_annoncee.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")


def _charger():
    """Isole le détecteur sans importer run_bot (dépendances réseau)."""
    tree = ast.parse(SRC)
    garde = {"_EMAIL_DANS_CORPS_RE", "_ANNONCE_MAX_CHARS",
             "_nouvelle_adresse_annoncee", "_ADDRESS_CHANGE_RE"}
    bouts = []
    for node in tree.body:
        nom = None
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            nom = node.targets[0].id
        elif isinstance(node, ast.FunctionDef):
            nom = node.name
        if nom in garde:
            bouts.append(ast.get_source_segment(SRC, node))
    espace: dict = {"re": re}
    exec("\n".join(bouts), espace)  # noqa: S102 — code du dépôt, pas une entrée
    return espace


ESPACE = _charger()
detecte = ESPACE["_nouvelle_adresse_annoncee"]
VIEILLE_REGLE = ESPACE["_ADDRESS_CHANGE_RE"]

CREARENOV = (
    "Bonjour,\n\nNotre adresse email (CONTACT) à changée ! Merci d'envoyer "
    "vos mails à :\ncontact@crearenov-valence.fr\n"
    "et de supprimer : contact.crearenov@gmail.com\n\n"
    "Vous en remerciant par avance,\nCordialement."
)


def test_le_cas_crearenov_est_attrape():
    assert VIEILLE_REGLE.search(CREARENOV) is None, (
        "si l'ancienne règle le voit, ce test ne prouve plus rien")
    assert detecte(CREARENOV, "contact.crearenov@gmail.com") == \
        "contact@crearenov-valence.fr"


def test_la_famille_entiere_passe_sans_un_mot_de_vocabulaire():
    """Quatre façons de dire la même chose, aucune tournure commune."""
    for corps in (
        "Cette boite est fermee : contact.crearenov@gmail.com. "
        "Desormais : contact@crearenov-valence.fr",
        "contact.crearenov@gmail.com >>> contact@crearenov-valence.fr",
        "Merci de remplacer contact.crearenov@gmail.com par "
        "contact@crearenov-valence.fr dans vos fichiers.",
        "Plus de contact.crearenov@gmail.com. Tout passe par "
        "contact@crearenov-valence.fr",
    ):
        assert detecte(corps, "contact.crearenov@gmail.com"), corps


def test_une_vraie_reponse_n_est_pas_silenciee():
    """Le prospect parle de l'offre : il ne cite pas sa propre adresse."""
    for corps in (
        "Bonjour, je suis interesse, rappelez-moi au 06 12 34 56 78.",
        "Merci pour votre message mais nous ne sommes pas interesses.",
        "Pouvez-vous m'envoyer un devis a l'adresse compta@crearenov.fr ?",
        "Non merci.",
    ):
        assert detecte(corps, "contact.crearenov@gmail.com") is None, corps


def test_un_message_long_fait_taire_la_regle():
    """Citation de l'échange non coupée : notre propre adresse traîne encore
    dans le corps, et le texte est long. La règle s'abstient plutôt que de
    risquer de faire taire une vraie réponse."""
    long = CREARENOV + "\n" + ("blabla de la citation precedente. " * 30)
    assert len(long) > 600
    assert detecte(long, "contact.crearenov@gmail.com") is None


def test_la_branche_est_bien_cablee_dans_la_boucle():
    assert "_nouvelle_adresse_annoncee(" in SRC
    i = SRC.index("_silence = None")
    bloc = SRC[i:i + 500]
    assert "elif _new_addr:" in bloc
    assert "nouvelle adresse annoncée" in bloc


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
