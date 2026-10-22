"""Réponses types FIXES + pré-tri déterministe : zéro appel IA sur les cas évidents.

Contexte (Rudy 14/09) : réduire les tokens. Deux étages :

1. `quick_intent` — pré-tri SANS IA, avant le classifier :
   - STOP / désinscription → `unsubscribe` (même règle `is_stop_signal` que le
     garde-fou post-classifier, simplement appliquée AVANT de payer l'IA) ;
   - « non merci » CLAIR et court, sans aucun signal d'ambiguïté →
     `not_interested_polite`.
   Tout le reste (le moindre doute) → None = le classifier IA tranche.
   Les absences / bounces sont déjà filtrés sans IA en amont (`is_bounce_or_auto`).

2. `fixed_draft` — pour les 3 négatifs auto (`not_interested_polite`,
   `objection_price`, `objection_already_have_solution`), renvoie la réponse type
   telle quelle au lieu d'appeler le drafter. Seulement si un modèle existe pour
   ce compte ET que le prospect écrit en français ; sinon None = rédaction IA.

⚠️ Les textes ci-dessous doivent rester IDENTIQUES aux modèles de
src/prompts/draft.md (blocs « NÉGATIFS AUTO »). Modifier les deux ensemble.
"""
from __future__ import annotations

import re

from .classifier import Classification, is_stop_signal
from .drafter import Draft

NEGATIVE_AUTO_INTENTS = frozenset({
    "not_interested_polite",
    "objection_price",
    "objection_already_have_solution",
    "wrong_person_redirect",  # vraie personne « pas nous » (Rudy 15/09)
})

# --- Modèles (HTML simple : <p> / <br>, comme le drafter) ---------------------

WC_NEGATIVE_HTML = (
    "<p>Bonjour,</p>"
    "<p>C'est noté, merci d'avoir pris le temps de répondre, je ne vous dérange pas plus. "
    "Auriez-vous en tête un ou des contacts qui rencontrent ces problématiques ?</p>"
    # Rudy 16/10 : la REFONTE passe en 2e (avec le pré-design offert et sa
    # question d'accroche), les deux outils ferment. Le bloc « applications IA
    # sur mesure » + etudes-cas-ia.pdf est RETIRÉ.
    "<p>Je propose également des refontes de sites (envoi avant tout paiement "
    "d'un pré-design pour que vous puissiez visualiser votre nouveau site) à "
    'petit prix <a href="https://www.webmarketing-conseil.fr/site-internet/">'
    "https://www.webmarketing-conseil.fr/site-internet/</a> (voudriez-vous voir "
    "ce que cela pourrait donner ?)</p>"
    "<p>J'ai enfin sorti deux outils d'automatisation :<br>"
    '- <a href="https://www.growpulser.com">www.growpulser.com</a> pour automatiser la '
    "création et publication de contenus sur les réseaux sociaux.<br>"
    '- <a href="https://www.growposter.com">www.growposter.com</a> pour automatiser la '
    "création et publication de contenus SEO.</p>"
    "<p>Bien à vous,<br>Rudy Viard</p>"
)

CMACLIM_NEGATIVE_HTML = (
    "<p>Bonjour,</p>"
    "<p>C'est noté, merci d'avoir pris le temps de nous répondre, nous ne vous "
    "solliciterons pas davantage.</p>"
    "<p>Si le sujet du confort thermique de vos locaux revient à l'ordre du jour (vague "
    "de chaleur, travaux, évolution de vos besoins), il vous suffira de répondre à ce "
    "message pour reprendre contact.</p>"
    "<p>Et si vous pensez à un autre établissement ou à un collègue que cela pourrait "
    "intéresser, n'hésitez pas à lui transférer cet email.</p>"
    # Variable ManyReach (signature du compte expéditeur), remplacée à l'envoi.
    "<p>Bien à vous,<br>{{SENDER_SIGNATURE}}</p>"
)

_TEMPLATES = {
    "wc": WC_NEGATIVE_HTML,
    "cmaclim": CMACLIM_NEGATIVE_HTML,
}

# --- Pré-tri déterministe -------------------------------------------------------

# Refus NETS (tous présents dans _CLEAR_NO_MARKERS de run_bot → le garde-fou
# anti-devinette en aval les accepte).
_CLEAR_NO = (
    "non merci", "merci mais non", "pas intéress", "pas interess", "intéresse pas",
    "interesse pas", "pas besoin", "pas de besoin", "aucun besoin", "n'ai pas besoin",
    "n'avons pas besoin", "pas concerné", "pas concerne", "ne souhaite pas",
    "ne souhaitons pas", "pas pour nous", "sans suite", "ne donnerai pas suite",
    "ne donnerons pas suite",
)

# Le MOINDRE de ces indices = cas potentiellement ambigu → on laisse l'IA
# trancher. Volontairement large : un faux « ambigu » coûte un appel IA, un faux
# « refus clair » envoie la mauvaise réponse (lead chaud, redirection, plainte…).
_AMBIGUITY_CUES = (
    "?", "@", "http", "www.",
    # nuance / porte ouverte / plus tard
    "mais", "sauf", "plutôt", "plutot", "peut-être", "peut être", "peut-etre", "peut etre",
    "plus tard", "prochain", "rentrée", "rentree", "recontact", "rappel", "appel",
    "téléphon", "telephon", "rdv", "rendez", "dispo",
    # demande d'info / prix / budget
    "tarif", "prix", "combien", "budget", "devis", "cher", "info", "précision",
    "precision", "plaquette", "document",
    # redirection vers quelqu'un d'autre
    "collègue", "collegue", "responsable", "direct", "transmet", "transmis", "transfér",
    "transfer", "adresse", "contactez", "contacter",
    # déjà équipé (= objection, réponse type aussi, mais laissée à l'IA)
    "déjà", "deja", "prestataire", "agence", "équipé", "en interne",
    # agacement / plainte → hostile / désinscription, jamais une réponse commerciale
    "spam", "arrêt", "arret", "harcel", "plainte", "cnil", "rgpd", "signal", "supprim",
    "retir", "liste", "insist", "relance", "marre", "!!",
    # intérêt (formes POSITIVES seulement : « ça ne m'intéresse pas » doit passer)
    "ça m'intéresse", "ca m'intéresse", "cela m'intéresse", "ça nous intéresse",
    "cela nous intéresse", "intéressant", "interessant", "curieu", "essayer",
    "tester", "gratuit",
)

_AFFIRMATIVE_RE = re.compile(r"\b(oui|ok|okay|d'accord|daccord|volontiers|avec plaisir)\b")

# « NON » SEC (Rudy 22/10, cas antoine@victorie-music.com). Le prospect répond
# « Non », rien d'autre. Aucun marqueur de _CLEAR_NO ne correspond (« non merci »,
# « pas intéressé »…), donc le garde-fou anti-devinette renvoyait ça en ALERTE —
# et le bot l'avait même classé « demande d'infos ». Rudy : « quand vraiment le
# mec dit non, ne me fais pas remonter l'alerte, fais juste le process
# automatique not interested et réponse auto ».
# On retire les formules de politesse : s'il ne reste QUE le mot de refus, c'est
# un refus, pas une ambiguïté.
_POLITESSE_RE = re.compile(
    r"\b(bonjour|bonsoir|madame|monsieur|messieurs|mesdames|merci|cordialement|"
    r"salutations|sincerement|sincèrement|respectueusement|"
    r"bonne (journee|journée|soiree|soirée|continuation|reception|réception)|"
    r"desole|desolee|désolé|désolée|desoles|désolés)\b")


def _is_bare_no(low: str) -> bool:
    """True si le message se réduit à « non » une fois la politesse retirée."""
    if "?" in (low or ""):
        return False   # une question reste une question, jamais un refus sec
    txt = _POLITESSE_RE.sub(" ", low or "")
    txt = re.sub(r"[^a-zàâäéèêëîïôöùûüç]+", " ", txt).strip()
    return txt in ("non", "no", "nan", "non non", "non non non")

MAX_QUICK_LEN = 300


def _norm(text: str) -> str:
    return (text or "").replace("’", "'").replace(" ", " ").lower().strip()


def quick_intent(clean_body: str, subject: str = "") -> str | None:
    """Intent évident sans IA, ou None si le moindre doute.

    `clean_body` = corps NETTOYÉ (sans HTML ni citation), comme pour le classifier.
    L'appelant écarte en plus les messages contenant une date/heure (_DATETIME_RE).
    """
    body = (clean_body or "").strip()
    if len(body) < 2:
        return None
    if is_stop_signal(body, subject):
        return "unsubscribe"
    low = _norm(body)
    # Un « non » sec passe AVANT les garde-fous d'ambiguïté : il n'y a rien à
    # interpréter, et c'est justement ce qui le faisait remonter en alerte.
    if _is_bare_no(low):
        return "not_interested_polite"
    if len(low) > MAX_QUICK_LEN:
        return None
    if not any(m in low for m in _CLEAR_NO):
        return None
    if any(c in low for c in _AMBIGUITY_CUES):
        return None
    if _AFFIRMATIVE_RE.search(low):
        return None
    return "not_interested_polite"


def quick_classification(intent: str, clean_body: str) -> Classification:
    return Classification(
        intent=intent,
        confidence=0.97,
        key_phrase=(clean_body or "").strip()[:120],
        redirected_email=None,
        redirected_to=None,
        language="fr",
        reasoning="pré-tri déterministe (cas évident, sans IA)",
    )


# --- Réponse type fixe ----------------------------------------------------------

def _template_key(client: dict | None, space_id: str | None) -> str | None:
    if space_id:
        # Un espace = un client. Pas de modèle pour cet espace → IA (on n'envoie
        # JAMAIS le modèle Webmarketing Conseil depuis l'espace d'un client).
        key = space_id.strip().lower()
        return key if key in _TEMPLATES and key != "wc" else None
    if client is None or client.get("is_default"):
        return "wc"
    cid = str(client.get("id") or "").strip().lower()
    return cid if cid in _TEMPLATES and cid != "wc" else None


def fixed_draft(
    classification: Classification,
    client: dict | None,
    space_id: str | None = None,
    silent_on_not_interested: bool = False,
) -> Draft | None:
    """Réponse type prête à envoyer, ou None → le drafter IA prend le relais."""
    if classification.intent not in NEGATIVE_AUTO_INTENTS:
        return None
    if not str(classification.language or "fr").lower().startswith("fr"):
        return None
    key = _template_key(client, space_id)
    if key is None:
        return None
    if silent_on_not_interested and classification.intent == "not_interested_polite":
        return Draft(body_html=None, subject=None, skip_send=True,
                     notes="silent_on_not_interested (réglage) — sans IA")
    return Draft(body_html=_TEMPLATES[key], subject=None, skip_send=False,
                 notes=f"réponse type fixe « {key} » (sans IA)")
