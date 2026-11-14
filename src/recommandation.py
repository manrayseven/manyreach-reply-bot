"""Contact RECOMMANDÉ dans une réponse (Rudy 14/11, demandé plusieurs fois).

« Bonjour, office@humy.be. Cordialement » ou « voyez plutôt avec notre
responsable, m.durand@… » : une vraie personne nous oriente vers le bon
interlocuteur. Lead Finder (scripts/reinjecter-nouvelles-adresses.ts, 8 h 15 et
16 h 15 sur le serveur) inscrit cette adresse dans la même campagne avec la
séquence déjà écrite. Le dashboard n'a donc PAS à la montrer.

MÊME LECTURE que lireRecommandation() dans Lead Finder
(src/pipeline/nouvelle-adresse.ts) : si l'une change, changer l'autre. Le bot
ne retire une alerte que si cette lecture-ci trouve l'adresse, c'est-à-dire
quand Lead Finder la trouvera aussi.
"""
from __future__ import annotations

import html
import re
import unicodedata

MAIL = re.compile(r"[a-z0-9][a-z0-9._%+-]*@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
CITATION = [
    re.compile(r"\ble (?:lun|mar|mer|jeu|ven|sam|dim)[^\n]{0,90}a ecrit"),
    re.compile(r"\ble \d{1,2}[ /][^\n]{0,70}a ecrit"),
    re.compile(r"-{3,}\s*(?:original message|message d'origine|message original)"),
    re.compile(r"\bon [a-z]{3}, [^\n]{0,90}wrote"),
    re.compile(r"\bde ?: [^\n]{0,90}\n?\s*(?:envoye|date) ?:"),
    re.compile(r"\bfrom ?: [^\n]{0,90}\n?\s*sent ?:"),
]
FORMULE_FIN = re.compile(
    r"\n?\s*(?:cordialement|bien (?:a vous|cordialement)|bonne (?:journee|soiree|continuation)"
    r"|sinceres salutations|salutations|belle journee|meilleures salutations|best regards"
    r"|kind regards|regards|envoye de mon|sent from my)\b")
REFUS = re.compile(
    r"(?:pas (?:interesse|interessee|interesses|besoin|de besoin|d'interet|pour nous|le moment)|non merci"
    r"|n'(?:avons|ai) pas (?:de )?besoins?"
    r"|ne sommes pas|ne suis pas|aucun (?:besoin|interet)|deja (?:un|une|equipe|prestataire)"
    r"|no thanks|not interested)")
ABSENCE = re.compile(
    r"\b(?:absent|absente|absence|conge|conges|vacances|out of office|de retour|a mon retour"
    r"|en deplacement|indisponible|annual leave|on leave|maternite)\b")
NE_PLUS_ECRIRE = re.compile(
    r"(?:\bstop\b|desabonn|desinscri|ne plus (?:me|nous) (?:contacter|solliciter|ecrire|demarcher)"
    r"|\bspam\b|demarchage|\brgpd\b|unsubscribe|retirer de (?:votre|vos) liste)")
ORIENTE = re.compile(
    r"(?:contact(?:er|ez)?|ecri(?:re|vez)|adress(?:er|ez)|voir (?:avec|plutot)|voyez"
    r"|transm(?:ettre|ettez|is)|envoy(?:er|ez)|joindre|interlocut|responsable|en charge"
    r"|qui (?:gere|s'occupe)|s'occupe|service|plutot|merci de|prendre contact|a l'attention)")
_NOUS = re.compile(r"webmarketing|growpulser|growposter")


def texte_nu(corps: str) -> str:
    s = str(corps or "")
    s = re.sub(r"(?is)<style.*?</style>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h\d)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[‘’ʼ]", "'", s)
    s = re.sub(r"[​-‏⁠﻿­]", "", s)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*", "\n", s)
    return s.strip()


def _masquer(s: str) -> str:
    return MAIL.sub(lambda m: re.sub(r"[.@]", "_", m.group(0)), s)


def lire_recommandation(corps: str, de: str, a: str = "") -> list[str]:
    """Adresses recommandées (1 ou 2), ou [] s'il n'y en a pas de sûre."""
    s = texte_nu(corps)
    for r in CITATION:
        m = r.search(s)
        if m and m.start() > 10:
            s = s[:m.start()]
    masque = _masquer(s)
    fin = FORMULE_FIN.search(masque)
    utile = s[:fin.start()] if fin and fin.start() > 0 else s[:400]
    utile_m = _masquer(utile)
    if REFUS.search(utile_m) or ABSENCE.search(masque) or NE_PLUS_ECRIRE.search(utile_m):
        return []
    moi = (de or "").strip().lower()
    nous = (a or "").strip().lower()
    dom_nous = nous.split("@")[1] if "@" in nous else ""
    out: list[str] = []
    for m in MAIL.finditer(utile):
        adr = m.group(0).rstrip(".")
        if adr in (moi, nous) or (dom_nous and adr.endswith("@" + dom_nous)):
            continue
        if _NOUS.search(adr) or adr in out:
            continue
        out.append(adr)
    if not out or len(out) > 2:
        return []
    reste = re.sub(r"[a-z0-9._%+-]+_[a-z0-9_-]+_[a-z]{2,}", " ", utile_m)
    reste = re.sub(r"\b(?:bonjour|bonsoir|hello|madame|monsieur|rudy|merci)\b", " ", reste)
    reste = re.sub(r"[^a-z]+", " ", reste).strip()
    if not ORIENTE.search(utile_m) and len(reste) > 25:
        return []
    return out


_PORTE = re.compile(r"\?|volontiers|plaquette|tarif|grille|devis|rappel|rendez|rdv|appel|echanger|discuter"
                    r"|(?<!pas )interess|n'hesitez|envoyez|presentation|documentation|plus d'info")


def refus_lisible(corps: str) -> bool:
    """Refus écrit en clair dans le corps utile, sans porte ouverte (question,
    plaquette, tarif, rappel…). Sert au contrôle quotidien : un refus n'est pas
    une piste perdue."""
    s = texte_nu(corps)
    for r in CITATION:
        m = r.search(s)
        if m and m.start() > 10:
            s = s[:m.start()]
    fin = FORMULE_FIN.search(_masquer(s))
    utile = s[:fin.start()] if fin and fin.start() > 0 else s[:400]
    return bool(REFUS.search(utile)) and not _PORTE.search(utile)
