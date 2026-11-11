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


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
