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
        resp = self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise ManyReachError(resp.status_code, resp.text, str(resp.request.url))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ----- Messages / Replies -----

    def list_replies(
        self,
        campaign_id: int | None = None,
        since: datetime | None = None,
        page_size: int = 100,
    ) -> Iterable[Message]:
        """Yield reply Messages. Filters out non-Reply types defensively."""
        params: dict[str, Any] = {"type": "Reply", "pageSize": page_size}
        if campaign_id is not None:
            params["campaignId"] = campaign_id
        offset = 0
        while True:
            params["offset"] = offset
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
            offset += page_size

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
    "delivery status notification",
    "mail delivery failed",
    "message not delivered",
    "returned mail",
    "failure notice",
    "non remis",
    "non livré",
    "absence",
    "out of office",
    "out-of-office",
)


def is_bounce_or_auto(msg: Message) -> bool:
    """Heuristic: identifies bounces, mailer-daemon, OOO, etc. before classification."""
    sender = (msg.from_email or "").lower()
    subject = (msg.subject or "").lower()
    for pat in BOUNCE_FROM_PATTERNS:
        if pat in sender:
            return True
    for pat in BOUNCE_SUBJECT_PATTERNS:
        if pat in subject:
            return True
    if "mail-out" in sender or "mailout" in sender:
        return True
    return False
