"""Generate a reply draft using Claude, given a classified reply + context."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic

from .classifier import Classification, _strip_html, _trim_quoted_history
from .manyreach import Message, Prospect

PROMPT_PATH = Path(__file__).parent / "prompts" / "draft.md"


def _salvage_draft_json(raw: str) -> dict | None:
    """Best-effort recovery of draft fields from malformed/truncated JSON.

    Returns a dict with at least body_html if recoverable, else None.
    """
    # Pull body_html: everything between "body_html": "  and the closing
    # quote that precedes the next JSON key (subject/skip_send/notes) or EOS.
    m = re.search(
        r'"body_html"\s*:\s*"(.*?)"\s*,\s*"(?:subject|skip_send|notes)"',
        raw,
        flags=re.DOTALL,
    )
    body = None
    if m:
        body = m.group(1)
    else:
        # Truncated mid-body: grab from body_html start to end of string.
        m2 = re.search(r'"body_html"\s*:\s*"(.*)$', raw, flags=re.DOTALL)
        if m2:
            body = m2.group(1).rstrip().rstrip('"').rstrip(",").rstrip()
    if not body:
        return None
    # Unescape common JSON escapes that survived
    body = body.replace('\\"', '"').replace("\\n", "").replace("\\/", "/")

    skip = bool(re.search(r'"skip_send"\s*:\s*true', raw))
    subj_m = re.search(r'"subject"\s*:\s*"([^"]*)"', raw)
    notes_m = re.search(r'"notes"\s*:\s*"([^"]*)"', raw)
    return {
        "body_html": body,
        "subject": subj_m.group(1) if subj_m else None,
        "skip_send": skip,
        "notes": (notes_m.group(1) if notes_m else "") + " [récupéré après JSON malformé — à relire]",
    }


@dataclass(frozen=True)
class Draft:
    body_html: str | None
    subject: str | None
    skip_send: bool
    notes: str
    slots_used: bool = False

    @classmethod
    def from_json(cls, data: dict) -> "Draft":
        return cls(
            body_html=data.get("body_html"),
            subject=data.get("subject"),
            skip_send=bool(data.get("skip_send", False)),
            notes=data.get("notes", ""),
            slots_used=bool(data.get("slots_used", False)),
        )


class Drafter:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-6",
    ):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY env var not set")
        # max_retries: ride through transient "Overloaded" (529) spikes.
        self.client = anthropic.Anthropic(api_key=key, max_retries=6)
        self.model = model
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def draft(
        self,
        reply: Message,
        classification: Classification,
        original_outreach: Message | None,
        prospect: Prospect | None,
        style_guide: str = "",
        proposed_slots: list[str] | None = None,
        silent_on_not_interested: bool = True,
    ) -> Draft:
        """Generate a reply draft.

        style_guide is the full content of training_examples.md — the drafter
        parses what it needs (voice, entity-level signature/backup link,
        universal pairs, principle examples).
        """
        clean_reply = _trim_quoted_history(_strip_html(reply.body))
        original_text = ""
        original_signature_hint = "(no cold mail context available)"
        if original_outreach:
            original_text = _trim_quoted_history(_strip_html(original_outreach.body), max_len=2500)
            # Pull the last ~300 chars as signature hint — that's where the entity name lives
            tail = original_text[-300:] if len(original_text) > 300 else original_text
            original_signature_hint = tail

        prospect_info = "(prospect data unavailable)"
        if prospect:
            parts = [
                f"firstName: {prospect.first_name or '?'}",
                f"lastName: {prospect.last_name or '?'}",
                f"company: {prospect.company or '?'}",
                f"jobPosition: {prospect.job_position or '?'}",
                f"industry: {prospect.industry or '?'}",
                f"website: {prospect.website or '?'}",
            ]
            prospect_info = "\n".join(parts)

        slots_text = "(aucun créneau Calendar fourni — utilise le pattern 'voici 3 créneaux la semaine prochaine')"
        if proposed_slots:
            slots_text = "\n".join(f"- {s}" for s in proposed_slots)

        user_content = f"""## Contexte pour le draft

### Intent classifié
- intent: {classification.intent}
- confidence: {classification.confidence}
- key_phrase: {classification.key_phrase}
- redirected_to: {classification.redirected_to or 'null'}
- redirected_email: {classification.redirected_email or 'null'}
- language: {classification.language}

### Mail cold initial envoyé (source de vérité pour l'offre + l'entité + le next step)
Subject: {original_outreach.subject if original_outreach else '(unknown)'}
From: {original_outreach.from_email if original_outreach else '(unknown)'}
---
{original_text or '(non disponible)'}
---
Tail (signature hint pour détecter l'entité) :
{original_signature_hint}

### Reply reçu du prospect
From: {reply.from_email}
Subject: {reply.subject}
---
{clean_reply}

### Données prospect
{prospect_info}

### Créneaux disponibles (Calendar)
{slots_text}

### Config runtime
- silent_on_not_interested: {silent_on_not_interested}

### Style guide (voice + entités + training pairs — parse ce dont tu as besoin)
{style_guide or '(no style guide provided)'}

---

Rédige maintenant la réponse selon les règles du system prompt. Réponds en JSON uniquement.
"""

        msg = self.client.messages.create(
            model=self.model,
            max_tokens=3000,
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
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```\s*$", "", raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: the model occasionally returns prose with literal
            # newlines/quotes that break strict JSON, or a (rare) truncated
            # response. Recover the key fields with tolerant regex so the run
            # doesn't lose the draft.
            data = _salvage_draft_json(raw)
            if data is None:
                raise ValueError(f"Drafter returned unparseable output: {raw[:500]}")
        return Draft.from_json(data)
