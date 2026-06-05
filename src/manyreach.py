"""ManyReach API v2 client.

Wraps the endpoints we use for reply handling. Auth via X-API-Key header.
Full OpenAPI spec is in manyreach_openapi.json at repo root.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

BASE_URL = "https://api.manyreach.com/api/v2"
DEFAULT_TIMEOUT = 30.0


class ManyReachError(Exception):
    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"ManyReach API {status} on {url}: {body[:200]}")


@dataclass(frozen=True)
class Message:
    message_id: str
    created_at: datetime
    type: str  # "Sent" | "Reply" | "SentManual"
    campaign_id: int
    followup_id: int
    from_email: str
    to_email: str
    subject: str
    body: str

    @classmethod
    def from_api(cls, d: dict) -> "Message":
        return cls(
            message_id=d["messageId"],
            created_at=datetime.fromisoformat(d["createdAt"].replace("Z", "+00:00")),
            type=d.get("type", ""),
            campaign_id=d.get("campaignId", 0),
            followup_id=d.get("followupId", 0),
            from_email=d.get("fromEmail", ""),
            to_email=d.get("toEmail", ""),
            subject=d.get("subject", ""),
            body=d.get("body", ""),
        )


@dataclass(frozen=True)
class Prospect:
    prospect_id: int
    email: str
    sending_status: str
    sending_active: bool
    first_name: str | None
    last_name: str | None
    company: str | None
    job_position: str | None
    industry: str | None
    website: str | None
    domain: str | None
    raw: dict

    @classmethod
    def from_api(cls, d: dict) -> "Prospect":
        return cls(
            prospect_id=d["prospectId"],
            email=d.get("email", ""),
            sending_status=d.get("sendingStatus", ""),
            sending_active=d.get("sendingActive", False),
            first_name=d.get("firstName") or None,
            last_name=d.get("lastName") or None,
            company=d.get("company") or None,
            job_position=d.get("jobPosition") or None,
            industry=d.get("industry") or None,
            website=d.get("website") or None,
            domain=d.get("domain") or None,
            raw=d,
        )


@dataclass(frozen=True)
class Tag:
    tag_id: int
    title: str


class ManyReachClient:
    def __init__(self, api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT):
        key = api_key or os.environ.get("MANYREACH_API_KEY")
        if not key:
            raise RuntimeError("MANYREACH_API_KEY env var not set")
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ManyReachClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        # Résilience au rate-limit ManyReach (60 req/min). Sur 429, on respecte
        # le Retry-After (ou un backoff progressif) et on réessaie, plutôt que
        # de faire planter tout le run (cause de replies non-envoyés quand l'API
        # est saturée par le cron + autres appels).
        import time as _t
        # Retry 429 COURT : 2 tentatives, 2s d'attente max. Le budget du run cron
        # est de ~22s → on ne peut PAS se permettre de dormir 15s (ça tuait le run
        # avant l'envoi). Si ça rate-limit encore après 1 retry court, on lève
        # l'erreur : le run suivant (cron 5 min) reprendra ce reply (FIFO).
        max_attempts = 2
        for attempt in range(max_attempts):
            resp = self._client.request(method, path, **kwargs)
            if resp.status_code == 429 and attempt < max_attempts - 1:
                _t.sleep(2.0)
                continue
            if resp.status_code >= 400:
                raise ManyReachError(resp.status_code, resp.text, str(resp.request.url))
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise ManyReachError(429, "rate limit — retry court épuisé", path)

    # ----- Messages / Replies -----

    # Statuses the BOT should process (draft a reply for). These are the human
    # replies that still need an action from the bot.
    #
    # Deliberately EXCLUDES MeetingBooked / MeetingCompleted / Won — Rudy handles
    # those manually (he moves Booked→Completed→proposal→Won himself). The bot
    # must NOT touch a prospect once a meeting is booked.
    IMPORTANT_STATUSES = (
        "Interested",      # → relancer/pousser vers un RDV
        "MaybeLater",      # → nurture
        "Neutral",         # → clarifier, faire avancer
        "NotInterested",   # → réponse polie + referral
        "CollegueReplied", # → un collègue a répondu, à traiter
    )

    def list_replies(
        self,
        campaign_id: int | None = None,
        since: datetime | None = None,
        page_size: int = 100,
        confirmed_statuses: tuple[str, ...] | None = None,
        max_per_status: int = 300,
        email_from: str | None = None,
    ) -> Iterable[Message]:
        """Yield reply Messages, most-recent first.

        If confirmed_statuses is given, only fetch replies ManyReach has tagged
        with one of those statuses (e.g. the "important" human replies) — this
        skips the bounce/auto-reply noise entirely. Otherwise fetch all replies.

        max_per_status caps how many we pull per status so we don't aspirate
        thousands of historical "NotInterested" just to process a handful.
        The ManyReach API caps `limit` at 100 per request.
        """
        page_size = min(page_size, 100)  # API hard cap

        # Targeted single-prospect fetch (used for the controlled live test).
        if email_from:
            page = 1
            while True:
                params = {
                    "type": "Reply",
                    "emailFrom": email_from,
                    "limit": page_size,
                    "page": page,
                }
                data = self._request("GET", "/messages", params=params)
                items = (data or {}).get("items", [])
                if not items:
                    return
                for item in items:
                    if item.get("type") != "Reply":
                        continue
                    msg = Message.from_api(item)
                    if since and msg.created_at < since:
                        continue
                    yield msg
                if len(items) < page_size:
                    return
                page += 1
            return

        if confirmed_statuses:
            seen: set[str] = set()
            collected: list[Message] = []
            for status in confirmed_statuses:
                fetched_for_status = 0
                page = 1
                while fetched_for_status < max_per_status:
                    params: dict[str, Any] = {
                        "type": "Reply",
                        "confirmedStatus": status,
                        "limit": page_size,
                        "page": page,
                    }
                    if campaign_id is not None:
                        params["campaignId"] = campaign_id
                    data = self._request("GET", "/messages", params=params)
                    items = (data or {}).get("items", [])
                    if not items:
                        break
                    for item in items:
                        if item.get("type") != "Reply":
                            continue
                        mid = item.get("messageId", "")
                        if mid in seen:
                            continue
                        msg = Message.from_api(item)
                        if since and msg.created_at < since:
                            continue
                        seen.add(mid)
                        collected.append(msg)
                    fetched_for_status += len(items)
                    if len(items) < page_size:
                        break
                    page += 1
            collected.sort(key=lambda m: m.created_at, reverse=True)
            yield from collected
            return

        params = {"type": "Reply", "limit": page_size, "page": 1}
        if campaign_id is not None:
            params["campaignId"] = campaign_id
        page = 1
        # Cap dur à 2 pages (200 replies les plus récents). Le listing est la
        # phase la plus lente du run (chaque page = 1 appel + risque 429). 2 pages
        # = ~3s, ça tient dans le budget cron. Les replies au-delà des 200 plus
        # récents (rare) sont rattrapés par le bouton "Pour cet email".
        max_pages = int(os.environ.get("LIST_MAX_PAGES", "2"))
        while page <= max_pages:
            params["page"] = page
            data = self._request("GET", "/messages", params=params)
            items = (data or {}).get("items", [])
            if not items:
                return
            page_had_recent = False
            for item in items:
                if item.get("type") != "Reply":
                    continue
                msg = Message.from_api(item)
                if since and msg.created_at < since:
                    continue
                page_had_recent = True
                yield msg
            # ARRÊT ANTICIPÉ : ManyReach renvoie du plus récent au plus ancien.
            # Si une page entière est plus vieille que `since`, toutes les pages
            # suivantes le sont aussi → inutile de continuer (économie d'appels
            # API = moins de risque de rate-limit 429).
            if since and not page_had_recent:
                return
            if len(items) < page_size:
                return
            page += 1

    def get_prospect_thread(self, prospect_id: int) -> list[Message]:
        data = self._request("GET", f"/prospects/{prospect_id}/messages")
        items = (data or {}).get("items", [])
        return [Message.from_api(i) for i in items]

    def send_reply(
        self,
        message_id: str,
        body_html: str,
        subject: str | None = None,
        send_as_reply: bool = True,
        from_email: str | None = None,
    ) -> dict:
        """POST /messages/reply — sends an email reply on an existing thread.

        body_html: HTML content. Use <br>, <p>, etc.
        send_as_reply: when true, ManyReach appends the original quoted message.
        from_email: override sender (default: original sender of the thread).
        """
        payload: dict[str, Any] = {
            "messageId": message_id,
            "body": body_html,
            "sendAsReply": send_as_reply,
        }
        if subject:
            payload["subject"] = subject
        if from_email:
            payload["fromEmail"] = from_email
        return self._request("POST", "/messages/reply", json=payload)

    # ----- Prospects -----

    def list_prospects(
        self,
        email: str | None = None,
        page_size: int = 100,
    ) -> Iterable[Prospect]:
        params: dict[str, Any] = {"pageSize": page_size}
        if email:
            params["email"] = email
        offset = 0
        while True:
            params["offset"] = offset
            data = self._request("GET", "/prospects", params=params)
            items = (data or {}).get("items", [])
            if not items:
                return
            for item in items:
                yield Prospect.from_api(item)
            if len(items) < page_size:
                return
            offset += page_size

    def find_prospect_by_email(self, email: str) -> Prospect | None:
        """Locate a prospect by email. Tries the email filter first, falls back to scan."""
        for p in self.list_prospects(email=email, page_size=20):
            if p.email.lower() == email.lower():
                return p
        return None

    def get_prospect(self, prospect_id: int) -> Prospect:
        data = self._request("GET", f"/prospects/{prospect_id}")
        return Prospect.from_api(data)

    def update_prospect(
        self,
        prospect_id: int,
        *,
        sending_status: str | None = None,
        sending_active: bool | None = None,
        notes: str | None = None,
        **other: Any,
    ) -> dict:
        """PATCH /prospects/{id}. Only includes fields explicitly passed."""
        payload: dict[str, Any] = {}
        if sending_status is not None:
            payload["sendingStatus"] = sending_status
        if sending_active is not None:
            payload["sendingActive"] = sending_active
        if notes is not None:
            payload["notes"] = notes
        payload.update(other)
        return self._request("PATCH", f"/prospects/{prospect_id}", json=payload)

    # ----- Tags -----

    def list_tags(self) -> list[Tag]:
        data = self._request("GET", "/tags")
        items = (data or {}).get("items", [])
        return [Tag(tag_id=i["tagId"], title=i["title"]) for i in items]

    def create_tag(self, title: str, description: str | None = None) -> Tag:
        payload: dict[str, Any] = {"title": title}
        if description:
            payload["description"] = description
        data = self._request("POST", "/tags", json=payload)
        return Tag(tag_id=data["tagId"], title=data["title"])

    def ensure_tag(self, title: str, _cache: dict[str, int] | None = None) -> int:
        """Returns tagId, creating the tag if it doesn't exist."""
        for t in self.list_tags():
            if t.title.lower() == title.lower():
                return t.tag_id
        return self.create_tag(title).tag_id

    def add_prospect_tag(self, prospect_id: int, tag_id: int) -> None:
        self._request(
            "POST",
            f"/prospects/{prospect_id}/tags",
            json={"tagId": tag_id},
        )

    def remove_prospect_tag(self, prospect_id: int, tag_id: int) -> None:
        self._request("DELETE", f"/prospects/{prospect_id}/tags/{tag_id}")

    # ----- Blacklist (unsubscribe / suppression) -----

    def blacklist_emails(self, emails: list[str]) -> dict:
        return self._request(
            "POST",
            "/blacklist/emails",
            json={"emails": emails},
        )

    def is_blacklisted(self, email: str) -> bool:
        try:
            data = self._request("GET", "/blacklist/emails/check", params={"email": email})
        except ManyReachError as e:
            if e.status == 404:
                return False
            raise
        if not data:
            return False
        # Defensive: API may return {blacklisted: true} or similar.
        if isinstance(data, dict):
            return bool(
                data.get("blacklisted")
                or data.get("isBlacklisted")
                or data.get("exists")
            )
        return bool(data)

    # ----- Campaigns (for context / stats) -----

    def get_campaign(self, campaign_id: int) -> dict:
        return self._request("GET", f"/campaigns/{campaign_id}")


# ----- Bounce / auto-reply detection -----

BOUNCE_FROM_PATTERNS = (
    "mailer-daemon@",
    "postmaster@",
    "mail-daemon@",
    "noreply@",
    "no-reply@",
)
BOUNCE_SUBJECT_PATTERNS = (
    "undelivered mail returned",
    "undeliverable",
    "couldn't be delivered",
    "could not be delivered",
    "delivery status notification",
    "mail delivery failed",
    "message not delivered",
    "returned mail",
    "failure notice",
    "sender action required",
    "non remis",
    "non livré",
    "absence",
    "out of office",
    "out of the office",
    "out-of-office",
    "automatic reply",
    "auto-reply",
    "auto reply",
    "réponse automatique",
    "reponse automatique",
    "extended leave",
    "on leave",
    "en congé",
    "en conge",
    "en vacances",
    "en arrêt",
    "vacation reply",
)


BOUNCE_BODY_PATTERNS = (
    # FR — congés / absence / société fermée / personne partie
    "je suis en congé",
    "suis en congés",
    "suis en conge",
    "actuellement en congé",
    "actuellement absent",
    "je suis absent",
    "actuellement en vacances",
    "en arrêt maladie",
    "en arrêt de travail",
    "pour raisons de santé",
    "raisons médicales",
    "raisons de santé",
    "ne fait plus partie",
    "ne fait plus parti",
    "n'est plus dans nos effectifs",
    "n'est plus dans la société",
    "a quitté ses fonctions",
    "a quitté l'entreprise",
    "a quitté la société",
    "a quitté l'agence",
    "cesse son activité",
    "cessation d'activité",
    "fermeture définitive",
    "fermeture de l'entreprise",
    "n'est désormais plus utilisée",
    "n'est plus utilisée",
    "n'est plus active",
    "veuillez noter la nouvelle adresse",
    "veuillez noter le changement",
    "ma nouvelle adresse",
    "ma boite mail change",
    # EN
    "i am out of the office",
    "i am out of office",
    "currently out of the office",
    "currently out of office",
    "i'm on leave",
    "i am on leave",
    "i'm on vacation",
    "on annual leave",
    "no longer with",
    "is no longer employed",
    "has left the company",
    # DE
    "ich bin abwesend",
    "bin im urlaub",
    "in ferien",
    "aus gesundheitlichen gründen abwesend",
    "krankheitsbedingt abwesend",
    "nicht mehr bei",
)


def is_bounce_or_auto(msg: Message) -> bool:
    """Heuristic: identifies bounces, mailer-daemon, OOO, congés, société fermée,
    personne partie, changement d'adresse — bref tout reply qu'il n'y a aucune
    raison de remonter à Rudy. On regarde sender + subject + BODY (les out-of-
    office français/allemand ont souvent un subject neutre, le signal est dans
    le corps)."""
    sender = (msg.from_email or "").lower()
    subject = (msg.subject or "").lower()
    body = (msg.body or "").lower()
    for pat in BOUNCE_FROM_PATTERNS:
        if pat in sender:
            return True
    for pat in BOUNCE_SUBJECT_PATTERNS:
        if pat in subject:
            return True
    if "mail-out" in sender or "mailout" in sender:
        return True
    # Body check : on regarde les 800 premiers chars (= avant les signatures /
    # quotes), suffisant pour les patterns OOO / changement d'adresse.
    body_head = body[:800]
    for pat in BOUNCE_BODY_PATTERNS:
        if pat in body_head:
            return True
    return False


# ----- MailInBlack detection (anti-spam validation gate) -----
#
# MailInBlack intercepts incoming mail and sends a "click to validate" challenge
# back to the sender. Until the validation is clicked, the original email never
# reaches the prospect. ManyReach strips the HTML of these challenge mails so we
# CANNOT auto-click the validation URL from the API alone — we'd need IMAP/Gmail
# API access to the sending mailbox to recover the URL.
#
# For now: detect properly, tag the prospect for manual click, DO NOT mark as
# bounce (the prospect's mailbox is healthy, just protected by MailInBlack).

MAILINBLACK_SENDER_PATTERNS = (
    "@invitations.mailinblack.com",
    "@mailinblack.com",
    "@mail-in-black.com",
)
MAILINBLACK_BODY_HINTS = (
    "mailinblack",
    "mail in black",
    "un clic pour délivrer",
    "un clic pour delivrer",
    "click to deliver",
    "validez votre envoi",
)


def is_mailinblack(msg: Message) -> bool:
    """Return True if this looks like a MailInBlack validation challenge."""
    sender = (msg.from_email or "").lower()
    for pat in MAILINBLACK_SENDER_PATTERNS:
        if pat in sender:
            return True
    body = (msg.body or "").lower()
    sender_hit = "mailinblack" in sender
    body_hit = any(h in body for h in MAILINBLACK_BODY_HINTS)
    return sender_hit or body_hit
