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
    """Affiche les timestamps en HEURE DE PARIS (le KV stocke en UTC mais Rudy
    lit en local). Évite la confusion 'rien depuis 12h' alors qu'en UTC c'est OK."""
    try:
        dt = datetime.fromisoformat(iso)
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo("Europe/Paris"))
        except Exception:
            pass
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


ALERT_INTENTS = {
    "interested_warm", "interested_lukewarm", "ask_more_info",
    "meeting_confirmed", "objection_timing",
}


def _render() -> str:
    enabled = kvstore.is_enabled()
    last_run = kvstore.get_last_run()
    last_run_fr = _time_fr(last_run) if last_run else "jamais"
    actions = kvstore.recent_actions(80)
    actions_full = kvstore.recent_actions(200)
    stats = _compute_stats(actions_full)
    ov = kvstore.get_settings_overrides()
    sending = ov.get("sending", {})
    hours = sending.get("allowed_hours", [9, 19])
    min_age = sending.get("min_reply_age_minutes", 12)

    # Tri 3 catégories : alertes Rudy (priorité), envois auto, erreurs, silencieux
    alerts = []        # à traiter par Rudy (leads chauds, RDV, plus tard, redirect)
    sent_list = []     # envois auto du bot
    error_list = []    # ❌ erreurs
    silent_list = []   # silencieux + ack + autre
    dismissed = kvstore.get_dismissed_alerts()  # alertes que Rudy a déjà traitées
    for a in actions:
        intent = a.get("intent", "")
        status = a.get("status", "")
        alert_id = f"{a.get('at', '')}|{(a.get('from') or '').lower()}"
        if intent == "error" or "ERREUR" in status:
            # AUTO-RÉSOLUTION : si une autre entrée plus récente concerne le
            # même email (envoi auto, alerte, silent), c'est que le bot a retry
            # avec succès au cron suivant → on cache l'erreur. Sinon Rudy doit
            # la voir (ou la dismiss manuellement avec ✕).
            err_email = (a.get("from") or "").lower().strip()
            err_at = a.get("at", "")
            resolved = err_email and any(
                (o.get("from") or "").lower().strip() == err_email
                and o.get("at", "") > err_at
                and o.get("intent") != "error"
                and "ERREUR" not in str(o.get("status", ""))
                for o in actions
            )
            if not resolved and alert_id not in dismissed:
                error_list.append(a)
        elif intent == "wrong_person_redirect":
            # Plus jamais d'alerte pour ces cas (changement d'adresse / personne
            # partie / autoreply congés) → on les cache rétroactivement aussi
            # pour les entries loggés avant le changement.
            silent_list.append(a)
        elif intent in ALERT_INTENTS or "ALERTE" in status:
            if alert_id not in dismissed:
                alerts.append(a)
        elif "envoyé" in status:
            sent_list.append(a)
        elif intent in ("test_resend", "run_now", "retry_alerts", "diagnose"):
            # diagnostics manuels → ne pas polluer les compteurs
            pass
        elif intent != "bounce_or_auto":
            silent_list.append(a)

    keyq = os.environ.get("DASHBOARD_KEY")
    keyparam = f"?key={keyq}" if keyq else ""

    # Dernier diagnostic prospect → bannière dans la zone Actions
    last_diag = None
    for a in actions_full:
        if a.get("intent") == "diagnose":
            last_diag = a
            break
    diag_banner = ""
    if last_diag:
        when = _time_fr(last_diag.get("at", ""))
        st = str(last_diag.get("response") or last_diag.get("status", ""))
        diag_banner = (
            f'<div class="diag-banner">📋 {html.escape(when)} — {html.escape(st)}</div>'
        )

    # Couleur statut bot
    status_color = "#16a34a" if enabled else "#dc2626"
    status_txt = "EN MARCHE" if enabled else "EN PAUSE"
    toggle_label = "Mettre en pause" if enabled else "Réactiver le bot"
    toggle_val = "0" if enabled else "1"

    # Calcul "fraîcheur" du dernier passage
    last_run_freshness = ""
    if last_run:
        try:
            from datetime import datetime as _dt, timezone as _tz
            lr = _dt.fromisoformat(last_run)
            mins = int((_dt.now(_tz.utc) - lr).total_seconds() / 60)
            if mins < 10:
                last_run_freshness = f'<span class="fresh-ok">à jour ({mins} min)</span>'
            elif mins < 30:
                last_run_freshness = f'<span class="fresh-warn">il y a {mins} min</span>'
            else:
                last_run_freshness = f'<span class="fresh-ko">⚠️ il y a {mins} min — le cron ne tourne peut-être plus</span>'
        except Exception:
            pass

    def _alert_row(a: dict) -> str:
        intent = a.get("intent", "")
        intent_emoji = {
            "interested_warm": "🔥",
            "interested_lukewarm": "🟡",
            "ask_more_info": "❓",
            "meeting_confirmed": "📅",
            "objection_timing": "⏰",
            "wrong_person_redirect": "↪️",
        }.get(intent, "🔔")
        intent_label = _INTENT_FR.get(intent, (intent, "#64748b"))[0]
        when = _time_fr(a.get("at", ""))
        frm = html.escape(str(a.get("from", "")))
        reply_txt = html.escape(str(a.get("reply", "")))[:300]
        alert_id = html.escape(f"{a.get('at', '')}|{(a.get('from') or '').lower()}")
        # Lien direct vers l'Unibox ManyReach filtré sur cette conversation.
        # IMPORTANT : on laisse activestatus= VIDE (au lieu de '1') pour que
        # les prospects passés en NotInterested (cas typique des "↪️ Mauvaise
        # personne" où le bot a déjà mis à jour le statut) restent visibles.
        # search=from:{email} reproduit ce que Rudy tape dans la barre d'inbox.
        # Si on a aussi un campaign_id (cas du reply lié à une campagne mais
        # prospect non retrouvé directement), on l'ajoute pour narrower la vue.
        mr_org = os.environ.get("MANYREACH_ORG_ID", "7288")
        mr_email = urllib.parse.quote(str(a.get("from", "")))
        mr_camp = a.get("campaign_id") or a.get("campaignId") or ""
        mr_url = (
            f"https://app.manyreach.com/e/inbox"
            f"?sender=-1&status=&leadstatus=&autostatus=&list=-1"
            f"&search=from:{mr_email}"
            f"&campaign={mr_camp}&type=campaign&activestatus=&pagesize=25&currentpage=1&o={mr_org}"
        )
        return f"""
        <div class="alert-row">
          <div class="alert-head">
            <span class="alert-icon">{intent_emoji}</span>
            <span class="alert-intent">{html.escape(intent_label)}</span>
            <a class="alert-email" href="mailto:{frm}">{frm}</a>
            <a class="alert-mr" href="{mr_url}" target="_blank" rel="noopener" title="Ouvrir la fiche ManyReach pour répondre">↗ ManyReach</a>
            <span class="alert-when">{when}</span>
            <form method="POST" action="/{keyparam}" style="display:inline" onsubmit="this.querySelector('button').disabled=true;return true;">
              <input type="hidden" name="action" value="dismiss_alert">
              <input type="hidden" name="alert_id" value="{alert_id}">
              <button type="submit" class="alert-dismiss" title="Marquer comme traité (cache cette alerte)">✕</button>
            </form>
          </div>
          <div class="alert-msg">{reply_txt}</div>
        </div>"""

    def _sent_row(a: dict) -> str:
        intent = a.get("intent", "")
        intent_label, intent_color = _INTENT_FR.get(intent, (intent, "#64748b"))
        when = _time_fr(a.get("at", ""))
        frm = html.escape(str(a.get("from", "")))
        return f"""<div class="sent-row">
          <span class="sent-when">{when}</span>
          <span class="sent-pill" style="background:{intent_color}22;color:{intent_color}">{html.escape(intent_label)}</span>
          <span class="sent-email">{frm}</span>
        </div>"""

    def _error_row(a: dict) -> str:
        when = _time_fr(a.get("at", ""))
        frm = html.escape(str(a.get("from", "")))
        status = html.escape(str(a.get("status", "")))[:200]
        err_alert_id = html.escape(f"{a.get('at', '')}|{(a.get('from') or '').lower()}")
        return f"""<div class="error-row">
          <div class="error-head">
            <span class="error-when">{when}</span>
            <span class="error-email">{frm}</span>
            <form method="POST" action="/{keyparam}" style="display:inline;margin-left:auto" onsubmit="this.querySelector('button').disabled=true;return true;">
              <input type="hidden" name="action" value="dismiss_alert">
              <input type="hidden" name="alert_id" value="{err_alert_id}">
              <button type="submit" class="alert-dismiss" title="Cacher cette erreur">✕</button>
            </form>
          </div>
          <div class="error-msg">{status}</div>
        </div>"""

    # Sections HTML
    alerts_html = "".join(_alert_row(a) for a in alerts[:25])
    if not alerts_html:
        alerts_html = '<div class="empty-section">Aucune alerte à traiter — tu es à jour ✓</div>'
    sent_html = "".join(_sent_row(a) for a in sent_list[:40])
    if not sent_html:
        sent_html = '<div class="empty-section">Aucun envoi récent.</div>'
    errors_html = "".join(_error_row(a) for a in error_list[:5])

    # KPIs simples
    rate_pct = f"{int(stats['meeting_rate'] * 100)}%" if stats["engaged_total"] else "—"

    # Bloc stats compact
    stats_html = f"""
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-v">{len(alerts)}</div><div class="kpi-l">🔔 À traiter</div></div>
      <div class="kpi"><div class="kpi-v">{len(sent_list)}</div><div class="kpi-l">✓ Envoyés auto</div></div>
      <div class="kpi"><div class="kpi-v">{stats['silent_count']}</div><div class="kpi-l">🔇 Silencieux</div></div>
      <div class="kpi"><div class="kpi-v">{len(error_list)}</div><div class="kpi-l">❌ Erreurs</div></div>
    </div>"""

    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ManyReach Bot</title>
<style>
 *{{box-sizing:border-box;margin:0;padding:0}}
 body{{font-family:-apple-system,'Inter','Segoe UI',Roboto,sans-serif;background:#f5f6fa;color:#1a1d29;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}}
 a{{color:#4f46e5;text-decoration:none}}
 a:hover{{text-decoration:underline}}
 .wrap{{max-width:960px;margin:0 auto;padding:24px 20px 60px}}

 /* HEADER */
 .header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:12px}}
 .header h1{{font-size:18px;font-weight:700;letter-spacing:-.01em}}
 .header h1 .dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:{status_color};margin-right:8px;vertical-align:middle;box-shadow:0 0 0 3px {status_color}22}}
 .header .status{{font-size:13px;color:#6b7280}}
 .fresh-ok{{color:#15803d;font-weight:600}}
 .fresh-warn{{color:#b45309;font-weight:600}}
 .fresh-ko{{color:#b91c1c;font-weight:600}}

 /* CARDS */
 .card{{background:#fff;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.04),0 0 0 1px rgba(0,0,0,.04)}}
 .card h2{{font-size:13px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:#6b7280;margin-bottom:14px;display:flex;align-items:center;gap:8px}}
 .card h2 .badge{{background:#1a1d29;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;font-weight:700;letter-spacing:0}}
 .card.alerts{{background:#fffbeb;border:1px solid #fde68a}}
 .card.alerts h2{{color:#92400e}}
 .card.alerts h2 .badge{{background:#f59e0b}}
 .card.errors{{background:#fef2f2;border:1px solid #fecaca}}
 .card.errors h2{{color:#991b1b}}
 .card.errors h2 .badge{{background:#dc2626}}

 /* KPI GRID */
 .kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}}
 @media (max-width:640px){{.kpi-grid{{grid-template-columns:repeat(2,1fr)}}}}
 .kpi{{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,.04),0 0 0 1px rgba(0,0,0,.04);text-align:center}}
 .kpi-v{{font-size:28px;font-weight:800;color:#1a1d29;line-height:1}}
 .kpi-l{{font-size:11px;color:#6b7280;margin-top:6px;font-weight:500;letter-spacing:.02em;text-transform:uppercase}}

 /* ALERTS */
 .alert-row{{padding:14px 0;border-bottom:1px solid #fde68a}}
 .alert-row:last-child{{border-bottom:0;padding-bottom:0}}
 .alert-row:first-child{{padding-top:0}}
 .alert-head{{display:flex;align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap}}
 .alert-icon{{font-size:18px}}
 .alert-intent{{font-size:11px;font-weight:700;color:#92400e;text-transform:uppercase;letter-spacing:.05em;background:#fef3c7;padding:3px 8px;border-radius:6px}}
 .alert-email{{font-weight:600;color:#1a1d29;font-size:13px}}
 .alert-when{{color:#6b7280;font-size:12px;margin-left:auto}}
 .alert-msg{{color:#374151;font-size:13px;line-height:1.5;padding-left:28px}}
 .alert-mr{{font-size:11px;font-weight:700;color:#4f46e5;background:#eef2ff;padding:3px 8px;border-radius:6px;text-decoration:none;border:1px solid #c7d2fe;transition:all .12s}}
 .alert-mr:hover{{background:#4f46e5;color:#fff;text-decoration:none;border-color:#4f46e5}}
 .alert-dismiss{{background:transparent;border:1px solid #d4a93f;color:#92400e;width:24px;height:24px;border-radius:50%;padding:0;font-size:13px;font-weight:700;line-height:1;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;transition:all .12s}}
 .alert-dismiss:hover{{background:#dc2626;color:#fff;border-color:#dc2626;transform:scale(1.08)}}
 .alert-explain{{font-size:12px;color:#78350f;background:#fef3c7;border-left:3px solid #f59e0b;padding:10px 12px;border-radius:6px;margin-bottom:14px;line-height:1.55}}
 .alert-explain b{{color:#451a03}}

 /* ACTIONS GRID — chaque cellule = bouton + description claire */
 .action-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px}}
 @media (max-width:900px){{.action-grid{{grid-template-columns:1fr 1fr}}}}
 @media (max-width:600px){{.action-grid{{grid-template-columns:1fr}}}}
 .diag-banner{{margin-bottom:12px;padding:12px 14px;border-radius:8px;background:#eef2ff;color:#312e81;font-size:12.5px;font-weight:500;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.55;word-break:break-word;border:1px solid #c7d2fe}}
 .action-cell{{background:#fafafa;padding:12px 14px;border-radius:10px;border:1px solid #e5e7eb;display:flex;flex-direction:column;gap:8px}}
 .action-cell form{{display:flex;gap:6px;align-items:stretch;margin:0}}
 .action-cell button{{flex:0 0 auto;white-space:nowrap}}
 .action-cell input[type=email]{{flex:1;min-width:0}}
 .action-cell .action-help{{font-size:11.5px;color:#6b7280;line-height:1.45}}
 .action-cell .action-help b{{color:#374151}}
 .action-toggle-row{{display:flex;justify-content:flex-end;padding-top:12px;border-top:1px solid #f3f4f6}}
 .action-toggle-row .toggle-help{{font-size:11.5px;color:#6b7280;margin-right:auto;align-self:center}}

 /* SENT FEED */
 .sent-row{{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid #f3f4f6;font-size:13px}}
 .sent-row:last-child{{border-bottom:0}}
 .sent-when{{color:#9ca3af;font-size:11px;font-variant-numeric:tabular-nums;min-width:90px}}
 .sent-pill{{font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;white-space:nowrap}}
 .sent-email{{color:#374151;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}

 /* ERRORS */
 .error-row{{padding:10px 0;border-bottom:1px solid #fecaca}}
 .error-row:last-child{{border-bottom:0}}
 .error-head{{display:flex;gap:10px;font-size:12px;margin-bottom:4px}}
 .error-when{{color:#7f1d1d;font-variant-numeric:tabular-nums}}
 .error-email{{color:#991b1b;font-weight:600}}
 .error-msg{{font-size:12px;color:#7f1d1d;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#fff;padding:6px 10px;border-radius:6px}}

 .empty-section{{color:#9ca3af;text-align:center;padding:20px;font-style:italic;font-size:13px}}

 /* ACTIONS BAR */
 .actions{{display:flex;flex-wrap:wrap;gap:8px;align-items:center}}
 button,input[type=submit]{{cursor:pointer;border:0;border-radius:8px;padding:9px 16px;font-size:13px;font-weight:600;font-family:inherit;transition:opacity .15s}}
 button:hover{{opacity:.85}}
 button:disabled{{opacity:.5;cursor:wait}}
 .btn-primary{{background:#1a1d29;color:#fff}}
 .btn-toggle{{background:{status_color};color:#fff}}
 .btn-save{{background:#4f46e5;color:#fff}}
 input[type=email],input[type=number]{{padding:9px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px;font-family:inherit;background:#fff}}
 input[type=email]{{min-width:220px}}
 input[type=number]{{width:80px}}
 .fields{{display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap}}
 .field label{{display:block;font-size:11px;color:#6b7280;margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em;font-weight:500}}

 details summary{{cursor:pointer;font-size:12px;color:#6b7280;padding:8px 0;list-style:none}}
 details summary::-webkit-details-marker{{display:none}}
 details summary:before{{content:"▸ ";margin-right:4px}}
 details[open] summary:before{{content:"▾ "}}
</style></head><body>
<div class="wrap">

  <div class="header">
    <h1><span class="dot"></span>ManyReach Bot · {status_txt}</h1>
    <div class="status">Dernier passage : {html.escape(last_run_fr)} · {last_run_freshness}</div>
  </div>

  {stats_html}

  <div class="card">
    <h2>⚡ Actions</h2>
    {diag_banner}
    <div class="action-grid">
      <div class="action-cell">
        <form method="POST" action="/{keyparam}"
              onsubmit="var b=this.querySelector('button'); b.disabled=true; b.innerHTML='⏳ En cours...'; return true;">
          <input type="hidden" name="action" value="run_now">
          <button class="btn-primary" type="submit">▶ Lancer maintenant</button>
        </form>
        <div class="action-help">Force le bot à <b>scanner les réponses</b> tout de suite, sans attendre le cron auto (qui tourne toutes les 5 min). Utile si tu veux le résultat <b>immédiatement</b>.</div>
      </div>
      <div class="action-cell">
        <form method="POST" action="/{keyparam}"
              onsubmit="var b=this.querySelector('button'); b.disabled=true; b.innerHTML='⏳...'; return true;">
          <input type="hidden" name="action" value="run_email">
          <input type="email" name="only_email" placeholder="email@prospect.com" required>
          <button class="btn-primary" type="submit">▶ Forcer</button>
        </form>
        <div class="action-help">Re-traite manuellement <b>UN prospect précis</b> (saisis son email). Utile si une réponse t'a échappé ou si tu veux re-essayer après un fix.</div>
      </div>
      <div class="action-cell">
        <form method="POST" action="/{keyparam}"
              onsubmit="var b=this.querySelector('button'); b.disabled=true; b.innerHTML='⏳ Diag...'; return true;">
          <input type="hidden" name="action" value="diagnose_prospect">
          <input type="email" name="only_email" placeholder="email@prospect.com" required>
          <button class="btn-primary" type="submit" style="background:#6366f1">📋 Diagnostic</button>
        </form>
        <div class="action-help">Affiche en clair pourquoi le bot ignore un prospect (statut, idempotence, déjà traité…). Aucun envoi.</div>
      </div>
    </div>
    <div class="action-toggle-row">
      <div class="toggle-help">Stop / start du bot. En pause, il ne traite plus aucune réponse jusqu'à réactivation.</div>
      <form method="POST" action="/{keyparam}">
        <input type="hidden" name="action" value="toggle">
        <input type="hidden" name="enabled" value="{toggle_val}">
        <button class="btn-toggle" type="submit">{toggle_label}</button>
      </form>
    </div>
  </div>

  <div class="card alerts">
    <h2>🔔 Alertes à traiter <span class="badge">{len(alerts)}</span></h2>
    <div class="alert-explain">
      <b>Ce sont les réponses de prospects que le bot NE traite PAS automatiquement</b> — il te les remonte ici pour que tu décides.<br>
      🔥 <b>Intéressé chaud</b> · 🟡 <b>tiède</b> · ❓ <b>demande d'infos</b> · 📅 <b>propose un RDV</b> · ⏰ <b>"plus tard"</b> · ↪️ <b>mauvaise personne</b> (te donne un autre contact).<br>
      Clique <b>↗ ManyReach</b> pour ouvrir la fiche du prospect et répondre. Clique <b>✕</b> pour cacher une ligne une fois traitée.
    </div>
    {alerts_html}
  </div>

  {"<div class='card errors'><h2>❌ Erreurs récentes <span class='badge'>" + str(len(error_list)) + "</span></h2>" + errors_html + "</div>" if error_list else ""}

  <div class="card">
    <h2>✓ Envois automatiques récents <span class="badge">{len(sent_list)}</span></h2>
    {sent_html}
  </div>

  <div style="text-align:right;font-size:11px;color:#94a3b8;margin-bottom:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">
    Version : {html.escape((os.environ.get("VERCEL_GIT_COMMIT_SHA","local") or "local")[:7])}
    · branche {html.escape(os.environ.get("VERCEL_GIT_COMMIT_REF","-"))}
  </div>

  <details>
    <summary>Réglages avancés (horaires d'envoi, délai)</summary>
    <div class="card">
      <form method="POST" action="/{keyparam}">
        <input type="hidden" name="action" value="save_settings">
        <div class="fields">
          <div class="field"><label>Début envoi (h)</label><input type="number" name="hour_start" value="{hours[0]}" min="0" max="23"></div>
          <div class="field"><label>Fin envoi (h)</label><input type="number" name="hour_end" value="{hours[1]}" min="0" max="23"></div>
          <div class="field"><label>Délai mini (min)</label><input type="number" name="min_age" value="{min_age}" min="0" max="240"></div>
          <input class="btn-save" type="submit" value="Enregistrer">
        </div>
      </form>
    </div>
  </details>

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
        elif action == "dismiss_alert":
            alert_id = (form.get("alert_id") or "").strip()
            if alert_id:
                kvstore.dismiss_alert(alert_id)
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
                    # Mode ciblé : un seul prospect. PAS de --reprocess → on garde
                    # l'idempotence thread (si un Sent existe déjà après le reply,
                    # on skip → plus de double-envoi quand on force 2x le même).
                    # Mais si l'envoi précédent avait échoué (pas de Sent), on retry.
                    sys.argv = [
                        "run_bot",
                        "--no-dry-run",
                        "--only-email", only_email,
                        "--ignore-window",
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
        elif action == "diagnose_prospect":
            # Visibilité sur l'état exact d'un prospect : statut MR, thread,
            # idempotence, KV processed → permet à Rudy de comprendre lui-même
            # pourquoi le bot ignore un email (au lieu de me demander à chaque
            # fois).
            from datetime import datetime as _dt, timezone as _tz
            diag_email = (form.get("only_email") or "").strip().lower()
            diag_result = ""
            if diag_email:
                try:
                    from src.manyreach import ManyReachClient
                    with ManyReachClient() as mr:
                        prospect = mr.find_prospect_by_email(diag_email)
                        if not prospect:
                            diag_result = f"❌ {diag_email} introuvable dans ManyReach (pas de prospect avec cet email)"
                        else:
                            thread = mr.get_prospect_thread(prospect.prospect_id)
                            replies = sorted([m for m in thread if m.type == "Reply"], key=lambda m: m.created_at)
                            sents = sorted([m for m in thread if m.type in ("Sent", "SentManual")], key=lambda m: m.created_at)
                            last_reply = replies[-1] if replies else None
                            last_sent = sents[-1] if sents else None
                            sent_after_reply = bool(last_reply and last_sent and last_sent.created_at > last_reply.created_at)
                            kv_processed = False
                            if last_reply and kvstore.kv_available():
                                kv_processed = kvstore.is_kv_processed(last_reply.message_id)
                            allowed_statuses = ("Interested", "MaybeLater", "Neutral", "NotInterested", "CollegueReplied")
                            reasons = []
                            if prospect.sending_status not in allowed_statuses:
                                reasons.append(f"⛔ statut={prospect.sending_status} hors liste traitée par le bot")
                            if sent_after_reply:
                                reasons.append(f"⛔ Sent après Reply (idempotence — bot considère déjà répondu)")
                            if kv_processed:
                                reasons.append(f"⛔ KV processed (déjà traité dans un run précédent)")
                            if not reasons:
                                reasons.append("✅ Devrait être traité au prochain cron (5 min)")
                            diag_result = (
                                f"{diag_email} (id={prospect.prospect_id}) | "
                                f"statut={prospect.sending_status} sendingActive={prospect.sending_active} | "
                                f"Reply: {last_reply.created_at.isoformat() if last_reply else 'aucun'} | "
                                f"Sent: {last_sent.created_at.isoformat() if last_sent else 'aucun'} | "
                                f"SentAfterReply={'OUI' if sent_after_reply else 'non'} | "
                                f"KVprocessed={'OUI' if kv_processed else 'non'} | "
                                f"→ {' / '.join(reasons)}"
                            )
                except Exception as e:  # noqa: BLE001
                    diag_result = f"❌ Erreur diagnostic : {str(e)[:200]}"
            else:
                diag_result = "❌ email vide"
            if kvstore.kv_available():
                kvstore.log_action({
                    "at": _dt.now(_tz.utc).isoformat(),
                    "from": "(diagnostic)",
                    "subject": f"📋 Diag {diag_email}",
                    "intent": "diagnose",
                    "status": diag_result[:400],
                    "reply": "",
                    "response": diag_result,
                })
        elif action == "retry_failed_alerts":
            # Renvoie via Resend toutes les alertes dont la livraison a échoué
            # (typiquement HTTP 403 sandbox tant que NOTIFY_EMAIL n'était pas
            # l'email du compte Resend). Une fois NOTIFY_EMAIL fixé, ce bouton
            # rattrape les leads coincés. Dédup par email pour ne pas spammer.
            from datetime import datetime as _dt, timezone as _tz
            try:
                from src.alerts import _send_via_resend
                actions_all = kvstore.recent_actions(200)
                alert_intents = {
                    "interested_warm", "interested_lukewarm", "ask_more_info",
                    "meeting_confirmed", "objection_timing", "wrong_person_redirect",
                }
                intent_label_map = {
                    "interested_warm": "🔥 LEAD CHAUD",
                    "interested_lukewarm": "🟡 Lead tiède",
                    "ask_more_info": "❓ Demande d'infos",
                    "meeting_confirmed": "📅 RDV proposé",
                    "objection_timing": "⏰ À recontacter plus tard",
                    "wrong_person_redirect": "↪️ Mauvaise personne",
                }
                already_resent: set = set()
                ok_count = 0
                ko_count = 0
                first_detail = ""
                for a in actions_all:
                    intent = a.get("intent", "")
                    if intent not in alert_intents:
                        continue
                    status_field = str(a.get("status", "")) + " " + str(a.get("response", ""))
                    failed = (
                        "❌" in status_field
                        or "NON envoyé" in status_field
                        or "HTTP 4" in status_field
                        or "HTTP 5" in status_field
                        or "Exception" in status_field
                        or "manquant" in status_field
                    )
                    if not failed:
                        continue
                    email = (a.get("from") or "").lower().strip()
                    if not email or email in already_resent:
                        continue
                    already_resent.add(email)
                    intent_label = intent_label_map.get(intent, f"⚠️ {intent}")
                    subject = f"[Renvoi] {intent_label} — {email}"
                    body = (
                        f"Alerte précédemment NON envoyée (Resend sandbox bloquait) "
                        f"— rattrapage maintenant.\n\n"
                        f"Type     : {intent_label} ({intent})\n"
                        f"Prospect : {email}\n"
                        f"Reçu le  : {a.get('at', '?')}\n"
                        f"Sujet    : {a.get('subject', '')}\n\n"
                        f"Extrait reply :\n---\n{a.get('reply', '')}\n---\n\n"
                        f"→ À toi de répondre."
                    )
                    ok, detail = _send_via_resend(subject, body)
                    if not first_detail:
                        first_detail = detail
                    if ok:
                        ok_count += 1
                    else:
                        ko_count += 1
                    # cap dur pour rester dans le budget 60s Vercel
                    if (ok_count + ko_count) >= 25:
                        break
                log_status = (
                    f"📨 Renvoi alertes : {ok_count} OK / {ko_count} KO"
                    + (f" — 1er retour : {first_detail}" if first_detail else "")
                )
                if (ok_count + ko_count) == 0:
                    log_status = "📨 Renvoi alertes : aucune alerte en échec trouvée"
            except Exception as e:  # noqa: BLE001
                log_status = f"❌ Renvoi alertes — Exception : {str(e)[:200]}"
            if kvstore.kv_available():
                kvstore.log_action({
                    "at": _dt.now(_tz.utc).isoformat(),
                    "from": "(retry alertes)",
                    "subject": "📨 Renvoi alertes en échec",
                    "intent": "retry_alerts",
                    "status": log_status,
                    "reply": "",
                    "response": log_status,
                })
        elif action == "test_resend":
            # Test direct : appelle _send_via_resend avec un payload minimal et
            # logue le résultat (HTTP code + body extrait) pour qu'on voie tout
            # de suite si Resend est mal configuré (clé invalide, from non
            # vérifié, sandbox bloquant, etc.).
            from datetime import datetime as _dt, timezone as _tz
            try:
                from src.alerts import _send_via_resend, ALERT_EMAIL
                from_addr = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
                ok, detail = _send_via_resend(
                    subject=f"[TEST] Bot ManyReach — ping Resend {_dt.now(_tz.utc).strftime('%H:%M')}",
                    body_text=(
                        "Si tu reçois cet email, l'API Resend fonctionne et la "
                        "config est OK.\n\n"
                        f"FROM utilisé : {from_addr}\n"
                        f"TO  utilisé : {ALERT_EMAIL}\n\n"
                        "Si tu vois CETTE alerte dans le dashboard avec ✉️ envoyé "
                        "mais que tu ne reçois RIEN dans ta boite, le mail est "
                        "probablement bloqué par le sandbox Resend (free tier : "
                        "from=onboarding@resend.dev ne peut envoyer qu'à l'email "
                        "du compte Resend lui-même). Soit tu vérifies un domaine, "
                        "soit tu mets l'email du compte Resend dans NOTIFY_EMAIL."
                    ),
                )
                log_status = f"{'✅' if ok else '❌'} TEST Resend — {detail}"
            except Exception as e:  # noqa: BLE001
                log_status = f"❌ TEST Resend — Exception : {str(e)[:200]}"
            if kvstore.kv_available():
                kvstore.log_action({
                    "at": _dt.now(_tz.utc).isoformat(),
                    "from": "(test resend)",
                    "subject": "✉️ Test Resend",
                    "intent": "test_resend",
                    "status": log_status,
                    "reply": "",
                    "response": log_status,
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
        # Pas de cache navigateur sur le dashboard : on veut toujours la dernière
        # version (commit récent / fix / nouveau log) sans qu'un Ctrl+R suffise pas.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))
