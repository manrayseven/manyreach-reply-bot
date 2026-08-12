"""ManyReach API v2 client.

Wraps the endpoints we use for reply handling. Auth via X-API-Key header.
Full OpenAPI spec is in manyreach_openapi.json at repo root.
"""
from __future__ import annotations

import os
import re
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
            try:
                resp = self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                # Timeout de lecture / blip réseau ManyReach. On ne retry QUE les
                # GET (idempotents) : retry un POST pourrait double-envoyer un
                # email. 1 retry court ; sinon on lève → le reply repasse au cron
                # suivant (non marqué traité), donc auto-réparation sans doublon.
                if method.upper() == "GET" and attempt < max_attempts - 1:
                    _t.sleep(1.0)
                    continue
                raise
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

    def fetch_full_body(self, from_email: str, subject: str,
                        message_id: str | None = None) -> str:
        """Récupère le CORPS COMPLET d'un reply (fullBodies=true). L'API tronque
        le corps par défaut (preview 1500 car) → parfois vide/coupé pour un
        message avec beaucoup de HTML. Utile quand la preview est vide/illisible.
        Renvoie le body du message qui matche message_id, sinon le plus récent."""
        try:
            params = {
                "type": "Reply", "emailFrom": from_email,
                "subject": (subject or "")[:120], "fullBodies": "true", "limit": 5,
            }
            data = self._request("GET", "/messages", params=params)
            items = (data or {}).get("items", []) or []
            if message_id:
                for it in items:
                    if str(it.get("messageId")) == str(message_id):
                        return it.get("body") or ""
            return (items[0].get("body") or "") if items else ""
        except Exception:  # noqa: BLE001
            return ""

    def fetch_challenge_url(self, from_email: str, subject: str) -> str | None:
        """Récupère le LIEN DE VALIDATION d'un challenge antispam en refetchant le
        message avec fullBodies=true (l'API tronque le corps par défaut → le lien
        est perdu ; avec fullBodies il est présent). Renvoie l'URL ou None."""
        try:
            params = {
                "type": "Reply",
                "emailFrom": from_email,
                "subject": subject[:120],
                "fullBodies": "true",
                "limit": 5,
            }
            data = self._request("GET", "/messages", params=params)
            for item in (data or {}).get("items", []):
                url = extract_challenge_url(Message.from_api(item))
                if url:
                    return url
        except Exception:  # noqa: BLE001
            return None
        return None


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
    # Congé maternité / parental : absence longue → silencieux, jamais d'alerte.
    # Souvent le corps est VIDE et l'info n'est QUE dans le sujet (cas
    # a.mocquet@neoaxess "Congé maternite" 16/06). On couvre accent/sans accent
    # via le préfixe "matern" / "parental".
    "congé matern",
    "conge matern",
    "congés matern",
    "conges matern",
    "congé parental",
    "conge parental",
    "maternity leave",
    "parental leave",
    # Congés / absence / fermeture temporaire : autoreply → silencieux. Le corps
    # est SOUVENT vide (l'info n'est que dans le sujet, cas c.nizon "Absente pour
    # congés", homeconcept36 "Congés Re:...", capservices83 "Fermeture Re:...").
    "congé",
    "congés",
    "conges",
    "absente",
    "absent ",
    "fermeture",
    "fermé pour",
    "fermeture estivale",
    "fermeture annuelle",
)


BOUNCE_BODY_PATTERNS = (
    # FR — congés / absence / société fermée / personne partie
    "je suis en congé",
    "suis en congés",
    "suis en conge",
    "actuellement en congé",
    "en congés jusqu",
    "en congé jusqu",
    # Absences datées : séminaire / formation / déplacement / absent "jusqu'au X".
    # Le "jusqu" impose une date de retour = autoreply, pas un vrai "recontactez-moi"
    # (cas benjamin.blanchard "Je suis en séminaire jusqu'au 17 Juillet inclus").
    "en séminaire jusqu",
    "en seminaire jusqu",
    "en formation jusqu",
    "en déplacement jusqu",
    "en deplacement jusqu",
    "absent jusqu",
    "absente jusqu",
    "actuellement absent",
    "je suis absent",
    "actuellement en vacances",
    # OOO "repos / on revient le X" (cas mollo.traiteur : "Notre équipe prend
    # quelques jours de repos, nous revenons le lundi 24 aout !"). Signatures
    # d'autoreply d'équipe — absentes d'un vrai reply intéressé. On garde des
    # marqueurs SPÉCIFIQUES ("nous revenons le", "de repos") pour ne pas avaler
    # un vrai "je reviens vers vous" / "recontactez-nous à la rentrée".
    "quelques jours de repos",
    "jours de repos",
    "jour de repos",
    "en repos jusqu",
    "nous revenons le",
    "nous serons de retour",
    "serons de retour le",
    "reviendrons le",
    "de retour parmi vous le",
    # Autoreply de fermeture temporaire + marqueur "pour toute(s) urgence(s)" (les
    # OOO renvoient vers un contact d'urgence — signal fort d'autoreply). On reste
    # sur "sera fermé" (annonce de fermeture à venir = autoreply) et PAS "sommes
    # fermés" (souvent une vraie réponse "on est fermé, recontactez à la réouverture"
    # → doit atteindre le classifier, cf. test fermeture saisonnière).
    "pour toute urgence",
    "pour toutes urgences",
    "sera fermé",
    # Fermeture datée + "nous répondrons à partir du..." (cas enault.plomberie
    # "bureau sera exceptionnellement fermé du 07/07... nous répondrons à vos
    # demandes à partir du 10/07"). Ces marqueurs N'attrapent PAS "fermés pour la
    # saison, réouverture le X, recontactez-nous" (qui reste un vrai objection_timing).
    "exceptionnellement fermé",
    "exceptionnellement ferme",
    "fermé du ",
    "ferme du ",
    "fermée du ",
    "fermee du ",
    "fermés du ",
    "nous répondrons",
    "nous repondrons",
    "répondrons à vos demandes",
    "repondrons a vos demandes",
    # Auto-reply d'absence "vacances/congés d'équipe" (cas joliebibietsonmini :
    # "sera absente du 24 juillet au 24 août... répondrons à vos mails à notre
    # retour"). Marqueurs très spécifiques d'OOO, absents d'un vrai reply.
    "à notre retour",
    "a notre retour",
    "à mon retour",
    "a mon retour",
    "dès notre retour",
    "des notre retour",
    "dès mon retour",
    "répondrons à vos mails",
    "repondrons a vos mails",
    "répondrons à vos e-mails",
    "répondrons à vos messages",
    "repondrons a vos messages",
    "sera absent",
    "serons absent",
    "sommes absents jusqu",
    "traiterons vos",
    "de retour au bureau le",
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
    # Changement d'adresse e-mail (cas harenovationconstruction : "notre adresse
    # e-mail a changé... utiliser uniquement cette nouvelle adresse... mettre à
    # jour vos contacts"). Autoreply de redirection → silencieux (wrong_person).
    "adresse e-mail a changé",
    "adresse email a changé",
    "adresse mail a changé",
    "adresse e-mail a change",
    "notre adresse a changé",
    "e-mail a changé",
    "utiliser uniquement cette nouvelle adresse",
    "d'utiliser uniquement cette nouvelle",
    "mettre à jour vos contacts",
    "mettre a jour vos contacts",
    "nous avons changé d'adresse",
    "changé d'adresse mail",
    "changé d'adresse e-mail",
    # EN
    "i am out of the office",
    "i am out of office",
    "currently out of the office",
    "currently out of office",
    "i'm on leave",
    "i am on leave",
    "i'm on vacation",
    "on annual leave",
    # OOO / voyage : autoreply d'absence individuelle (≠ fermeture saisonnière
    # d'une entreprise qui, elle, doit rester en objection_timing). Cas vu le
    # 15/06 : Antoine Langbeen "I am currently travelling... back in the office
    # Thursday June 18th" → ne doit PAS remonter en alerte, c'est un auto-reply.
    "currently travelling",
    "currently traveling",
    "i am travelling",
    "i am traveling",
    "uneven access to my email",
    "uneven access to email",
    "limited access to my email",
    "limited access to email",
    "allow for some delay in my response",
    "please allow for some delay",
    "i will be back in the office",
    "back in the office fully",
    "back in the office on",
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


# ----- Anti-spam challenge detection (MailInBlack & friendes) -----
#
# Les filtres "challenge-réponse" (MailInBlack en tête chez les mairies/TPE
# françaises) interceptent l'email et renvoient un défi "cliquez pour délivrer
# votre message" (+ captcha). Tant que personne ne clique, l'email d'origine
# n'arrive JAMAIS. Le clic valide aussi l'adresse d'envoi définitivement
# (whitelist) → chaque défi est un prospect joignable à coup sûr si Rudy clique.
#
# ManyReach strippe le HTML de ces emails : le lien de validation (un <a href>)
# est PERDU côté API pour MailInBlack (vérifié en live le 09/08/2026 avec
# fullBodies=true). Certains filtres mettent l'URL en texte brut → dans ce cas
# on la récupère et le dashboard affiche un lien direct. Détecter ≠ bounce :
# la boîte du prospect est saine, juste derrière un portail.

# (nom affiché, patterns d'expéditeur) — expéditeurs connus de challenges.
CHALLENGE_SENDER_FILTERS = (
    ("MailInBlack", (
        "@invitations.mailinblack.com",
        "@mailinblack.com",
        "@mail-in-black.com",
        "@mib.ipgarde.com",   # passerelle revendeur observée (ex. ardrom@mib.ipgarde.com)
        "mib-daemon",         # moteur MailInBlack (message-id / relais)
    )),
    ("SpamEnMoins", ("@spamenmoins.com", "spamenmoins")),
    ("Boxbe", ("@boxbe.com",)),
    ("Altospam", ("@altospam.com",)),
)

# Indices dans le corps/sujet (tous filtres confondus, FR + EN). Un seul suffit.
CHALLENGE_BODY_HINTS = (
    # MailInBlack — ⚠️ PAS le simple mot "mailinblack" : il apparaît dans TOUT
    # email envoyé à un destinataire protégé (MailInBlack "Secure Link" réécrit
    # les liens en mibc-*.mailinblack.com/securelink/...) → ça flaggait des emails
    # NORMAUX comme des challenges (cas autocars-groussin : pas un challenge, juste
    # des liens réécrits). On ne détecte MailInBlack que par l'EXPÉDITEUR
    # (@mailinblack.com, via CHALLENGE_SENDER_FILTERS) ou une VRAIE phrase de défi :
    "un clic pour délivrer",
    "un clic pour delivrer",
    "click to deliver",
    # Captcha "authentification" (security-mail.net, poitiers.cci.fr, APM…) :
    # "n'a pas été délivré car le destinataire a souhaité mettre en place un
    # Captcha pour valider l'existance de l'expéditeur. Pour libérer l'email…"
    "captcha pour valider",
    "valider l'existance de l'expéditeur",
    "valider l'existence de l'expéditeur",
    "pour libérer l'email",
    "pour libérer votre email",
    "remplir le formulaire accessible ici",
    "access to the delivery page",
    "enhanced protection against email threats",
    # Génériques FR
    "validez votre envoi",
    "valider votre envoi",
    "votre message est en attente de validation",
    "message est en attente de livraison",
    "pour que votre message soit délivré",
    "pour que votre message soit delivre",
    "confirmez votre adresse pour que votre message",
    "prouvez que vous n'êtes pas un robot",
    "prouver que vous n'êtes pas un robot",
    "je ne suis pas un robot",
    "expéditeur inconnu, merci de valider",
    "anti-spam vous demande de confirmer",
    "antispam vous demande de confirmer",
    # Génériques EN
    "waiting for your confirmation",
    "confirm your email address so your message",
    "sender verification",
    "verify that you are a real person",
    "prove you are human",
    "your message is waiting for approval",
    "message is held for approval",
    # BoxTrapper / cPanel "verify you are a real live human" (cas comquoi.fr :
    # "The message you sent requires that you verify that you are a real live
    # human being and not a spam source... click the following link: .../bxd.cgi")
    "real live human being",
    "not a spam source",
    "leave the subject line intact",
    "requires that you verify",
    "requires verification",
)

# Sujets typiques de challenge quand le corps est vide/strippé.
CHALLENGE_SUBJECT_HINTS = (
    "en attente de validation",
    "validation requise",
    "confirmez votre envoi",
    "confirm your message",
    "sender verification",
    "bloqué pour authentification",
    "bloque pour authentification",
    "authentification mail",
    "requires verification",
    "requires that you verify",
)

# Domaines de validation connus → priorité lors de l'extraction d'URL.
CHALLENGE_URL_DOMAINS = (
    "mailinblack.com",
    "spamenmoins.com",
    "boxbe.com",
    "altospam.com",
)

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)


# Un BOUNCE relayé via un serveur MailInBlack ("host mx-...mailinblack.com said:
# 550 User unknown") contient "mailinblack" → il était pris à tort pour un
# challenge. Ces marqueurs de rejet de remise priment : ce n'est PAS un challenge.
_BOUNCE_NOT_CHALLENGE = (
    "undelivered mail", "returned to sender", "delivery status notification",
    "mail delivery failed", "could not be delivered", "delivery has failed",
    "mailer-daemon", "the mail system", "mail delivery subsystem",
    "recipient address rejected", "user unknown", "no such user",
    "550 5.1.1", "550 5.4", "550-5.1.1", "does not exist", "adresse inexistante",
)


def detect_antispam_challenge(msg: Message) -> str | None:
    """Nom du filtre challenge-réponse détecté ('MailInBlack', …, 'Challenge'
    pour un filtre non identifié), ou None si ce n'est pas un challenge.

    ⚠️ Un BOUNCE (adresse inexistante) relayé par un serveur MailInBlack contient
    "mailinblack" mais N'EST PAS un challenge → on le laisse au filtre bounce."""
    sender = (msg.from_email or "").lower()
    subject = (msg.subject or "").lower()
    body = (msg.body or "").lower()
    if any(b in subject or b in body for b in _BOUNCE_NOT_CHALLENGE):
        return None  # c'est un bounce, pas un challenge
    for name, patterns in CHALLENGE_SENDER_FILTERS:
        if any(p in sender for p in patterns):
            return name
    for hint in CHALLENGE_BODY_HINTS:
        if hint in body:
            if "mailinblack" in hint or "mail in black" in hint or "clic pour d" in hint:
                return "MailInBlack"
            return "Challenge"
    for hint in CHALLENGE_SUBJECT_HINTS:
        if hint in subject:
            return "Challenge"
    return None


def extract_challenge_url(msg: Message) -> str | None:
    """Meilleure URL de VALIDATION dans le corps. ⚠️ Le corps doit être le HTML
    COMPLET (fetch avec fullBodies=true) : la preview tronquée de l'API n'a pas le
    lien. On IGNORE les pixels de tracking d'ouverture (/tr/op/) et les images, et
    on PRÉFÈRE les chemins de validation (verify/valider/confirm/unlock)."""
    body = msg.body or ""
    urls = [u.rstrip(".,;)\"'") for u in _URL_RE.findall(body)]
    if not urls:
        return None
    _junk = (".png", ".jpg", ".jpeg", ".gif", ".css", ".js", "unsubscribe",
             "desinscription", "/tr/op/", "mailto:",
             # SCANNERS DE LIENS (pas des validations d'expéditeur) : MailInBlack
             # "Secure Link" réécrit les liens DE TON email pour les analyser
             # (/protect/securelink?url=...) → cliquer ça ne valide RIEN (erreur).
             "/protect/securelink", "securelink", "?url=", "&url=", "/urlscan",
             "/link-protection", "safelinks.protection", "/scan?",
             # NAMESPACES / SCHÉMAS XML des emails Outlook/Word (xmlns:...) — ce ne
             # sont PAS des liens cliquables (cas autocars-groussin : renvoyait
             # schemas.microsoft.com/office/2004/12/omml → page morte).
             "schemas.microsoft.com", "schemas.openxmlformats.org", "www.w3.org",
             "purl.org", "schemas.xmlsoap.org", "/tr/vc/", "/wf/open")
    clean = [u for u in urls if not any(x in u.lower() for x in _junk)]
    # 1) Domaine de validation connu (mailinblack.com, …).
    for u in clean:
        if any(d in u.lower() for d in CHALLENGE_URL_DOMAINS):
            return u
    # 2) Chemin explicitement "validation" (verify/valider/confirm/authentif/libérer…).
    _kw = ("verify", "valid", "confirm", "unlock", "release", "activate",
           "challenge", "authentif", "liberer", "captcha", "delivery",
           # BoxTrapper / cPanel : lien type .../cgi-sys/bxd.cgi?a=...&id=...
           "bxd.cgi", "cgi-sys", "boxtrapper")
    for u in clean:
        if any(k in u.lower() for k in _kw):
            return u
    # 3) Lien de clic tracké (bouton "Valider" → redirige vers la validation).
    for u in clean:
        if "/tr/cl/" in u.lower() or "/click" in u.lower():
            return u
    # 4) SINON → None. On ne retourne JAMAIS une URL "au hasard" (footer,
    #    namespace, réseau social…) : mieux vaut "lien dans la boîte" que
    #    d'envoyer Rudy sur une page morte. Le lien de validation est souvent
    #    strippé par ManyReach → réellement absent, on l'assume.
    return None


def is_mailinblack(msg: Message) -> bool:
    """Compat : True si le message est un challenge antispam (tous filtres)."""
    return detect_antispam_challenge(msg) is not None
