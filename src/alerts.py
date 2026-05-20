"""Alert delivery for meeting-booked events.

When the bot detects a confirmed meeting, Rudy wants an email alert at
contact@webmarketing-conseil.fr.

Email sending requires an external service. We support Resend (free tier,
simple API) if RESEND_API_KEY is set. If not configured, we degrade gracefully:
the alert is written to alerts/meetings_to_review.txt AND printed to the console,
so nothing is lost — it just isn't emailed until a service is wired up.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

ALERT_EMAIL = os.environ.get("NOTIFY_EMAIL", "contact@webmarketing-conseil.fr")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALERTS_DIR = PROJECT_ROOT / "alerts"


def _write_local_alert(subject: str, body: str) -> Path:
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    f = ALERTS_DIR / "meetings_to_review.txt"
    ts = datetime.now(timezone.utc).isoformat()
    with f.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{'=' * 60}\n[{ts}] {subject}\n{'-' * 60}\n{body}\n")
    return f


def _send_via_resend(subject: str, body_text: str) -> bool:
    """Send the alert via Resend. Returns True on success."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return False
    from_addr = os.environ.get("RESEND_FROM", "alertes@webmarketing-conseil.fr")
    try:
        import httpx

        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": from_addr,
                "to": [ALERT_EMAIL],
                "subject": subject,
                "text": body_text,
            },
            timeout=15.0,
        )
        return resp.status_code < 300
    except Exception as e:  # pragma: no cover
        print(f"  !! Resend alert failed: {e}")
        return False


def send_meeting_alert(
    *,
    prospect_email: str,
    prospect_name: str | None,
    company: str | None,
    reply_snippet: str,
    proposed_when: str,
    campaign_id: int | None,
    dry_run: bool = True,
) -> str:
    """Fire a meeting-booked alert. Returns a human-readable status line."""
    name = prospect_name or prospect_email
    subject = f"🤝 RDV potentiel — {name}" + (f" ({company})" if company else "")
    body = (
        f"Un prospect a confirmé/proposé un créneau.\n\n"
        f"Prospect : {name} <{prospect_email}>\n"
        f"Société  : {company or '(inconnue)'}\n"
        f"Campagne : {campaign_id or '(inconnue)'}\n"
        f"Créneau  : {proposed_when}\n\n"
        f"Extrait du reply :\n{reply_snippet}\n\n"
        f"→ Action : vérifie ta dispo, confirme la date exacte, ajoute à ton agenda.\n"
        f"(Le draft de confirmation est en review dans le run du bot.)"
    )

    # Always write the local alert (audit trail + zero-config fallback)
    local_path = _write_local_alert(subject, body)

    if dry_run:
        return f"[DRY-RUN] Alerte RDV (non envoyée) — écrite dans {local_path.name}"

    if _send_via_resend(subject, body):
        return f"[EXEC] Alerte RDV envoyée par email à {ALERT_EMAIL}"
    return (
        f"[EXEC] Alerte RDV écrite dans {local_path.name} "
        f"(email non configuré — ajoute RESEND_API_KEY dans .env pour l'envoi auto)"
    )
