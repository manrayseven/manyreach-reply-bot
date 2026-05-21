"""Dashboard Vercel — réglages, suivi des actions, bouton stop/start.

Page unique servie sur l'URL Vercel. Lit/écrit l'état dans Vercel KV.
Protection simple : si DASHBOARD_KEY est défini, exige ?key=... dans l'URL.

GET  /            → la page HTML
POST / (form)     → actions : toggle on/off, sauver des réglages
"""
from http.server import BaseHTTPRequestHandler
import html
import os
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import kvstore  # noqa: E402


def _check_key(query: dict) -> bool:
    key = os.environ.get("DASHBOARD_KEY")
    if not key:
        return True  # pas de protection configurée
    return query.get("key", [""])[0] == key


def _render() -> str:
    enabled = kvstore.is_enabled()
    last_run = kvstore.get_last_run() or "jamais"
    actions = kvstore.recent_actions(40)
    ov = kvstore.get_settings_overrides()
    sending = ov.get("sending", {})
    hours = sending.get("allowed_hours", [9, 19])
    min_age = sending.get("min_reply_age_minutes", 12)

    rows = ""
    for a in actions:
        when = a.get("at", "")[:16].replace("T", " ")
        intent = html.escape(str(a.get("intent", "")))
        frm = html.escape(str(a.get("from", "")))
        sent = "ENVOYÉ" if a.get("auto_send") else ("GARDÉ" if a.get("held") else "review")
        acts = html.escape(" | ".join(a.get("actions", []))[:160])
        rows += f"<tr><td>{when}</td><td>{frm}</td><td>{intent}</td><td>{sent}</td><td class='small'>{acts}</td></tr>"
    if not rows:
        rows = "<tr><td colspan=5 class='small'>Aucune action encore (le bot n'a rien traité, ou KV vide).</td></tr>"

    status_color = "#16a34a" if enabled else "#dc2626"
    status_txt = "EN MARCHE" if enabled else "EN PAUSE"
    toggle_label = "⏸️ Mettre en pause" if enabled else "▶️ Réactiver"
    toggle_val = "0" if enabled else "1"
    keyq = os.environ.get("DASHBOARD_KEY")
    keyparam = f"?key={keyq}" if keyq else ""

    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ManyReach Bot — Dashboard</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9;margin:0;padding:24px;color:#0f172a}}
 h1{{font-size:22px;margin:0 0 4px}} .muted{{color:#64748b;font-size:13px}}
 .card{{background:#fff;border-radius:10px;padding:18px 20px;margin:16px 0;box-shadow:0 1px 3px rgba(0,0,0,.08);max-width:920px}}
 .badge{{display:inline-block;color:#fff;padding:4px 12px;border-radius:6px;font-weight:600}}
 table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid #eef2f7}}
 th{{color:#64748b;font-weight:600}} .small{{color:#64748b;font-size:12px}}
 button,input[type=submit]{{cursor:pointer;border:0;border-radius:7px;padding:10px 16px;font-size:14px;font-weight:600}}
 .btn-stop{{background:{status_color};color:#fff}} .btn-save{{background:#0891b2;color:#fff}}
 input[type=number]{{width:70px;padding:6px;border:1px solid #cbd5e1;border-radius:6px}}
 label{{font-size:13px;color:#334155;margin-right:14px}}
</style></head><body>
<h1>ManyReach Reply Bot</h1>
<div class="muted">Dashboard de pilotage · dernier passage : {html.escape(last_run)}</div>

<div class="card">
  <span class="badge" style="background:{status_color}">{status_txt}</span>
  <form method="POST" action="/{keyparam}" style="display:inline;margin-left:14px">
    <input type="hidden" name="action" value="toggle">
    <input type="hidden" name="enabled" value="{toggle_val}">
    <button class="btn-stop" type="submit">{toggle_label}</button>
  </form>
</div>

<div class="card">
  <h3>Réglages rapides</h3>
  <form method="POST" action="/{keyparam}">
    <input type="hidden" name="action" value="save_settings">
    <label>Heure début envoi <input type="number" name="hour_start" value="{hours[0]}" min="0" max="23"></label>
    <label>Heure fin envoi <input type="number" name="hour_end" value="{hours[1]}" min="0" max="23"></label>
    <label>Délai mini avant réponse (min) <input type="number" name="min_age" value="{min_age}" min="0" max="240"></label>
    <br><br><input class="btn-save" type="submit" value="Enregistrer">
  </form>
  <p class="small">Les réglages complets (voice, règles RDV, cadence) restent dans le code pour l'instant — éditables ici dans une prochaine version.</p>
</div>

<div class="card">
  <h3>Suivi des actions ({len(actions)} dernières)</h3>
  <table>
    <tr><th>Quand</th><th>Prospect</th><th>Intent</th><th>Statut</th><th>Détail</th></tr>
    {rows}
  </table>
</div>
</body></html>"""


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if not _check_key(query):
            self._send(403, "Accès refusé (clé manquante : ajoute ?key=... à l'URL)")
            return
        if not kvstore.kv_available():
            self._send(200, "<h2>KV non configurée</h2><p>Connecte une base Vercel KV au projet (voir DEPLOY-VERCEL.md).</p>", html_=True)
            return
        self._send(200, _render(), html_=True)

    def do_POST(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if not _check_key(query):
            self._send(403, "Accès refusé")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        action = form.get("action")
        if action == "toggle":
            kvstore.set_enabled(form.get("enabled") == "1")
        elif action == "save_settings":
            ov = kvstore.get_settings_overrides()
            sending = ov.get("sending", {})
            try:
                sending["allowed_hours"] = [int(form.get("hour_start", 9)), int(form.get("hour_end", 19))]
                sending["min_reply_age_minutes"] = int(form.get("min_age", 12))
            except ValueError:
                pass
            ov["sending"] = sending
            kvstore.set_settings_overrides(ov)
        # redirect back to dashboard
        keyq = os.environ.get("DASHBOARD_KEY")
        loc = f"/?key={keyq}" if keyq else "/"
        self.send_response(303)
        self.send_header("Location", loc)
        self.end_headers()

    def _send(self, status: int, content: str, html_: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html_ else "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))
