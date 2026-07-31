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


# Intents "simples" → réponse courte/templatée → Haiku (5-10x moins cher).
# Les intents à ENJEU (prospects chauds, RDV, demande d'info, objection prix)
# restent sur le modèle qualité (Sonnet).
SIMPLE_DRAFT_INTENTS = frozenset({
    "not_interested_polite",
    "wrong_person_redirect",
    "objection_already_have_solution",
    "objection_timing",
})


class Drafter:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-6",
        simple_model: str | None = None,
    ):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY env var not set")
        # max_retries: ride through transient "Overloaded" (529) spikes.
        self.client = anthropic.Anthropic(api_key=key, max_retries=6)
        self.model = model
        # Modèle éco pour les intents simples (défaut Haiku 4.5). Override possible
        # via settings.yaml > models.drafter_simple.
        self.simple_model = simple_model or "claude-haiku-4-5-20251001"
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
        company_context: str = "",
        client_context: str = "",
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
                f"sendingStatus (AVANT ce reply): {prospect.sending_status or '?'}",
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

### Contexte entreprise (extrait du SITE WEB du prospect — pour personnaliser)
{company_context or '(pas de contexte site — réponds sans, ne fais référence à rien d''extérieur)'}

RÈGLES sur ce contexte (anti-cringe / anti-"IA") :
- Utilise-le SEULEMENT s'il apporte un détail VRAIMENT pertinent et exact (ex. leur secteur,
  une spécificité claire de leur activité). Sinon, ignore-le complètement.
- UN seul détail intégré naturellement, jamais 2-3 (ça sonne robot/stalker).
- Ne dis JAMAIS "j'ai visité votre site / scanné votre profil". Glisse le détail
  naturellement comme si tu connaissais le secteur.
- ZÉRO invention : si tu n'es pas sûr à 100% d'un fait, ne le mentionne pas.
- En cas de doute, reste générique : mieux vaut sobre que faux.

---

Rédige maintenant la réponse selon les règles du system prompt et du style guide. Réponds en JSON uniquement.
"""

        # OPTIMISATION COÛT : le style guide (gros, ~quasi statique) est mis dans
        # le bloc SYSTEM avec cache_control → mis en cache par l'API Claude (lectures
        # à 10% du prix). Avant il était dans le message user = renvoyé plein tarif à
        # CHAQUE draft. Idem pour le system prompt. Le message user ne contient plus
        # que le contexte spécifique à ce reply (petit).
        # Routage modèle : Haiku (éco) pour les intents simples, Sonnet (qualité)
        # pour les prospects à enjeu. La majorité des replies étant des refus,
        # ça réduit fortement le coût sans toucher à la qualité des leads chauds.
        chosen_model = (
            self.simple_model
            if classification.intent in SIMPLE_DRAFT_INTENTS
            else self.model
        )
        system_blocks = [
            {
                "type": "text",
                "text": self.system_prompt,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "## STYLE GUIDE (voix + entités + training pairs)\n"
                + (style_guide or "(no style guide provided)"),
                "cache_control": {"type": "ephemeral"},
            },
        ]
        # Bloc identité CLIENT (multi-clients) : quand la réponse part au nom d'un
        # client ayant sa propre offre, ce bloc prime sur le contexte GrowPulser
        # par défaut. Vide pour le client "moi" sans surcharge → inchangé.
        if (client_context or "").strip():
            system_blocks.append({
                "type": "text",
                "text": client_context,
                "cache_control": {"type": "ephemeral"},
            })
        msg = self.client.messages.create(
            model=chosen_model,
            max_tokens=3000,
            system=system_blocks,
            messages=[{"role": "user", "content": user_content}],
        )
        return self._parse(msg.content[0].text.strip())

    def _parse(self, raw: str) -> Draft:
        """Parse la sortie JSON du modèle (tolérant) → Draft, + nettoyage tirets."""
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
        # Rudy ne veut JAMAIS de tirets longs (— / –) dans ses emails → remplace par "-".
        if data.get("body_html"):
            data["body_html"] = (
                data["body_html"].replace(" — ", " - ").replace("—", "-").replace("–", "-")
            )
        return Draft.from_json(data)

    def draft_polite_close(
        self,
        reply: Message,
        prospect: Prospect | None = None,
        original_outreach: Message | None = None,
        style_guide: str = "",
    ) -> Draft:
        """Réponse de CLÔTURE POLIE : Rudy a décidé de NE PAS donner suite à ce lead.
        Remercie + décline en douceur, AUCUN engagement (pas de RDV, question, relance).
        Déclenché manuellement depuis le dashboard (bouton "réponse auto")."""
        clean_reply = _trim_quoted_history(_strip_html(reply.body))
        orig = ""
        if original_outreach:
            orig = _trim_quoted_history(_strip_html(original_outreach.body), 1500)
        sys_prompt = (
            "Tu rédiges, dans la VOIX de Rudy (cf. STYLE GUIDE ci-dessous), une réponse "
            "de CLÔTURE POLIE à un prospect. Rudy a décidé de NE PAS donner suite à cet "
            "échange (lead non retenu).\n"
            "OBJECTIF : une réponse CONTEXTUELLE et NATURELLE, du même calibre que les "
            "réponses automatiques habituelles de Rudy — surtout PAS un texte générique "
            "qui sonne template/copié-collé.\n"
            "Règles STRICTES :\n"
            "- RÉAGIS au contenu SPÉCIFIQUE de leur message (reprends naturellement ce "
            "qu'ils ont dit : un numéro donné, une dispo proposée, une info partagée, une "
            "absence annoncée…) pour que ça sonne écrit pour eux, pas un copier-coller.\n"
            "- Décline en douceur : on ne va pas donner suite / ça ne va pas matcher de "
            "notre côté pour le moment. Sans justification lourde, sans excuse exagérée, "
            "sans dénigrer, sans promettre de revenir vers eux.\n"
            "- AUCUN rendez-vous, AUCUN créneau proposé, AUCUNE question, AUCUNE relance, "
            "AUCUN lien. (Même si le prospect propose un appel/un créneau : on remercie "
            "mais on n'enchaîne pas dessus.)\n"
            "- 2 à 4 phrases. Ton humain, chaleureux, sobre, varié (pas deux fois la même "
            "tournure d'un prospect à l'autre).\n"
            "- Termine par la signature habituelle de Rudy (déduite du style guide / du "
            "mail initial).\n"
            "- JAMAIS de tiret long (— ou –).\n"
            "- Langue = celle du reply du prospect.\n"
            'Réponds UNIQUEMENT en JSON : {"body_html": "<HTML simple, <br> pour les '
            'sauts de ligne>", "subject": null, "skip_send": false, "notes": ""}'
        )
        user_content = (
            f"### Reply du prospect\nFrom: {reply.from_email}\nSubject: {reply.subject}\n"
            f"---\n{clean_reply}\n\n"
            f"### Mail initial envoyé (contexte + voix/signature)\n{orig or '(non disponible)'}\n"
        )
        msg = self.client.messages.create(
            model=self.simple_model,  # clôture simple → modèle éco
            max_tokens=1200,
            system=[
                {"type": "text", "text": sys_prompt, "cache_control": {"type": "ephemeral"}},
                {
                    "type": "text",
                    "text": "## STYLE GUIDE (voix + signature)\n" + (style_guide or "(none)"),
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        return self._parse(msg.content[0].text.strip())
