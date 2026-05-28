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


@dataclass(frozen=True)
class Draft:
    body_html: str | None
    subject: str | None
    skip_send: bool
    notes: str

    @classmethod
    def from_json(cls, data: dict) -> "Draft":
        return cls(
            body_html=data.get("body_html"),
            subject=data.get("subject"),
            skip_send=bool(data.get("skip_send", False)),
            notes=data.get("notes", ""),
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
        self.client = anthropic.Anthropic(api_key=key)
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
            max_tokens=1200,
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
        except json.JSONDecodeError as e:
            raise ValueError(f"Drafter returned non-JSON: {raw[:500]}") from e
        return Draft.from_json(data)
