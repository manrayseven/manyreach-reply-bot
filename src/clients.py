"""Multi-clients : router une réponse reçue vers le bon compte client.

Contexte : Rudy fait tourner des campagnes ManyReach pour PLUSIEURS clients
depuis un même compte ManyReach. Chaque réponse reçue doit être rattachée au
bon client pour (a) répondre aux négatifs avec la bonne offre/voix, (b) alerter
et "mettre en relation" le bon client.

Contrainte clé (voulue par Rudy) : NE PAS maintenir la liste EXHAUSTIVE des
adresses d'envoi et des campagnes de chaque client. On part de QUELQUES exemples
seed, et le système :
  1. route sur la MAILBOX D'ENVOI (signal le plus stable : chaque client a ses
     adresses d'envoi dédiées, elles changent rarement) ;
  2. à défaut, sur la CAMPAGNE ;
  3. à défaut, laisse Claude deviner d'après le cold mail + la description du
     client ; si pas sûr → `needs_triage` = Rudy tranche dans le dashboard, et
     le système APPREND (mémorise la mailbox/campagne pour ce client).

Ce module ne fait AUCUN I/O (ni KV ni réseau) sauf `infer_client_llm` qui est
optionnel et explicitement appelé. La persistance vit dans kvstore.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Un "client" = dict JSON-friendly. Champs :
#   id            slug unique (ex. "rudy", "client-durand")
#   name          nom affiché
#   description   ce que le client vend / son offre (sert au fallback LLM)
#   contact_email email où envoyer le récap "Mettre en relation" (vide pour soi)
#   mailboxes     adresses d'envoi ManyReach connues (seed + apprises)
#   campaigns     ids de campagnes connus (seed + appris)
#   offer_context instructions drafter : qui est le client, quoi pitcher aux
#                 négatifs, quels liens de valeur. Vide → comportement par défaut.
#   signature     bloc signature à utiliser dans les réponses (optionnel)
#   is_default    True pour le client "moi" (Rudy) = fallback quand rien ne matche


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().strip()).strip("-")
    return s or "client"


def normalize_mailbox(email: str) -> str:
    return (email or "").lower().strip()


def normalize_campaign(cid: Any) -> str:
    return str(cid or "").strip()


def default_client(clients: list[dict]) -> dict | None:
    """Le client par défaut (is_default), sinon le premier de la liste."""
    if not clients:
        return None
    for c in clients:
        if c.get("is_default"):
            return c
    return clients[0]


def _mailboxes(c: dict) -> set[str]:
    return {normalize_mailbox(m) for m in (c.get("mailboxes") or []) if m}


def _campaigns(c: dict) -> set[str]:
    return {normalize_campaign(x) for x in (c.get("campaigns") or []) if str(x).strip()}


def route_by_known(
    clients: list[dict],
    sender_mailbox: str | None,
    campaign_id: str | None,
) -> tuple[dict | None, str]:
    """Route sur les signaux CONNUS (mailbox puis campagne). Pur, pas d'I/O.

    Renvoie (client | None, raison). raison ∈ {mailbox, campaign, ""}.
    La mailbox prime : c'est le signal le plus stable et le moins ambigu.
    """
    mb = normalize_mailbox(sender_mailbox or "")
    cid = normalize_campaign(campaign_id or "")
    if mb:
        for c in clients:
            if mb in _mailboxes(c):
                return c, "mailbox"
    if cid:
        for c in clients:
            if cid in _campaigns(c):
                return c, "campaign"
    return None, ""


def needs_llm(clients: list[dict]) -> bool:
    """Vrai seulement s'il y a AU MOINS 2 clients : avec un seul client, tout
    lui revient (pas de tri à faire) → on n'appelle jamais le LLM."""
    return len([c for c in clients if c.get("id")]) >= 2


def route(
    clients: list[dict],
    sender_mailbox: str | None,
    campaign_id: str | None,
) -> dict:
    """Routage complet SANS LLM. Renvoie un dict :
      { client, client_id, reason, needs_triage }

    - match mailbox/campagne connu → client, needs_triage False.
    - sinon, s'il n'y a qu'un client → le client par défaut, needs_triage False.
    - sinon (2+ clients, rien ne matche) → client par défaut proposé mais
      needs_triage True (le LLM ou Rudy doit trancher).
    """
    c, reason = route_by_known(clients, sender_mailbox, campaign_id)
    if c is not None:
        return {"client": c, "client_id": c.get("id"), "reason": reason, "needs_triage": False}
    dflt = default_client(clients)
    if not needs_llm(clients):
        # 0 ou 1 client → pas de tri possible/nécessaire.
        return {"client": dflt, "client_id": (dflt or {}).get("id"), "reason": "default", "needs_triage": False}
    # 2+ clients et aucun signal connu → à trier (le LLM peut aider en amont).
    return {"client": dflt, "client_id": (dflt or {}).get("id"), "reason": "unknown", "needs_triage": True}


def learn(client: dict, sender_mailbox: str | None, campaign_id: str | None) -> dict:
    """Renvoie une COPIE du client enrichie de la mailbox/campagne apprise
    (dédupliquée). Ne mute pas l'original. Utilisé après un match LLM confiant
    ou une assignation manuelle de Rudy → auto-expansion sans maintenance."""
    c = dict(client)
    mb = normalize_mailbox(sender_mailbox or "")
    cid = normalize_campaign(campaign_id or "")
    if mb:
        mbs = list(c.get("mailboxes") or [])
        if mb not in {normalize_mailbox(x) for x in mbs}:
            mbs.append(mb)
            c["mailboxes"] = mbs
    if cid:
        cps = list(c.get("campaigns") or [])
        if cid not in {normalize_campaign(x) for x in cps}:
            cps.append(cid)
            c["campaigns"] = cps
    return c


def infer_client_llm(
    anthropic_client,
    clients: list[dict],
    original_outreach_text: str,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[str | None, float]:
    """Devine le client d'après le TEXTE du cold mail + les descriptions clients.

    Appelé UNIQUEMENT quand il y a 2+ clients et qu'aucun signal connu (mailbox/
    campagne) ne matche. Renvoie (client_id | None, confidence 0..1). None si le
    modèle n'est pas sûr → l'alerte partira "à trier".
    """
    cand = [c for c in clients if c.get("id")]
    if not cand or not (original_outreach_text or "").strip():
        return None, 0.0
    listing = "\n".join(
        f'- id="{c["id"]}" | nom="{c.get("name","")}" | offre: {c.get("description","")}'
        for c in cand
    )
    sys = (
        "Tu tries des réponses à des cold emails par CLIENT. On te donne le texte "
        "du COLD MAIL initial (envoyé au prospect) et la liste des clients avec la "
        "description de leur offre. Déduis quel client a envoyé ce cold mail (offre "
        "pitchée, ton, signature, liens). Réponds UNIQUEMENT en JSON: "
        '{"client_id": "<id ou null>", "confidence": 0.0-1.0}. '
        "Mets null si tu n'es pas raisonnablement sûr (confidence < 0.7)."
    )
    user = f"### Clients\n{listing}\n\n### Cold mail initial\n{original_outreach_text[:2000]}"
    try:
        msg = anthropic_client.messages.create(
            model=model,
            max_tokens=120,
            system=sys,
            messages=[{"role": "user", "content": user}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
        data = json.loads(raw)
        cid = data.get("client_id")
        conf = float(data.get("confidence") or 0.0)
        valid = {c["id"] for c in cand}
        if cid in valid and conf >= 0.7:
            return cid, conf
        return None, conf
    except Exception:  # noqa: BLE001
        return None, 0.0


def build_client_draft_context(client: dict | None) -> str:
    """Bloc d'identité injecté au drafter pour les réponses NÉGATIVES d'un client.
    Vide pour le client par défaut sans offer_context (→ comportement historique
    inchangé). Sinon décrit qui est le client et quoi pitcher."""
    if not client:
        return ""
    if client.get("is_default") and not (client.get("offer_context") or "").strip():
        return ""  # Rudy sans surcharge → on garde draft.md/training tels quels
    parts = []
    oc = (client.get("offer_context") or "").strip()
    if oc:
        parts.append(oc)
    sig = (client.get("signature") or "").strip()
    if sig:
        parts.append(f"Signature à utiliser :\n{sig}")
    if not parts:
        return ""
    body = "\n\n".join(parts)
    return (
        "## IDENTITÉ CLIENT POUR CETTE RÉPONSE (PRIORITAIRE)\n"
        f"Cette réponse est envoyée au nom du client « {client.get('name','')} ». "
        "Réponds dans SA voix et pour SON offre décrite ci-dessous. Ignore toute "
        "autre offre (ex. GrowPulser) qui ne correspondrait pas à ce client.\n\n"
        f"{body}"
    )
