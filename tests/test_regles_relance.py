# -*- coding: utf-8 -*-
"""Les règles de relance de Rudy (11/11, reprécisées le 19/11).

    « Si la personne est Interested              -> je traite a la main.
      Si elle est Interested et s'est vue proposer un rendez-vous mais n'a pas
         repondu OU n'est pas passee en Meeting Booked
                                                 -> relancer, SANS jour ni
                                                    horaire precis.
      Si elle est Meeting Booked ou Meeting Completed ET a recu un email avec
         une proposition restee sans reponse     -> relancer sur la proposition.
      Ils doivent tous etre traites automatiquement, y compris les anciens. »

Avant : une cadence aveugle J+2 / J+5 pour tout le monde, quel que soit le
statut et quoi qu'on ait envoye.

Lance sans pytest :  python tests/test_regles_relance.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from src import relances  # noqa: E402

MAINTENANT = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


@dataclass
class M:
    type: str
    created_at: datetime
    body: str = ""
    message_id: str = "m"


def _il_y_a(j):
    return MAINTENANT - timedelta(days=j)


def _ctx(messages):
    return relances.lire_contexte(messages, MAINTENANT)


class _P:
    first_name = "Xavier"


# -- Regle 1 ---------------------------------------------------------------

def test_un_interested_sans_rien_de_propose_est_laisse_a_rudy():
    fil = [M("Sent", _il_y_a(40), "Bonjour, je developpe une approche pour..."),
           M("Reply", _il_y_a(39), "Ca m'interesse"),
           M("Sent", _il_y_a(38), "Merci de votre retour, je reviens vers vous.")]
    d = relances.decider("Interested", _ctx(fil), MAINTENANT)
    assert not d.relancer
    assert "main" in d.raison


# -- Regle 2 ---------------------------------------------------------------

def test_un_rdv_propose_sans_suite_est_relance():
    fil = [M("Reply", _il_y_a(10), "Ca m'interesse"),
           M("Sent", _il_y_a(9), "Etes-vous disponible cette semaine pour 15 min en visio ?")]
    d = relances.decider("Interested", _ctx(fil), MAINTENANT)
    assert d.relancer and d.motif == "rdv" and d.numero == 1


def test_la_relance_de_rdv_ne_donne_ni_jour_ni_heure():
    """« Pas avec un horaire et jour precis, car tu n'as pas acces a mon
    calendrier. » Proposer une heure que Rudy ne pourra pas tenir est pire que
    ne rien proposer."""
    fil = [M("Reply", _il_y_a(10), "ok"),
           M("Sent", _il_y_a(9), "Pouvons-nous appeler ? 15 min suffisent.")]
    c = _ctx(fil)
    for n in (1, 2):
        txt = relances.corps("rdv", n, _P(), c).lower()
        for interdit in ("lundi", "mardi", "mercredi", "jeudi", "vendredi",
                         "h30", "9h", "10h", "14h", "15h"):
            assert interdit not in txt, (n, interdit)
        brut = relances.corps("rdv", n, _P(), c)
        assert "Bien à vous" in brut and "Rudy Viard" in brut
        assert "Cordialement" not in brut
        assert "http" not in brut          # zero lien
        assert "€" not in brut        # zero tarif
    # Et on demande SES disponibilites a lui.
    assert "arrangent" in relances.corps("rdv", 1, _P(), c)


def test_trop_tot_apres_la_proposition_de_rdv():
    fil = [M("Reply", _il_y_a(2), "ok"),
           M("Sent", _il_y_a(1), "Un creneau cette semaine ?")]
    d = relances.decider("Interested", _ctx(fil), MAINTENANT)
    assert not d.relancer and d.revoir_dans > 1


# -- Regle 3 ---------------------------------------------------------------

def test_une_proposition_sans_reponse_est_relancee_apres_un_rdv():
    fil = [M("Reply", _il_y_a(30), "ok pour un appel"),
           M("Sent", _il_y_a(28), "Voici ce que je propose : ... je vous joins mes plaquettes")]
    for statut in ("MeetingBooked", "MeetingCompleted"):
        d = relances.decider(statut, _ctx(fil), MAINTENANT)
        assert d.relancer and d.motif == "proposition", statut
    txt = relances.corps("proposition", 1, _P(), _ctx(fil))
    assert "proposition du 22 octobre" in txt
    assert "Bonjour Xavier" in txt


def test_un_rdv_cale_sans_proposition_n_est_pas_touche():
    """Rudy pilote ses rendez-vous lui-meme (consigne du 11/11)."""
    fil = [M("Reply", _il_y_a(20), "ok jeudi 9h"),
           M("Sent", _il_y_a(19), "Parfait, c'est note.")]
    d = relances.decider("MeetingBooked", _ctx(fil), MAINTENANT)
    assert not d.relancer and "aucune proposition" in d.raison


def test_un_client_gagne_n_est_jamais_relance():
    """Consigne du 11/11 : Won bloque partout."""
    fil = [M("Reply", _il_y_a(30), "ok"),
           M("Sent", _il_y_a(29), "Voici ma proposition, plaquette jointe")]
    d = relances.decider("Won", _ctx(fil), MAINTENANT)
    assert not d.relancer and "jamais relanc" in d.raison


# -- Garde-fous communs ----------------------------------------------------

def test_on_ne_relance_jamais_celui_qui_a_parle_en_dernier():
    fil = [M("Sent", _il_y_a(10), "Un creneau ?"),
           M("Reply", _il_y_a(4), "je regarde")]
    d = relances.decider("Interested", _ctx(fil), MAINTENANT)
    assert not d.relancer and "parlé en dernier" in d.raison


def test_deux_relances_maximum():
    """Le releve du 19/11 montrait deja 51 fils avec trois envois ou plus
    d'affilee. Au-dela de deux, c'est du harcelement."""
    fil = [M("Reply", _il_y_a(40), "ok"),
           M("Sent", _il_y_a(39), "Etes-vous disponible pour 15 min ?"),
           M("Sent", _il_y_a(30), "Je me permets de revenir vers vous rapidement"),
           M("Sent", _il_y_a(20), "Dernier message de ma part")]
    c = _ctx(fil)
    assert c.relances_deja == 2
    assert not relances.decider("Interested", c, MAINTENANT).relancer


def test_une_relance_ne_compte_pas_comme_une_proposition():
    """Sinon la relance s'auto-alimente : chaque envoi justifie le suivant."""
    fil = [M("Reply", _il_y_a(40), "ok"),
           M("Sent", _il_y_a(39), "Bonjour, merci de votre retour."),
           M("Sent", _il_y_a(30), "Je me permets de revenir vers vous rapidement - "
                                  "est-ce toujours d'actualite ?")]
    c = _ctx(fil)
    assert c.relances_deja == 1
    assert not c.rdv_propose and not c.proposition_envoyee
    assert not relances.decider("Interested", c, MAINTENANT).relancer


def test_la_maquette_n_est_relancee_que_si_rudy_l_active():
    """Une maquette n'est ni un rendez-vous ni une proposition chiffree : elle
    ne figure dans aucune des trois regles."""
    lien = "https://x.fr/wp-content/uploads/abc.html"
    fil = [M("Reply", _il_y_a(20), "ok montrez-moi"),
           M("Sent", _il_y_a(18), 'Voici : <a href="' + lien + '">apercu</a>')]
    c = _ctx(fil)
    assert c.maquette_envoyee
    assert not relances.decider("Interested", c, MAINTENANT).relancer
    d = relances.decider("Interested", c, MAINTENANT, maquettes_actives=True)
    assert d.relancer and d.motif == "maquette"


def test_le_moteur_utilise_bien_ces_regles():
    src = (ROOT / "scripts" / "run_bumps.py").read_text(encoding="utf-8")
    assert "relances.decider(" in src and "relances.corps(" in src
    assert "relances.STATUTS_RELANCABLES" in src
    # Un flux PAR statut : le statut n'est pas sur la fiche prospect.
    assert "confirmed_statuses=(_st,)" in src
    # L'ancienne cadence aveugle a disparu.
    assert "decide_bump(" not in src and "build_bump_body(" not in src


def test_plafond_dur_d_envois_consecutifs():
    """L'ancien moteur s'arretait a trois messages sans reponse. En reecrivant
    les regles j'avais perdu ce garde-fou : le releve du 19/11 compte 51 fils a
    trois envois d'affilee ou plus, dont douze a cinq. Leur en envoyer deux de
    plus serait du harcelement, quelle que soit la regle qui le justifie."""
    fil = [M("Reply", _il_y_a(60), "ok"),
           M("Sent", _il_y_a(50), "Etes-vous disponible pour 15 min ?"),
           M("Sent", _il_y_a(40), "Je reviens vers vous."),
           M("Sent", _il_y_a(30), "Un point rapide ?"),
           M("Sent", _il_y_a(20), "Toujours interesse ?")]
    c = _ctx(fil)
    assert c.envois_consecutifs == 4
    assert c.relances_deja == 0          # aucune de NOS formules de relance
    d = relances.decider("Interested", c, MAINTENANT)
    assert not d.relancer and "affil" in d.raison
    # Trois d'affilee passent encore : le plafond est a quatre.
    c3 = _ctx(fil[:-1])
    assert c3.envois_consecutifs == 3
    assert relances.decider("Interested", c3, MAINTENANT).relancer


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
