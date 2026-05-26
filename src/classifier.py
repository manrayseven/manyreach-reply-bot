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


def _trim_quoted_history(body: str, max_len: int = 4000) -> str:
    """Trim the quoted '> ...' history that gmail/outlook clients append."""
    # Common quote markers in French and English
    markers = [
        "On wrote:",
        "Le ",  # "Le 12 août 2025, ... a écrit :"
        "De :",
        "From:",
        "________________________________",
        "-----Original Message-----",
    ]
    earliest = len(body)
    for m in markers:
        idx = body.find(m)
        if idx > 100 and idx < earliest:
            earliest = idx
    body = body[:earliest].strip()
    if len(body) > max_len:
        body = body[:max_len] + "...[truncated]"
    return body


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
            max_tokens=500,
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
        except json.JSONDecodeError as e:
            raise ValueError(f"Classifier returned non-JSON: {raw[:300]}") from e
        return Classification.from_json(data)
