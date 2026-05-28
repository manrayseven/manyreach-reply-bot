"""Classify a ManyReach reply into one of N intents using Claude."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import anthropic

from .manyreach import Message

PROMPT_PATH = Path(__file__).parent / "prompts" / "classify.md"

VALID_INTENTS = frozenset(
    {
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
        self.client = anthropic.Anthropic(api_key=key)
        self.model = model
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def classify(
        self,
        reply: Message,
        original_outreach: Message | None = None,
    ) -> Classification:
        clean_reply = _trim_quoted_history(_strip_html(reply.body))
        original_text = ""
        if original_outreach:
            original_text = _trim_quoted_history(_strip_html(original_outreach.body), max_len=2000)

        user_content = (
            f"## Mail cold initial envoyé par Rudy\n"
            f"Subject: {original_outreach.subject if original_outreach else '(unknown)'}\n"
            f"From: {original_outreach.from_email if original_outreach else '(unknown)'}\n"
            f"---\n"
            f"{original_text or '(non disponible)'}\n\n"
            f"## Reply reçu\n"
            f"From: {reply.from_email}\n"
            f"Subject: {reply.subject}\n"
            f"---\n"
            f"{clean_reply}\n"
        )

        msg = self.client.messages.create(
            model=self.model,
            max_tokens=400,
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
