"""Verify the Google Calendar connection and show the next free slots.

Run AFTER the Google Cloud setup (see GUIDE-GOOGLE-CALENDAR.md):
  py scripts/check_calendar.py

Writes the full result to calendar_test.txt so nothing is lost even if the
console window closes. Never crashes — any error is captured to the file.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Make console output robust on Windows cp1252 (avoid UnicodeEncodeError crashes)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESULT_FILE = PROJECT_ROOT / "calendar_test.txt"
_lines: list[str] = []


def out(msg: str = "") -> None:
    _lines.append(msg)
    try:
        print(msg)
    except Exception:
        print(msg.encode("ascii", "replace").decode("ascii"))


def run() -> None:
    from dotenv import load_dotenv
    import yaml

    load_dotenv()

    from src.calendar_slots import CalendarClient

    settings_path = PROJECT_ROOT / "config" / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    cal_cfg = settings.get("calendar", {})

    out("Connexion a Google Calendar...")
    client = CalendarClient()

    name = client.whoami()
    out(f"[OK] Connecte a l'agenda : {name}")

    wh = cal_cfg.get("working_hours") or {
        "monday": [["10:00", "12:00"], ["14:00", "18:00"]],
        "tuesday": [["10:00", "12:00"], ["14:00", "18:00"]],
        "wednesday": [["10:00", "12:00"], ["14:00", "18:00"]],
        "thursday": [["10:00", "12:00"], ["14:00", "18:00"]],
        "friday": [["10:00", "12:00"]],
    }
    slots = client.find_free_slots(
        working_hours=wh,
        tz_name=cal_cfg.get("timezone", "Europe/Paris"),
        duration_min=cal_cfg.get("meeting_duration_minutes", 30),
        buffer_min=cal_cfg.get("buffer_minutes", 15),
        days_ahead=cal_cfg.get("days_ahead", 5),
        max_slots=3,
    )
    out("")
    out("Tes 3 prochains creneaux libres :")
    if not slots:
        out("  (aucun trouve - verifie tes working_hours dans settings.yaml)")
    for s in slots:
        out(f"  - {s.fr()}")
    out("")
    out("[SUCCES] Le Calendar fonctionne. Dis a Claude qu'il branche tout.")


def main() -> int:
    code = 0
    try:
        run()
    except Exception as e:
        code = 1
        out("")
        out(f"[ERREUR] {type(e).__name__}: {e}")
        out("")
        # Friendly diagnosis for the most common causes
        msg = str(e).lower()
        if "certificate" in msg or "ssl" in msg:
            out("Cause probable : certificat SSL (souvent un antivirus/proxy reseau).")
            out("On regardera ensemble - colle ce fichier a Claude.")
        elif "not found" in msg or "notfound" in msg or "404" in msg:
            out("Cause probable : l'agenda n'est pas partage avec le compte de service,")
            out("OU le GOOGLE_CALENDAR_ID est faux. Verifie l'etape 5 du guide.")
        elif "introuvable" in msg or "service account" in msg:
            out("Cause probable : le fichier google-service-account.json est mal place/nomme.")
        else:
            out("Detail technique (pour Claude) :")
            out(traceback.format_exc())
    # Always write the result file
    try:
        RESULT_FILE.write_text("\n".join(_lines), encoding="utf-8")
        out("")
        out(f"(Resultat aussi ecrit dans : {RESULT_FILE.name})")
    except Exception:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())
