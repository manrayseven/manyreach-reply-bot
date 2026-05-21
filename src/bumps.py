"""Follow-up (bump) engine — relance les prospects intéressés sans réponse.

Stateless : l'état de relance est DÉDUIT de l'historique du thread ManyReach,
pas stocké séparément (compatible Vercel/serverless).

Logique par prospect "Interested" / "MaybeLater" :
- On regarde le thread.
- Si le DERNIER message est un Reply du prospect → ce n'est pas une relance,
  c'est une réponse à traiter (le bot principal s'en occupe). On saute.
- Si le dernier message est un Sent/SentManual de nous → on a parlé en dernier.
  On compte les Sent consécutifs en fin de thread = nombre de relances déjà faites.
  - 1 Sent (notre 1re réponse) + >= bump_days[0] jours → on envoie la relance #1
  - 2 Sent + >= bump_days[1] jours (cumulés) → relance #2
  - >= 3 Sent → stop (on archive en nurture, plus de relance)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .manyreach import ManyReachClient, Message, Prospect


@dataclass
class BumpDecision:
    should_bump: bool
    bump_number: int          # 1 ou 2
    reason: str
    body_html: str | None = None


# Templates de relance (courts, légers, jamais lourds).
# {slots} sera remplacé par les créneaux concrets si dispo, sinon une question ouverte.
BUMP_1 = (
    "<p>Bonjour{name},</p>"
    "<p>Je me permets de revenir vers vous rapidement — est-ce toujours d'actualité de votre côté ?</p>"
    "<p>{slots}</p>"
    "<p>Bien à vous,<br>Rudy Viard<br>Fondateur Webmarketing Conseil</p>"
)
BUMP_2 = (
    "<p>Bonjour{name},</p>"
    "<p>Dernier message de ma part pour ne pas vous importuner. Si le timing n'est pas le bon, "
    "aucun souci — dites-le moi et je vous laisse tranquille.</p>"
    "<p>Sinon, {slots}</p>"
    "<p>Bien à vous,<br>Rudy Viard<br>Fondateur Webmarketing Conseil</p>"
)


def _trailing_sent_count(thread: list[Message]) -> tuple[int, Message | None]:
    """Nombre de Sent/SentManual consécutifs en fin de thread + le dernier message."""
    if not thread:
        return 0, None
    ordered = sorted(thread, key=lambda m: m.created_at)
    last = ordered[-1]
    count = 0
    for m in reversed(ordered):
        if m.type in ("Sent", "SentManual"):
            count += 1
        else:
            break
    return count, last


def decide_bump(
    thread: list[Message],
    now: datetime,
    bump_days: list[int],
) -> BumpDecision:
    trailing_sent, last = _trailing_sent_count(thread)
    if last is None:
        return BumpDecision(False, 0, "thread vide")
    if last.type == "Reply":
        return BumpDecision(False, 0, "le prospect a parlé en dernier — réponse à traiter, pas relance")
    days_since = (now - last.created_at).total_seconds() / 86400.0
    if trailing_sent >= 3:
        return BumpDecision(False, 0, "déjà 3 messages sans réponse — stop (nurture)")
    if trailing_sent == 1:
        if days_since >= bump_days[0]:
            return BumpDecision(True, 1, f"1re réponse il y a {days_since:.1f}j, relance #1")
        return BumpDecision(False, 1, f"trop tôt pour relance #1 ({days_since:.1f}j < {bump_days[0]}j)")
    if trailing_sent == 2:
        if days_since >= (bump_days[1] - bump_days[0]):
            return BumpDecision(True, 2, f"relance #1 il y a {days_since:.1f}j, relance #2")
        return BumpDecision(False, 2, f"trop tôt pour relance #2 ({days_since:.1f}j)")
    return BumpDecision(False, 0, f"{trailing_sent} Sent en fin de thread, cas non géré")


def build_bump_body(bump_number: int, prospect: Prospect | None, slots_text: str | None) -> str:
    name = ""
    if prospect and prospect.first_name and not prospect.first_name[:4].isdigit():
        name = f" {prospect.first_name}"
    if slots_text:
        slots = f"Êtes-vous disponible {slots_text} ? Sur quel numéro vous rappeler ?"
    else:
        slots = "êtes-vous disponible cette semaine ou la suivante pour 15 min ? Sur quel numéro vous rappeler ?"
    template = BUMP_1 if bump_number == 1 else BUMP_2
    return template.format(name=name, slots=slots)
