"""Classify a ManyReach reply into one of N intents using Claude."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic

from .manyreach import Message

PROMPT_PATH = Path(__file__).parent / "prompts" / "classify.md"

VALID_INTENTS = frozenset(
    {
        "meeting_confirmed",
        "interested_warm",
        "interested_lukewarm",
        "objection_price",
        "objection_timing",
        "objection_already_have_solution",
        "wrong_person_redirect",
        "ask_more_info",
        "not_interested_polite",
        "unsubscribe",
        "hostile",
        "bounce_or_auto",
        "ack_only",  # le prospect remercie/accuse réception → on se TAIT (silencieux)
    }
)


@dataclass(frozen=True)
class Classification:
    intent: str
    confidence: float
    key_phrase: str
    redirected_email: str | None
    redirected_to: str | None
    language: str
    reasoning: str
    # Booking details (filled mainly for meeting_confirmed)
    confirmed_datetime: str | None = None  # ISO 8601, the agreed meeting start
    contact_phone: str | None = None       # phone to call the prospect on
    zoom_link: str | None = None           # prospect's own video link if given
    offer_label: str | None = None         # short label for the event title
    prospect_name: str | None = None       # name extracted from the reply signature
    # Le prospect partage SON calendrier / un lien de booking ("share my calendar",
    # "pick a time", "Calendly link") → on N'envoie PAS de réponse auto (le bot ne
    # peut pas réserver dans le calendrier du prospect), et on prévient Rudy par
    # email pour qu'il aille booker lui-même.
    prospect_offers_calendar: bool = False
    # Pour les objection_timing : date à laquelle relancer ("dans 3 mois" → ISO 8601
    # de la date cible, "septembre" → 1er septembre, "Q4" → 1er octobre). Sinon
    # null → on prend par défaut J+90 quand on crée l'event.
    recontact_datetime: str | None = None

    @classmethod
    def from_json(cls, data: dict) -> "Classification":
        intent = data.get("intent", "").strip()
        if intent not in VALID_INTENTS:
            raise ValueError(f"Invalid intent from classifier: {intent!r}")
        return cls(
            intent=intent,
            confidence=float(data.get("confidence", 0.0)),
            key_phrase=data.get("key_phrase", ""),
            redirected_email=data.get("redirected_email") or None,
            redirected_to=data.get("redirected_to") or None,
            language=data.get("language", "fr"),
            reasoning=data.get("reasoning", ""),
            confirmed_datetime=data.get("confirmed_datetime") or None,
            contact_phone=data.get("contact_phone") or None,
            zoom_link=data.get("zoom_link") or None,
            offer_label=data.get("offer_label") or None,
            prospect_name=data.get("prospect_name") or None,
            prospect_offers_calendar=bool(data.get("prospect_offers_calendar", False)),
            recontact_datetime=data.get("recontact_datetime") or None,
        )


def _strip_html(html: str) -> str:
    """Remove HTML tags for cleaner classification context."""
    # Strip script/style blocks first
    cleaned = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"&nbsp;", " ", cleaned)
    cleaned = re.sub(r"&amp;", "&", cleaned)
    cleaned = re.sub(r"&lt;", "<", cleaned)
    cleaned = re.sub(r"&gt;", ">", cleaned)
    cleaned = re.sub(r"&quot;", '"', cleaned)
    cleaned = re.sub(r"&#39;", "'", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


# En-têtes de citation Gmail/Outlook : "... a écrit :" (FR) / "... wrote:" (EN) /
# "-----Original Message-----" / ligne "____". Non ambigus (toujours suivis du
# message cité) → on coupe DÈS leur apparition, même tôt dans le texte.
# CRUCIAL pour les refus COURTS : "Non, pas intéressé. Bonne journée Le 17 juin
# ... a écrit : <tout le pitch cité>". Sans ça, le classifier lit le pitch cité
# et classe à tort meeting_confirmed → faux MeetingBooked + alerte au lieu d'une
# réponse auto (cas proramonage45, gpharmaciese — 17/06).
_QUOTE_END_RE = re.compile(
    r"a\s+écrit\s*:|wrote\s*:|-{2,}\s*Original Message|_{10,}"
    # Séparateur de citation FR (Outlook/webmail Orange…) : "----- Message
    # d'origine -----" / "Message original" / "Le message d'origine". CRUCIAL :
    # cas contact@remialgis (refus court + pitch cité "disponible pour un appel"
    # → sinon classé à tort meeting_confirmed → faux MeetingBooked).
    r"|-{0,}\s*Message\s+d['’]origine|-{0,}\s*Message\s+original"
    # En-têtes de citation Outlook (FR/EN) + Allemand : "De : X Envoyé/Objet : ..."
    # / "From: X Sent/Subject: ..." / "Von: X Gesendet/Betreff: ...". Non ambigus
    # (un vrai message n'a jamais cette paire) → coupe même tôt. CRUCIAL pour les
    # refus courts + citation Outlook du pitch (cas contact@3gelec.fr — sinon
    # l'override lit "audits flash / créneau" cité et bascule en meeting_confirmed).
    r"|\bDe\s*:.{0,220}?\b(?:Envoy\w{0,3}|Objet)\s*:"
    r"|\bFrom\s*:.{0,220}?\b(?:Sent|Subject)\s*:"
    r"|\bVon\s*:.{0,220}?\b(?:Gesendet|Betreff)\s*:",
    re.IGNORECASE | re.DOTALL,
)


def _trim_quoted_history(body: str, max_len: int = 4000) -> str:
    """Trim the quoted history that gmail/outlook clients append."""
    earliest = len(body)
    m = _QUOTE_END_RE.search(body)
    if m:
        cut = m.start()
        # Remonter au début de l'en-tête ("Le <date> ..."/"On <date> ...") si on
        # trouve un "Le "/"On " proche AVEC un chiffre (= une date) entre les deux
        # → on enlève l'en-tête entier (évite que la date "à 11:05" soit lue comme
        # un horaire de RDV). Sinon on coupe juste avant "a écrit :"/"wrote:".
        for kw in ("Le ", "On "):
            h = body.rfind(kw, max(0, cut - 220), cut)
            if h != -1 and any(ch.isdigit() for ch in body[h:cut]):
                cut = min(cut, h)
        earliest = cut
    # Marqueurs Outlook positionnels (gardés avec le garde-fou >100 car "De :"/
    # "From:" peuvent apparaître légitimement tôt dans une phrase).
    for marker in ("De :", "From:"):
        idx = body.find(marker)
        if idx > 100 and idx < earliest:
            earliest = idx
    body = body[:earliest].strip()
    if len(body) > max_len:
        body = body[:max_len] + "...[truncated]"
    return body


def _salvage_classification(raw: str) -> dict:
    """Récupère les champs d'une sortie classifier malformée (JSON tronqué/cassé)
    via regex tolérant, pour ne pas perdre le traitement du reply. L'intent
    récupéré est ensuite validé par l'appelant (fallback si invalide)."""
    def _f(pat, default=None):
        m = re.search(pat, raw)
        return m.group(1) if m else default

    conf = _f(r'"confidence"\s*:\s*([0-9.]+)')
    return {
        "intent": _f(r'"intent"\s*:\s*"([a-z_]+)"'),
        "confidence": float(conf) if conf else 0.5,
        "key_phrase": _f(r'"key_phrase"\s*:\s*"([^"]*)"', ""),
        "language": _f(r'"language"\s*:\s*"([a-z]+)"', "fr"),
        "reasoning": "[récupéré après JSON classifier malformé]",
        "redirected_email": _f(r'"redirected_email"\s*:\s*"([^"]+)"'),
        "redirected_to": _f(r'"redirected_to"\s*:\s*"([^"]+)"'),
        "contact_phone": _f(r'"contact_phone"\s*:\s*"([^"]+)"'),
    }


class Classifier:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-haiku-4-5-20251001",
    ):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY env var not set")
        # max_retries: the SDK retries 429/5xx/529 with exponential backoff.
        # Bumped to 6 to ride through transient "Overloaded" (529) spikes.
        self.client = anthropic.Anthropic(api_key=key, max_retries=6)
        self.model = model
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def classify(
        self,
        reply: Message,
        original_outreach: Message | None = None,
        previous_message: str = "",
    ) -> Classification:
        clean_reply = _trim_quoted_history(_strip_html(reply.body))
        original_text = ""
        if original_outreach:
            original_text = _trim_quoted_history(_strip_html(original_outreach.body), max_len=2000)

        # Reference "now" so the model can resolve relative dates ("mardi prochain
        # 14h") into an absolute ISO datetime. Use the reply's reception time as the
        # anchor (close to when the prospect wrote it).
        now_ref = reply.created_at.astimezone() if reply.created_at else datetime.now()
        now_str = now_ref.strftime("%A %d %B %Y %H:%M (%z)")

        prev_block = ""
        if previous_message:
            prev_block = (
                f"## DERNIER message qu'on a envoyé au prospect (juste avant son reply)\n"
                f"(Si le prospect ACCEPTE/valide un créneau proposé ici — ex. 'ok mardi', "
                f"'le 1er créneau', 'ça marche pour 14h' — résous confirmed_datetime au "
                f"créneau EXACT proposé ci-dessous.)\n"
                f"---\n{previous_message[:1500]}\n\n"
            )

        user_content = (
            f"## Date/heure de référence (pour résoudre les dates relatives)\n"
            f"Le reply a été reçu le : {now_str}\n"
            f"Fuseau : Europe/Paris. Résous tout 'mardi prochain', 'demain 14h', etc. par rapport à cette date.\n\n"
            f"## Mail cold initial envoyé par Rudy\n"
            f"Subject: {original_outreach.subject if original_outreach else '(unknown)'}\n"
            f"From: {original_outreach.from_email if original_outreach else '(unknown)'}\n"
            f"---\n"
            f"{original_text or '(non disponible)'}\n\n"
            f"{prev_block}"
            f"## Reply reçu\n"
            f"From: {reply.from_email}\n"
            f"Subject: {reply.subject}\n"
            f"---\n"
            f"{clean_reply}\n"
        )

        msg = self.client.messages.create(
            model=self.model,
            max_tokens=700,
            # temperature=0 : classification DÉTERMINISTE. Sans ça (défaut 1.0), un
            # même reply pouvait être classé différemment d'un run à l'autre sur les
            # cas-limites → alertes parasites (ex. jlevasseur "nous avons une bonne
            # agence marketing" tantôt déjà-équipé/auto, tantôt tiède/alerte).
            temperature=0,
            system=[
                {
                    "type": "text",
                    "text": self.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        raw = msg.content[0].text.strip()
        # Strip ```json fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```\s*$", "", raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # JSON malformé/tronqué (ex. reply = juste un numéro de tél → le modèle
            # part parfois en vrille + coupe à max_tokens). On RÉCUPÈRE les champs par
            # regex au lieu de crasher tout le traitement du reply.
            data = _salvage_classification(raw)
        # Intent absent ou invalide → fallback SÛR = interested_warm (→ alerte, review
        # humaine) plutôt que lever une erreur. Ne jamais bloquer un reply là-dessus.
        if data.get("intent") not in VALID_INTENTS:
            data = {
                **data,
                "intent": "interested_warm",
                "confidence": min(float(data.get("confidence", 0.5) or 0.5), 0.7),
                "reasoning": (data.get("reasoning") or "")
                + " [intent invalide/illisible → fallback interested_warm pour review]",
            }
        return Classification.from_json(data)
