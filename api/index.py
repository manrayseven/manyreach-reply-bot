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


def _compute_stats(actions: list) -> dict:
    """Aggrège l'historique du KV log pour donner une vue de perf simple.

    Logique :
    - On groupe par prospect (email).
    - Chaque prospect a un "état effectif" = meeting_confirmed s'il a un
      meeting_confirmed dans son historique, sinon son DERNIER intent.
    - On compte : total prospects, RDV pris, intéressés en cours, conversion.
    - On compte aussi : envoyés vs gardés vs en attente (vue activité brute).
    """
    by_email: dict[str, list] = {}
    for a in actions:
        email = (a.get("from") or "").lower().strip()
        if email:
            by_email.setdefault(email, []).append((
                a.get("at", ""),
                a.get("intent", ""),
                a.get("status", ""),
            ))

    # Counts UNIQUE par prospect (pas par action) — sinon les crashes de cron ou
    # le re-traitement avant les fixes gonflaient artificiellement les chiffres.
    sent_count = held_count = pending_count = silent_count = 0
    for email, items in by_email.items():
        statuses = " ".join((s or "") for _, _, s in items).lower()
        # Priorité au statut le plus "fort" pour ce prospect (envoyé > attente > gardé > silencieux)
        if "envoyé" in statuses:
            sent_count += 1
        elif "attente" in statuses:
            pending_count += 1
        elif "gardé" in statuses or "relire" in statuses:
            held_count += 1
        elif "silencieux" in statuses or "silent" in statuses:
            silent_count += 1

    intent_counts: dict[str, int] = {}
    meetings = 0
    interested = 0
    for email, items in by_email.items():
        items.sort(key=lambda x: x[0], reverse=True)
        has_meeting = any(i[1] == "meeting_confirmed" for i in items)
        if has_meeting:
            effective = "meeting_confirmed"
            meetings += 1
        else:
            effective = items[0][1] if items else ""
        intent_counts[effective] = intent_counts.get(effective, 0) + 1
        if effective in ("interested_warm", "interested_lukewarm", "ask_more_info"):
            interested += 1

    engaged = meetings + interested
    rate = (meetings / engaged) if engaged else 0.0
    return {
        "unique_prospects": len(by_email),
        "sent_count": sent_count,
        "held_count": held_count,
        "pending_count": pending_count,
        "silent_count": silent_count,
        "intent_counts": intent_counts,
        "meetings": meetings,
        "interested_in_pipeline": interested,
        "engaged_total": engaged,
        "meeting_rate": rate,
    }


def _render() -> str:
    enabled = kvstore.is_enabled()
    last_run = kvstore.get_last_run()
    last_run_fr = _time_fr(last_run) if last_run else "jamais"
    actions = kvstore.recent_actions(60)  # déjà du + récent au + ancien (LPUSH)
    actions_full = kvstore.recent_actions(200)  # historique élargi pour les stats
    stats = _compute_stats(actions_full)
    ov = kvstore.get_settings_overrides()
    sending = ov.get("sending", {})
    hours = sending.get("allowed_hours", [9, 19])
    min_age = sending.get("min_reply_age_minutes", 12)

    # Bloc stats — top 5 intents non-vides
    def _row(label: str, val, color: str = "#0f172a") -> str:
        return f"<div class='stat'><div class='stat-v' style='color:{color}'>{val}</div><div class='stat-l'>{label}</div></div>"
    rate_pct = f"{int(stats['meeting_rate'] * 100)}%" if stats["engaged_total"] else "—"
    top_intents = sorted(
        ((k, v) for k, v in stats["intent_counts"].items() if k and v > 0),
        key=lambda kv: kv[1], reverse=True,
    )[:6]
    intent_rows = ""
    for intent_key, count in top_intents:
        label, color = _INTENT_FR.get(intent_key, (intent_key, "#64748b"))
        intent_rows += (
            f"<tr><td><span class='pill' style='background:{color}'>{html.escape(label)}</span></td>"
            f"<td style='text-align:right;font-weight:700'>{count}</td></tr>"
        )
    if not intent_rows:
        intent_rows = "<tr><td colspan='2' style='color:#64748b;font-style:italic;padding:10px'>Pas encore de données</td></tr>"

    stats_html = f"""
    <div class="card">
      <h3>Performance ({stats['unique_prospects']} prospects sur les ~{len(actions_full)} dernières actions)</h3>
      <div class="stats-grid">
        {_row("RDV calés", stats['meetings'], "#15803d")}
        {_row("Intéressés en cours", stats['interested_in_pipeline'], "#16a34a")}
        {_row("Taux de conversion en RDV", rate_pct, "#0f172a")}
        {_row("Réponses envoyées", stats['sent_count'])}
        {_row("Gardées (fenêtre)", stats['held_count'], "#a16207")}
        {_row("Silencieuses (unsub/hostile)", stats['silent_count'], "#991b1b")}
      </div>
      <table class="intent-table">
        <thead><tr><th>État final par prospect (top 6)</th><th style="text-align:right">Nombre</th></tr></thead>
        <tbody>{intent_rows}</tbody>
      </table>
      <div class="hint">Conversion = RDV / (RDV + intéressés en cours). Les stats portent sur l'historique récent (max 200 actions stockées dans Vercel KV).</div>
    </div>
    """

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
 .btn-toggle{{background:{status_color};color:#fff}}
 .btn-run{{background:#0f172a;color:#fff}}
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
 .stats-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:14px}}
 @media (max-width:640px){{.stats-grid{{grid-template-columns:repeat(2,1fr)}}}}
 .stat{{background:#f8fafc;border:1px solid var(--line);border-radius:10px;padding:12px 14px;text-align:center}}
 .stat-v{{font-size:24px;font-weight:800;line-height:1.1}}
 .stat-l{{font-size:11px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.4px}}
 .intent-table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}}
 .intent-table th{{text-align:left;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;padding:8px 6px;border-bottom:1px solid var(--line)}}
 .intent-table td{{padding:8px 6px;border-bottom:1px solid #f1f5f9}}
</style></head><body>
<div class="top">
  <h1>📬 ManyReach Reply Bot</h1>
  <div class="sub">Pilotage en temps réel · dernier passage : {html.escape(last_run_fr)}</div>
</div>
<div class="wrap">

  <div class="card">
    <div class="statusline">
      <span class="dot"></span><span class="stxt">{status_txt}</span>
      <form method="POST" action="/{keyparam}" style="margin-left:auto;display:flex;gap:8px"
            onsubmit="var b=this.querySelector('button'); b.disabled=true; b.innerHTML='⏳ Lancement (jusqu\\'à 35s)...'; b.style.opacity=.7; return true;">
        <input type="hidden" name="action" value="run_now">
        <button class="btn-run" type="submit" title="Force un passage du bot maintenant (purge le backlog si le cron externe est en rade)">▶ Lancer maintenant</button>
      </form>
      <form method="POST" action="/{keyparam}" style="display:flex;gap:6px"
            onsubmit="var b=this.querySelector('button'); b.disabled=true; b.innerHTML='⏳...'; b.style.opacity=.7; return true;">
        <input type="hidden" name="action" value="run_email">
        <input type="email" name="only_email" placeholder="forcer 1 prospect…" required style="padding:9px 11px;border:1px solid #cbd5e1;border-radius:9px;font-size:13px;min-width:200px">
        <button class="btn-run" type="submit" title="Force le traitement IMMÉDIAT de ce prospect uniquement (bypass cache + idempotence)">▶ Pour cet email</button>
      </form>
      <form method="POST" action="/{keyparam}">
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

  {stats_html}

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
        elif action == "run_now" or action == "run_email":
            # Lancement manuel synchrone du bot. Si action=run_email + champ
            # only_email présent, on force le traitement de CE prospect précis
            # (ignore cache, idempotence thread, send_lock).
            only_email = (form.get("only_email") or "").strip().lower()
            os.environ.setdefault("LOG_DIR", "/tmp/mr-logs")
            from datetime import datetime as _dt, timezone as _tz
            log_status = "exécuté"
            try:
                scripts_dir = str(ROOT / "scripts")
                if scripts_dir not in sys.path:
                    sys.path.insert(0, scripts_dir)
                import run_bot
                old_argv = sys.argv
                os.environ["RUN_BUDGET_SECONDS"] = "35"
                if action == "run_email" and only_email:
                    # Mode ciblé : un seul prospect, on bypass tout
                    sys.argv = [
                        "run_bot",
                        "--no-dry-run",
                        "--only-email", only_email,
                        "--ignore-window",
                        "--reprocess",
                    ]
                    log_status = f"exécuté pour {only_email}"
                else:
                    sys.argv = [
                        "run_bot",
                        "--no-dry-run",
                        "--limit", "5",
                        "--ignore-window",
                        "--since-days", "3",
                    ]
                try:
                    run_bot.main()
                finally:
                    sys.argv = old_argv
            except SystemExit:
                pass
            except Exception as e:  # noqa: BLE001
                import traceback
                log_status = f"erreur ({e})"
                traceback.print_exc()
            # Trace dans le dashboard pour que Rudy voie que le bouton a bien tourné
            if kvstore.kv_available():
                kvstore.log_action({
                    "at": _dt.now(_tz.utc).isoformat(),
                    "from": "(manuel)",
                    "subject": "🖱 Lancer maintenant",
                    "intent": "run_now",
                    "status": log_status,
                    "reply": "",
                    "response": "",
                })
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
