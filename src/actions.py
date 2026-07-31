"""Map a classified intent into concrete ManyReach API actions.

This module is the contract between "what the classifier said" and "what we
actually do" — kept separate so it's easy to audit and to tweak the mapping
without touching the classifier or drafter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .classifier import Classification
from .drafter import Draft
from .manyreach import ManyReachClient, Message, Prospect

# Map intent -> (sendingStatus | None, sendingActive | None, [tags to add])
# A None status/active means "do NOT change the prospect's status" — only tag.
INTENT_PROSPECT_UPDATE: dict[str, tuple[str | None, bool | None, list[str]]] = {
    # meeting_confirmed : le bot NE POSE PLUS "MeetingBooked" lui-même. Il ne
    # détecte qu'un SIGNAL de RDV (le prospect propose/accepte un créneau) — ce
    # n'est pas un RDV confirmé, et Rudy cale/valide les RDV à la main. Poser
    # MeetingBooked automatiquement était dangereux : une mauvaise classif (refus
    # avec pitch cité en dessous → faux meeting) verrouillait le prospect en
    # "booké" ET le sortait du traitement du bot (cas antedi, remialgis, racine,
    # 31/07). On le traite donc comme un LEAD CHAUD (Interested) + alerte ; Rudy
    # passe en MeetingBooked lui-même quand le RDV est réellement calé.
    "meeting_confirmed":               ("Interested",     True,  ["bot:meeting-signal"]),
    "interested_warm":                 ("Interested",     True,  ["bot:hot-lead"]),
    "interested_lukewarm":             ("Neutral",        True,  ["bot:lukewarm"]),
    "objection_price":                 ("MaybeLater",     True,  ["bot:price-objection"]),
    "objection_timing":                ("MaybeLater",     True,  ["bot:timing-objection"]),
    "objection_already_have_solution": ("NotInterested",  False, ["bot:has-solution"]),
    "wrong_person_redirect":           ("NotInterested",  False, ["bot:wrong-person"]),
    "ask_more_info":                   ("Neutral",        True,  ["bot:info-requested"]),
    "not_interested_polite":           ("NotInterested",  False, ["bot:not-interested"]),
    "unsubscribe":                     ("Unsub",          False, ["bot:unsub"]),
    "hostile":                         ("Unsub",          False, ["bot:hostile"]),
    # bounce_or_auto: do NOT touch status. ManyReach detects its own real bounces;
    # the bot would only risk wrongly archiving a valid prospect (empty replies,
    # auto-replies from working mailboxes, etc.). Just tag for audit, send nothing.
    "bounce_or_auto":                  (None,             None,  ["bot:bounce-or-auto"]),
    # ack_only = remerciement seul → on ne touche RIEN (pas de status update,
    # pas de blacklist, juste un tag d'audit). Le prospect reste à son statut.
    "ack_only":                        (None,             None,  ["bot:ack-only"]),
}

# Intents eligible for auto-send.
# Phase 1 strategy: REVIEW EVERYTHING. The goal of the first weeks is for Rudy
# to validate draft quality on 50-100 real cases. Once trust is established,
# re-enable auto-send progressively on the safe intents (wrong_person_redirect,
# objection_timing, not_interested_polite) by uncommenting them below.
# 100% AUTONOME (validé par Rudy 2026-05-21) : le bot envoie TOUS les intents
# "réponse". Garde-fous conservés : confidence >= min_autosend_confidence (sinon
# review), et interested_warm n'auto-send que si des créneaux Calendar sont dispo.
# NOUVEAU CADRE (validé par Rudy 2026-06-03) :
#  - AUTOSEND : le bot répond automatiquement (refus polis + objections à travailler).
#  - ALERT_ONLY : le bot ENVOIE UNE ALERTE EMAIL à contact@webmarketing-conseil.fr,
#    PAS de réponse auto. Rudy gère lui-même (RDV, leads chauds, "plus tard").
#  - ALWAYS_SILENT : action silencieuse (blacklist/tag) sans email.
AUTOSEND_ELIGIBLE = frozenset({
    "not_interested_polite",
    "objection_already_have_solution",
    "objection_price",  # objection à travailler / convaincre
})

# Intents qui DÉCLENCHENT UNE ALERTE EMAIL à Rudy (pas de réponse auto).
# Rudy traite manuellement : leads chauds, RDV, demandes d'info, et "plus tard"
# (= recontact futur qu'il veut piloter lui-même).
ALERT_ONLY = frozenset({
    "interested_warm",
    "interested_lukewarm",
    "ask_more_info",
    "meeting_confirmed",
    "objection_timing",
})

# Intents that NEVER send a reply (silent action only).
# wrong_person_redirect (qq'un qui a quitté la boîte / changement d'adresse /
# autoreply de congés) : Rudy ne veut PAS d'alerte ni de réponse polie automatique.
# Le bot tag, passe en NotInterested côté MR, et c'est fini.
ALWAYS_SILENT = frozenset({"unsubscribe", "hostile", "bounce_or_auto", "ack_only", "wrong_person_redirect"})


ActionKind = Literal[
    "update_prospect",
    "add_tag",
    "blacklist_email",
    "send_reply",
    "skip_send",
    "needs_review",
    "mailinblack_pending",
]


@dataclass
class PlannedAction:
    kind: ActionKind
    payload: dict
    description: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.description}"


@dataclass
class ActionPlan:
    classification: Classification
    draft: Draft | None
    actions: list[PlannedAction] = field(default_factory=list)
    auto_send: bool = False
    review_reason: str = ""


def plan_mailinblack_actions(
    *,
    reply: Message,
    prospect: Prospect | None,
) -> ActionPlan:
    """Special-case handler for MailInBlack validation challenges.

    Until we have IMAP/Gmail access to extract the validation URL, we just tag
    the prospect for a manual click and log clearly which mailbox Rudy needs to
    visit. The prospect status is NOT changed to BounceHard (their mailbox is
    fine — they're just behind a MailInBlack gate).
    """
    from .classifier import Classification

    fake_cls = Classification(
        intent="mailinblack_pending",
        confidence=1.0,
        key_phrase="MailInBlack validation requise",
        redirected_email=None,
        redirected_to=None,
        language="fr",
        reasoning="Pre-filtered as MailInBlack challenge — needs manual click validation",
    )
    plan = ActionPlan(classification=fake_cls, draft=None)
    if prospect is not None:
        plan.actions.append(
            PlannedAction(
                kind="add_tag",
                payload={"prospect_id": prospect.prospect_id, "tag_title": "bot:mailinblack-pending-click"},
                description=(
                    f"Tag prospect {prospect.prospect_id} ({prospect.email}) "
                    f"as mailinblack-pending-click"
                ),
            )
        )
    plan.actions.append(
        PlannedAction(
            kind="mailinblack_pending",
            payload={
                "challenge_sender": reply.from_email,
                "destination_mailbox": reply.to_email,
                "subject": reply.subject,
            },
            description=(
                f"MAILINBLACK: clic manuel requis dans la boîte {reply.to_email} "
                f"(challenge envoyé par {reply.from_email})"
            ),
        )
    )
    plan.auto_send = True  # silent action — no reply, just tag + flag
    return plan


def plan_actions(
    *,
    reply: Message,
    prospect: Prospect | None,
    classification: Classification,
    draft: Draft | None,
    min_autosend_confidence: float = 0.92,
    has_calendar_slots: bool = False,
) -> ActionPlan:
    """Decide the full set of actions to take, plus whether to auto-send or queue for review."""
    plan = ActionPlan(classification=classification, draft=draft)
    intent = classification.intent

    mapping = INTENT_PROSPECT_UPDATE.get(intent)
    if mapping is None:
        plan.actions.append(
            PlannedAction(
                kind="needs_review",
                payload={"reason": f"unknown intent {intent!r}"},
                description=f"Unknown intent {intent!r} — review required",
            )
        )
        plan.review_reason = f"unknown intent {intent!r}"
        return plan

    sending_status, sending_active, tags = mapping

    if prospect is not None:
        # Only PATCH the prospect status when the mapping actually specifies one.
        # (bounce_or_auto uses None/None → tag only, never change status.)
        if sending_status is not None or sending_active is not None:
            plan.actions.append(
                PlannedAction(
                    kind="update_prospect",
                    payload={
                        "prospect_id": prospect.prospect_id,
                        "sending_status": sending_status,
                        "sending_active": sending_active,
                    },
                    description=(
                        f"PATCH prospect {prospect.prospect_id} "
                        f"({prospect.email}) → status={sending_status}, active={sending_active}"
                    ),
                )
            )
        for tag_title in tags:
            plan.actions.append(
                PlannedAction(
                    kind="add_tag",
                    payload={"prospect_id": prospect.prospect_id, "tag_title": tag_title},
                    description=f"Add tag {tag_title!r} to prospect {prospect.prospect_id}",
                )
            )

    if intent in ALWAYS_SILENT:
        if intent in ("unsubscribe", "hostile"):
            plan.actions.append(
                PlannedAction(
                    kind="blacklist_email",
                    payload={"emails": [reply.from_email]},
                    description=f"Blacklist {reply.from_email} (RGPD/safety)",
                )
            )
        plan.actions.append(
            PlannedAction(
                kind="skip_send",
                payload={"reason": f"intent {intent} is always silent"},
                description=f"No reply sent for intent {intent}",
            )
        )
        plan.auto_send = True  # silent = always "safe to execute"
        return plan

    if draft is None or draft.skip_send:
        plan.actions.append(
            PlannedAction(
                kind="skip_send",
                payload={"reason": "drafter returned skip_send"},
                description="Drafter requested skip_send",
            )
        )
        plan.auto_send = True
        return plan

    can_autosend = (
        intent in AUTOSEND_ELIGIBLE
        and classification.confidence >= min_autosend_confidence
        and bool(draft.body_html)
    )
    # meeting_confirmed = simple accusé "c'est noté, je vous recontacte au [num]
    # à [heure] pile" → on l'envoie TOUJOURS dès qu'il y a un body, sans gate de
    # confidence (intent à très faible risque, et c'est ce que le prospect attend).
    # Bonus crucial : garantir cet envoi déclenche l'idempotence (un Sent apparaît
    # dans le thread) → plus de re-traitement à chaque run → plus de doublons d'event.
    if intent == "meeting_confirmed":
        can_autosend = bool(draft.body_html)
    # Note historique : on bloquait l'auto-send d'interested_warm sans créneaux
    # Calendar. Rudy veut 100% autonome → on autorise quand même (le drafter
    # produit une CTA douce "vous êtes dispo cette semaine ?" qui marche très
    # bien sans créneaux concrets). Pas de gate ici.

    if not can_autosend and not plan.review_reason:
        plan.review_reason = (
            f"confidence {classification.confidence:.2f} < {min_autosend_confidence}"
            if classification.confidence < min_autosend_confidence
            else f"intent {intent} pas dans auto-send allow-list"
        )

    plan.actions.append(
        PlannedAction(
            kind="send_reply",
            payload={
                "message_id": reply.message_id,
                "body_html": draft.body_html,
                "subject": draft.subject,
            },
            description=(
                f"reply to {reply.from_email} ({len(draft.body_html or '')} chars HTML)"
            ),
        )
    )
    plan.auto_send = can_autosend
    return plan


def execute_plan(
    plan: ActionPlan,
    reply: Message,
    client: ManyReachClient,
    *,
    dry_run: bool = True,
    tag_cache: dict[str, int] | None = None,
) -> list[str]:
    """Execute the planned actions. Returns a list of human-readable result lines."""
    tag_cache = tag_cache if tag_cache is not None else {}
    results: list[str] = []
    for action in plan.actions:
        prefix = "[DRY-RUN]" if dry_run else "[EXEC]"
        if action.kind == "update_prospect":
            if not dry_run:
                client.update_prospect(
                    action.payload["prospect_id"],
                    sending_status=action.payload["sending_status"],
                    sending_active=action.payload["sending_active"],
                )
            results.append(f"{prefix} {action.description}")
        elif action.kind == "add_tag":
            tag_title = action.payload["tag_title"]
            if tag_title not in tag_cache:
                if dry_run:
                    tag_id = -1
                else:
                    tag_id = client.ensure_tag(tag_title)
                tag_cache[tag_title] = tag_id
            tag_id = tag_cache[tag_title]
            if not dry_run:
                try:
                    client.add_prospect_tag(action.payload["prospect_id"], tag_id)
                except Exception as e:
                    # 409 = already has this tag, ignore
                    if "409" not in str(e):
                        raise
            results.append(f"{prefix} {action.description} (tagId={tag_id})")
        elif action.kind == "blacklist_email":
            if not dry_run:
                client.blacklist_emails(action.payload["emails"])
            results.append(f"{prefix} {action.description}")
        elif action.kind == "send_reply":
            if not plan.auto_send:
                results.append(
                    f"[À RELIRE — non envoyé] {action.description} — {plan.review_reason}"
                )
                continue
            if not dry_run:
                _mid = action.payload["message_id"]
                _kv = None
                try:
                    from src import kvstore as _kv  # noqa: PLC0415
                except Exception:  # noqa: BLE001
                    _kv = None
                # GARDE-FOU 1 (permanent) : a-t-on DÉJÀ répondu à ce reply ?
                # Indépendant de l'indexation ManyReach → bloque tout 2e envoi
                # même si le Sent n'est pas encore visible dans le thread.
                if _kv is not None and _kv.kv_available() and _kv.already_replied(_mid):
                    results.append(f"[DÉJÀ RÉPONDU] {_mid} — skip (anti-double-envoi)")
                    continue
                # GARDE-FOU 2 (mutex court) : verrou exclusif pendant l'envoi pour
                # les runs concurrents (cron + clic manuel simultanés).
                if _kv is not None and _kv.kv_available() and not _kv.acquire_send_lock(_mid):
                    results.append(f"[DUPLICATE-LOCK] envoi en cours pour {_mid} — skip")
                    continue
                client.send_reply(
                    message_id=_mid,
                    body_html=action.payload["body_html"],
                    subject=action.payload.get("subject"),
                    send_as_reply=True,
                )
                # GARDE-FOU 3 : on marque "répondu" (permanent 30j) APRÈS succès.
                if _kv is not None and _kv.kv_available():
                    try:
                        _kv.mark_replied(_mid)
                    except Exception:  # noqa: BLE001
                        pass
                results.append(f"[ENVOYÉ ✓] {action.description}")
            else:
                results.append(f"[DRY-RUN] ENVERRAIT {action.description}")
        elif action.kind == "skip_send":
            results.append(f"{prefix} {action.description}")
        elif action.kind == "needs_review":
            results.append(f"[REVIEW] {action.description}")
        elif action.kind == "mailinblack_pending":
            # No API write — this is just a flag for Rudy. The tag is added via
            # a separate add_tag action queued by plan_mailinblack_actions.
            results.append(f"{prefix} {action.description}")
    return results
