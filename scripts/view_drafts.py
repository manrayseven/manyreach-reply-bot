"""Generate a clean HTML report of the drafts from the latest run and open it.

Reads the most recent logs/run_*.jsonl, renders each human draft (skips bounces
and MailInBlack), and opens the report in the default browser. Much nicer than
reading raw HTML in a console.

Usage: py scripts/view_drafts.py
"""
from __future__ import annotations

import html
import json
import sys
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = PROJECT_ROOT / "logs"

INTENT_LABELS = {
    "meeting_confirmed": ("RDV confirmé", "#16a34a"),
    "interested_warm": ("Intéressé chaud", "#16a34a"),
    "interested_lukewarm": ("Intéressé tiède", "#65a30d"),
    "ask_more_info": ("Demande d'infos", "#0891b2"),
    "objection_price": ("Objection prix", "#d97706"),
    "objection_timing": ("Pas le moment", "#d97706"),
    "objection_already_have_solution": ("A déjà une solution", "#d97706"),
    "wrong_person_redirect": ("Mauvaise personne", "#7c3aed"),
    "not_interested_polite": ("Pas intéressé", "#dc2626"),
    "unsubscribe": ("Désinscription", "#dc2626"),
    "hostile": ("Hostile", "#dc2626"),
}


def latest_log() -> Path | None:
    logs = sorted(LOGS_DIR.glob("run_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def build_report(log_path: Path) -> str:
    cards = []
    n_human = 0
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        intent = j.get("intent", "")
        if intent in ("bounce_or_auto", "mailinblack_pending"):
            continue
        if j.get("draft_skip_send"):
            continue
        body = j.get("draft_body") or ""
        if not body:
            continue
        n_human += 1

        label, color = INTENT_LABELS.get(intent, (intent, "#475569"))
        conf = j.get("confidence", "")
        review = j.get("review_reason", "")
        auto = j.get("auto_send", False)
        actions = j.get("actions", [])
        status_action = next((a for a in actions if "status=" in a), "")
        notes = j.get("draft_notes", "")

        badge_send = (
            '<span style="background:#16a34a;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px">AUTO-SEND</span>'
            if auto
            else '<span style="background:#f59e0b;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px">À RELIRE</span>'
        )

        reply_clean = j.get("reply_clean", "") or "(contenu du reply non disponible)"
        original_clean = j.get("original_clean", "") or "(cold mail d'origine non disponible)"

        cards.append(f"""
        <div class="card">
          <div class="head">
            <div>
              <span class="intent" style="background:{color}">{html.escape(label)}</span>
              <span class="conf">confiance {conf}</span>
              {badge_send}
            </div>
            <div class="from">{html.escape(j.get('from',''))}</div>
          </div>
          <div class="subject">Sujet : {html.escape(j.get('subject',''))}</div>

          <details class="context">
            <summary>📨 Voir l'email d'origine (ton cold mail) + la réponse du prospect</summary>
            <div class="orig-label">Ton cold mail d'origine :</div>
            <div class="orig">{html.escape(original_clean)}</div>
            <div class="orig-label">Ce que le prospect a répondu :</div>
            <div class="reply">{html.escape(reply_clean)}</div>
          </details>

          <div class="section-label">↳ Réponse que le bot propose d'envoyer :</div>
          <div class="draft">{body}</div>
          <details>
            <summary>Détails (action ManyReach + notes du bot)</summary>
            <div class="meta">
              <div><b>Action pipeline :</b> {html.escape(status_action)}</div>
              <div><b>Décision :</b> {'envoi auto' if auto else 'review — ' + html.escape(review)}</div>
              <div><b>Notes du bot :</b> {html.escape(notes)}</div>
            </div>
          </details>
        </div>
        """)

    cards_html = "\n".join(cards) if cards else "<p>Aucun draft humain dans ce run.</p>"
    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Drafts — ManyReach Reply Bot</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f1f5f9; margin:0; padding:24px; color:#0f172a; }}
  h1 {{ font-size:22px; }}
  .sub {{ color:#64748b; margin-bottom:24px; }}
  .card {{ background:#fff; border-radius:10px; padding:20px; margin-bottom:18px; box-shadow:0 1px 3px rgba(0,0,0,.1); max-width:780px; }}
  .head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; flex-wrap:wrap; gap:8px; }}
  .intent {{ color:#fff; padding:3px 10px; border-radius:5px; font-size:13px; font-weight:600; }}
  .conf {{ color:#64748b; font-size:12px; margin-left:8px; }}
  .from {{ color:#334155; font-size:14px; font-weight:600; }}
  .subject {{ color:#64748b; font-size:13px; margin-bottom:14px; }}
  .section-label {{ font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:#94a3b8; margin-bottom:6px; }}
  .draft {{ background:#f8fafc; border-left:3px solid #16a34a; padding:14px 16px; border-radius:4px; line-height:1.55; font-size:15px; }}
  details {{ margin-top:12px; }}
  details.context {{ margin-top:0; margin-bottom:14px; }}
  summary {{ cursor:pointer; color:#0891b2; font-size:13px; }}
  .orig-label {{ font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:#94a3b8; margin:10px 0 4px; }}
  .orig {{ background:#fafaf9; border-left:3px solid #cbd5e1; padding:10px 14px; border-radius:4px; font-size:13px; color:#475569; white-space:pre-wrap; line-height:1.5; }}
  .reply {{ background:#eff6ff; border-left:3px solid #3b82f6; padding:10px 14px; border-radius:4px; font-size:14px; color:#1e3a5f; white-space:pre-wrap; line-height:1.5; }}
  .meta {{ margin-top:8px; font-size:13px; color:#475569; line-height:1.6; }}
  .meta div {{ margin-bottom:4px; }}
</style></head><body>
<h1>Drafts générés par le bot — {n_human} réponses humaines</h1>
<div class="sub">Source : {html.escape(log_path.name)} · Mode dry-run (aucun mail envoyé) · Les réponses "À RELIRE" attendent ta validation.</div>
{cards_html}
</body></html>"""


def main() -> int:
    log_path = latest_log()
    if not log_path:
        print("Aucun log trouvé. Lance d'abord le bot (option 1).")
        return 1
    report = build_report(log_path)
    out = PROJECT_ROOT / "drafts_review.html"
    out.write_text(report, encoding="utf-8")
    print(f"Rapport généré : {out}")
    try:
        webbrowser.open(out.as_uri())
        print("Ouverture dans ton navigateur...")
    except Exception:
        print(f"Ouvre ce fichier dans ton navigateur : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
