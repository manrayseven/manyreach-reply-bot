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


_INTENT_FR = {
    "meeting_confirmed": ("RDV confirmé", "#15803d"),
    "interested_warm": ("Intéressé chaud", "#16a34a"),
    "interested_lukewarm": ("Intéressé tiède", "#65a30d"),
    "ask_more_info": ("Demande d'infos", "#0891b2"),
    "objection_price": ("Objection prix", "#d97706"),
    "objection_timing": ("Pas le moment", "#d97706"),
    "objection_already_have_solution": ("Déjà équipé", "#b45309"),
    "wrong_person_redirect": ("Mauvaise personne", "#7c3aed"),
    "not_interested_polite": ("Pas intéressé", "#dc2626"),
    "unsubscribe": ("Désinscription", "#991b1b"),
    "hostile": ("Hostile", "#991b1b"),
    "bounce_or_auto": ("Auto/Bounce", "#94a3b8"),
    "mailinblack_pending": ("MailInBlack", "#a855f7"),
}


def _time_fr(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return iso[:16].replace("T", " ")


def _render() -> str:
    enabled = kvstore.is_enabled()
    last_run = kvstore.get_last_run()
    last_run_fr = _time_fr(last_run) if last_run else "jamais"
    actions = kvstore.recent_actions(60)  # déjà du + récent au + ancien (LPUSH)
    ov = kvstore.get_settings_overrides()
    sending = ov.get("sending", {})
    hours = sending.get("allowed_hours", [9, 19])
    min_age = sending.get("min_reply_age_minutes", 12)

    # Feed conversationnel : message reçu -> réponse du bot, du + récent au + ancien
    feed = ""
    shown = 0
    for a in actions:
        intent = a.get("intent", "")
        if intent in ("bounce_or_auto",):  # on masque le bruit pur
            continue
        label, color = _INTENT_FR.get(intent, (intent, "#64748b"))
        when = _time_fr(a.get("at", ""))
        frm = html.escape(str(a.get("from", "")))
        subj = html.escape(str(a.get("subject", "")))
        status = html.escape(str(a.get("status", "")))
        reply_txt = html.escape(str(a.get("reply", ""))).replace("\n", "<br>")
        resp_txt = html.escape(str(a.get("response", ""))).replace("\n", "<br>")
        status_bg = "#dcfce7" if "envoyé" in status else ("#fef9c3" if "relire" in status or "gardé" in status else "#f1f5f9")
        shown += 1
        feed += f"""
        <div class="msg">
          <div class="msg-head">
            <span class="pill" style="background:{color}">{html.escape(label)}</span>
            <span class="who">{frm}</span>
            <span class="when">{when}</span>
            <span class="status" style="background:{status_bg}">{status}</span>
          </div>
          <div class="subj">{subj}</div>
          <div class="bubble in"><div class="lbl">Réponse du prospect</div>{reply_txt or '<i>(vide)</i>'}</div>
          <div class="bubble out"><div class="lbl">Réponse envoyée par le bot</div>{resp_txt or '<i>(aucune)</i>'}</div>
        </div>"""
    if not feed:
        feed = "<div class='empty'>Aucun message traité pour l'instant. Dès que le bot tournera (cron-job.org), les conversations s'afficheront ici, de la plus récente à la plus ancienne.</div>"

    status_color = "#16a34a" if enabled else "#dc2626"
    status_txt = "EN MARCHE" if enabled else "EN PAUSE"
    toggle_label = "Mettre en pause" if enabled else "Réactiver le bot"
    toggle_val = "0" if enabled else "1"
    keyq = os.environ.get("DASHBOARD_KEY")
    keyparam = f"?key={keyq}" if keyq else ""

    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ManyReach Bot — Pilotage</title>
<style>
 :root{{--bg:#0f172a;--card:#fff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--brand:#4f46e5;}}
 *{{box-sizing:border-box}}
 body{{font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:#eef2f7;margin:0;color:var(--ink);}}
 .top{{background:linear-gradient(120deg,#4f46e5,#7c3aed);color:#fff;padding:22px 28px;}}
 .top h1{{margin:0;font-size:20px;letter-spacing:.2px}}
 .top .sub{{opacity:.85;font-size:13px;margin-top:3px}}
 .wrap{{max-width:880px;margin:0 auto;padding:20px 16px 60px}}
 .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:16px 0;box-shadow:0 1px 2px rgba(15,23,42,.04)}}
 .statusline{{display:flex;align-items:center;gap:14px;flex-wrap:wrap}}
 .dot{{width:11px;height:11px;border-radius:50%;display:inline-block;background:{status_color};box-shadow:0 0 0 4px {status_color}22}}
 .stxt{{font-weight:700;font-size:15px}}
 button,input[type=submit]{{cursor:pointer;border:0;border-radius:9px;padding:10px 18px;font-size:14px;font-weight:600;transition:.15s}}
 button:hover{{opacity:.9}}
 .btn-toggle{{background:{status_color};color:#fff;margin-left:auto}}
 .btn-save{{background:var(--brand);color:#fff}}
 h3{{font-size:14px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin:0 0 14px}}
 .fields{{display:flex;gap:18px;flex-wrap:wrap;align-items:flex-end}}
 .field label{{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}}
 input[type=number]{{width:90px;padding:9px;border:1px solid #cbd5e1;border-radius:9px;font-size:14px}}
 .hint{{color:var(--muted);font-size:12px;margin-top:12px}}
 .msg{{border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}}
 .msg-head{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px}}
 .pill{{color:#fff;font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px}}
 .who{{font-weight:600;font-size:13px}} .when{{color:var(--muted);font-size:12px}}
 .status{{font-size:11px;padding:3px 9px;border-radius:20px;color:#334155;margin-left:auto}}
 .subj{{color:var(--muted);font-size:12px;margin:2px 0 10px}}
 .bubble{{border-radius:10px;padding:10px 13px;font-size:14px;line-height:1.5;margin-top:6px}}
 .bubble .lbl{{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:4px}}
 .bubble.in{{background:#eff6ff;border-left:3px solid #3b82f6}}
 .bubble.out{{background:#f0fdf4;border-left:3px solid #16a34a}}
 .empty{{color:var(--muted);text-align:center;padding:30px 10px;font-size:14px}}
</style></head><body>
<div class="top">
  <h1>📬 ManyReach Reply Bot</h1>
  <div class="sub">Pilotage en temps réel · dernier passage : {html.escape(last_run_fr)}</div>
</div>
<div class="wrap">

  <div class="card">
    <div class="statusline">
      <span class="dot"></span><span class="stxt">{status_txt}</span>
      <form method="POST" action="/{keyparam}" style="margin-left:auto">
        <input type="hidden" name="action" value="toggle">
        <input type="hidden" name="enabled" value="{toggle_val}">
        <button class="btn-toggle" type="submit">{toggle_label}</button>
      </form>
    </div>
  </div>

  <div class="card">
    <h3>Réglages rapides</h3>
    <form method="POST" action="/{keyparam}">
      <input type="hidden" name="action" value="save_settings">
      <div class="fields">
        <div class="field"><label>Début envoi (h)</label><input type="number" name="hour_start" value="{hours[0]}" min="0" max="23"></div>
        <div class="field"><label>Fin envoi (h)</label><input type="number" name="hour_end" value="{hours[1]}" min="0" max="23"></div>
        <div class="field"><label>Délai mini avant réponse (min)</label><input type="number" name="min_age" value="{min_age}" min="0" max="240"></div>
        <input class="btn-save" type="submit" value="Enregistrer">
      </div>
    </form>
    <div class="hint">Les réglages complets (voix, règles RDV, cadence) sont dans le code — éditables ici dans une prochaine version.</div>
  </div>

  <div class="card">
    <h3>Conversations traitées ({shown}) — de la plus récente à la plus ancienne</h3>
    {feed}
  </div>

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
