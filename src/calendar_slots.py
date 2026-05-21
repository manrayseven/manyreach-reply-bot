"""Google Calendar integration: find free slots + create meeting events.

Uses a Google service account (no OAuth consent flow). Setup:
  1. Create a service account in Google Cloud, download its JSON key.
  2. Share your Google Calendar with the service account's email
     (permission: "Make changes to events").
  3. Set GOOGLE_SERVICE_ACCOUNT_JSON (path to key) + GOOGLE_CALENDAR_ID in .env.

See GUIDE-GOOGLE-CALENDAR.md for the click-by-click setup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCOPES = ["https://www.googleapis.com/auth/calendar"]

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# French slot formatting
_FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_FR_MONTHS = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime

    def fr(self) -> str:
        """Human French rendering, e.g. 'mardi 12 mai à 14h30'."""
        d = self.start
        hour = f"{d.hour}h" + (f"{d.minute:02d}" if d.minute else "")
        return f"{_FR_DAYS[d.weekday()]} {d.day} {_FR_MONTHS[d.month]} à {hour}"


class CalendarClient:
    def __init__(self, sa_json_path: str | None = None, calendar_id: str | None = None):
        self.calendar_id = calendar_id or os.environ.get("GOOGLE_CALENDAR_ID")
        if not self.calendar_id:
            raise RuntimeError("GOOGLE_CALENDAR_ID non défini")

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        # Deux sources possibles pour la clé du compte de service :
        #  - LOCAL : un fichier (GOOGLE_SERVICE_ACCOUNT_JSON = chemin)
        #  - VERCEL/cloud : le JSON entier (ou base64) dans une variable d'env
        #    GOOGLE_SERVICE_ACCOUNT_INFO (pas de fichier sur disque en serverless)
        import json as _json

        sa_info_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_INFO")
        if sa_info_env:
            raw = sa_info_env.strip()
            if not raw.startswith("{"):
                import base64
                raw = base64.b64decode(raw).decode("utf-8")
            creds = service_account.Credentials.from_service_account_info(
                _json.loads(raw), scopes=SCOPES
            )
        else:
            sa_path = sa_json_path or os.environ.get(
                "GOOGLE_SERVICE_ACCOUNT_JSON", "google-service-account.json"
            )
            if not Path(sa_path).exists():
                raise RuntimeError(
                    f"Service account introuvable : ni GOOGLE_SERVICE_ACCOUNT_INFO "
                    f"(env) ni le fichier {sa_path}. Voir GUIDE-GOOGLE-CALENDAR.md"
                )
            creds = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)

        # Force httplib2 (used by the Google client) to verify TLS against the
        # certifi CA bundle. On Windows, Python's default SSL store often can't
        # find the issuer cert ("unable to get local issuer certificate"), which
        # breaks both the token refresh and the API calls. Routing everything
        # through certifi fixes it.
        try:
            import certifi
            import httplib2
            from google_auth_httplib2 import AuthorizedHttp

            base_http = httplib2.Http(ca_certs=certifi.where())
            authed_http = AuthorizedHttp(creds, http=base_http)
            self.service = build("calendar", "v3", http=authed_http, cache_discovery=False)
        except ImportError:
            # Fallback to default transport if the httplib2 helpers aren't present
            self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    def whoami(self) -> str:
        """Return the calendar summary — used to verify the connection works."""
        cal = self.service.calendars().get(calendarId=self.calendar_id).execute()
        return cal.get("summary", self.calendar_id)

    def _busy_intervals(self, tz: ZoneInfo, days_ahead: int) -> list[tuple[datetime, datetime]]:
        now = datetime.now(tz)
        body = {
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(days=days_ahead + 2)).isoformat(),
            "items": [{"id": self.calendar_id}],
        }
        fb = self.service.freebusy().query(body=body).execute()
        busy = fb["calendars"][self.calendar_id].get("busy", [])
        out = []
        for b in busy:
            out.append(
                (
                    datetime.fromisoformat(b["start"]).astimezone(tz),
                    datetime.fromisoformat(b["end"]).astimezone(tz),
                )
            )
        return out

    def find_free_slots(
        self,
        working_hours: dict[str, list[list[str]]],
        tz_name: str = "Europe/Paris",
        duration_min: int = 30,
        buffer_min: int = 15,
        days_ahead: int = 5,
        max_slots: int = 3,
        exclude_starts: set[datetime] | None = None,
        min_lead_hours: int = 3,
        late_cutoff_hour: int = 16,
        next_day_min_time_if_late: str = "10:30",
    ) -> list[Slot]:
        """Return up to max_slots free Slots within working hours over the next days.

        Rules applied:
        - exclude_starts: slots already proposed to other prospects (soft-holds).
        - min_lead_hours: never propose a slot less than N hours from now.
        - late rule: if it's already past late_cutoff_hour, don't propose any slot
          on the NEXT day before next_day_min_time_if_late (Rudy wouldn't see an
          early-morning meeting booked late the evening before).
        - working_hours windows already exclude lunch run (11h-12h) etc.
        """
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        busy = self._busy_intervals(tz, days_ahead)
        excluded = {
            s.astimezone(tz).replace(second=0, microsecond=0)
            for s in (exclude_starts or set())
        }
        lead_cutoff = now + timedelta(hours=min_lead_hours)
        tomorrow = (now + timedelta(days=1)).date()
        is_late_now = now.hour >= late_cutoff_hour
        next_day_min = time.fromisoformat(next_day_min_time_if_late)

        def overlaps(s: datetime, e: datetime) -> bool:
            return any(not (e <= b0 or s >= b1) for b0, b1 in busy)

        def is_excluded(s: datetime) -> bool:
            return s.replace(second=0, microsecond=0) in excluded

        def violates_rules(s: datetime) -> bool:
            if s < lead_cutoff:
                return True
            if is_late_now and s.date() == tomorrow and s.time() < next_day_min:
                return True
            return False

        slots: list[Slot] = []
        day = now.date()
        checked = 0
        while len(slots) < max_slots and checked <= days_ahead + 7:
            weekday = WEEKDAYS[day.weekday()]
            for win in working_hours.get(weekday, []):
                start_t = time.fromisoformat(win[0])
                end_t = time.fromisoformat(win[1])
                win_start = datetime.combine(day, start_t, tzinfo=tz)
                win_end = datetime.combine(day, end_t, tzinfo=tz)
                cursor = max(win_start, lead_cutoff)
                # round up to next quarter hour for clean slot times
                if cursor.minute % 15:
                    cursor += timedelta(minutes=15 - (cursor.minute % 15))
                    cursor = cursor.replace(second=0, microsecond=0)
                while cursor + timedelta(minutes=duration_min) <= win_end:
                    slot_end = cursor + timedelta(minutes=duration_min)
                    if (
                        not overlaps(cursor, slot_end)
                        and not is_excluded(cursor)
                        and not violates_rules(cursor)
                    ):
                        slots.append(Slot(cursor, slot_end))
                        if len(slots) >= max_slots:
                            break
                        cursor = slot_end + timedelta(minutes=buffer_min)
                    else:
                        cursor += timedelta(minutes=15)
                if len(slots) >= max_slots:
                    break
            day += timedelta(days=1)
            checked += 1
        return slots[:max_slots]

    def create_event(
        self,
        *,
        title: str,
        start: datetime,
        duration_min: int,
        description: str,
        tz_name: str = "Europe/Paris",
    ) -> dict:
        """Create a calendar event. Returns the created event resource."""
        end = start + timedelta(minutes=duration_min)
        event = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
        }
        return self.service.events().insert(calendarId=self.calendar_id, body=event).execute()


def build_meeting_title(offer: str, who: str) -> str:
    """Format per Rudy's spec: '14.00 Call <offre> avec <entreprise/prénom>'.

    The leading time is added by the caller (it knows the slot). This returns
    'Call <offre> avec <who>'.
    """
    return f"Call {offer} avec {who}".strip()


def build_meeting_description(
    *,
    email: str,
    phone: str | None = None,
    zoom_link: str | None = None,
    website: str | None = None,
    company: str | None = None,
    notes: str | None = None,
) -> str:
    """Structured event description with the prospect's coordinates."""
    lines = [f"Email : {email}"]
    if phone:
        lines.append(f"Téléphone : {phone}")
    if zoom_link:
        lines.append(f"Lien visio : {zoom_link}")
    if website:
        lines.append(f"Site : {website}")
    if company:
        lines.append(f"Entreprise : {company}")
    if notes:
        lines.append("")
        lines.append(notes)
    return "\n".join(lines)
