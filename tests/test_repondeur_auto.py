"""Un répondeur qui se déclare reste silencieux (Rudy 11/11).

Cas amandineverne@gmail.com. Objet : « Re: … », parfaitement neutre. Corps,
première ligne : « Ceci est un message automatique. » La phrase n'était
surveillée que dans l'OBJET — le message est donc parti en classification,
a été lu comme une demande d'infos, et a remonté en alerte.

Il citait en plus l'adresse d'une consœur ergothérapeute, ce qui déclenchait la
règle « un contact donné est une piste exploitable » : l'alerte devenait
inévitable. La détecter comme réponse automatique coupe tout en amont, avant
même l'appel au modèle.

Lance sans pytest :  python tests/test_repondeur_auto.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from src.manyreach import is_bounce_or_auto  # noqa: E402


@dataclass
class M:
    from_email: str
    subject: str
    body: str
    created_at: datetime = datetime(2026, 11, 11, tzinfo=timezone.utc)
    type: str = "Reply"
    message_id: str = "x"


AMANDINE = (
    "Bonjour, Ceci est un message automatique. Je vous remercie pour votre "
    "message. Afin de vous répondre le plus efficacement possible, voici "
    "quelques précisions utiles : - Je suis désormais spécialisée en "
    "rééducation intensive auprès des enfants de 0 à 4 ans porteurs de retard "
    "ou handicap moteur. Je ne réalise plus de suivis hebdomadaires. Si vous "
    "souhaitez la réalisation d'un bilan dans un autre contexte, je vous "
    "invite à vous rapprocher de l'autre Ergothérapeute du cabinet Mme Flavy "
    "FERNANDES : 06.10.74.10.61 - Flavyfernandes.ergo@gmail.com"
)


def test_le_repondeur_damandine_est_silencieux():
    assert is_bounce_or_auto(
        M("amandineverne@gmail.com", "Re: Votre page d'accueil", AMANDINE))


def test_la_tournure_en_est_une_reponse_automatique():
    """Cas evalequin (11/11) : « Ce message EST une réponse automatique générée
    à la réception de votre mail ». Les motifs attendaient « ceci est » — c'est
    le verbe qui est le point commun, pas le sujet."""
    corps = (
        "Bonjour, Ce message est une réponse automatique générée à la réception "
        "de votre mail. Je vous informe que mon activité libérale "
        "d'ergothérapeute est actuellement en pause. La reprise de mon activité "
        "est prévue au début de l'année 2027. Dans ce cas, ce message "
        "automatique fait foi et aucune autre réponse ne sera apportée."
    )
    assert is_bounce_or_auto(M("evalequin.ergo@gmail.com", "Re: Fiche Google", corps))


def test_les_autres_declarations_sont_couvertes():
    for corps in (
        "Cet accusé de réception est généré automatiquement. Le cabinet est fermé.",
        "Merci de ne pas répondre à ce message, il est envoyé par un robot.",
        "This is an automated response. We will get back to you shortly.",
        "Il s'agit d'un message automatique, votre demande a bien été reçue.",
    ):
        assert is_bounce_or_auto(M("x@y.fr", "Re: Question", corps)), corps


def test_un_humain_qui_mentionne_lautomatique_nest_pas_silencie():
    """La détection reste sur des formulations EXPLICITES : un humain qui
    s'excuse d'un envoi automatique doit continuer de remonter."""
    for corps in (
        "Désolé pour le message automatique de la semaine dernière, "
        "pouvez-vous me rappeler ?",
        "Votre email est tombé dans mes réponses automatiques, je vous réponds "
        "maintenant : oui, votre offre m'intéresse.",
    ):
        assert not is_bounce_or_auto(M("x@y.fr", "Re: Question", corps)), corps


def test_la_regle_du_delai_existe_et_exige_les_DEUX_conditions():
    """Cas Margaux Terrou (11/11) : un répondeur qui ne se déclare PAS. Après
    quatre rattrapages de formulation dans la journée, la détection ne repose
    plus seulement sur le vocabulaire — un message long ET immédiat est un
    répondeur, quoi qu'il dise.

    Les deux conditions doivent rester liées : un message court et immédiat est
    une vraie réponse (« oui », « rappelez-moi »), et un message long arrivé
    deux jours plus tard aussi.
    """
    src = (Path(__file__).resolve().parents[1] / "scripts" / "run_bot.py").read_text(
        encoding="utf-8")
    i = src.index("RÉPONDEUR DÉTECTÉ PAR LE DÉLAI")
    bloc = src[i:i + 1600]
    assert "_AUTO_MIN_CHARS" in bloc and "_AUTO_MAX_DELAY_S" in bloc
    # Le ET, pas le OU : les deux seuils dans la même condition.
    assert "and len(_clean_check) >= _AUTO_MIN_CHARS" in bloc
    assert "0 <= _delai <= _AUTO_MAX_DELAY_S" in bloc
    # Mesuré sur NOS envois antérieurs au reply, jamais sur un envoi postérieur.
    assert "m.created_at <= reply.created_at" in bloc
    # Réglable sans redéploiement.
    assert 'os.environ.get("AUTO_REPLY_MIN_CHARS"' in src
    assert 'os.environ.get("AUTO_REPLY_MAX_DELAY_S"' in src


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
