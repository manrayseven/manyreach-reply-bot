"""Dashboard Vercel — réglages, suivi des actions, bouton stop/start.

Page unique servie sur l'URL Vercel. Lit/écrit l'état dans Vercel KV.
Protection simple : si DASHBOARD_KEY est défini, exige ?key=... dans l'URL.

GET  /            → la page HTML
POST / (form)     → actions : toggle on/off, sauver des réglages
"""
from http.server import BaseHTTPRequestHandler
import html
import json
import os
import re
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


# Compte par défaut "MOI" (Rudy) auto-créé au 1er chargement si aucun client
# n'existe. offer_context et signature VIDES = drafting historique 100% inchangé
# (draft.md / training_examples / contexte GrowPulser restent la voie par défaut).
# Rudy peut tout éditer ensuite dans la section "Comptes clients".
DEFAULT_CLIENT_SEED = {
    "id": "moi",
    "name": "Webmarketing Conseil",
    "contact_email": "contact@webmarketing-conseil.fr",
    "description": (
        "Rudy Viard, Webmarketing Conseil : conseil webmarketing, cold email, "
        "audits SEO/digital, et ses outils GrowPulser (publication automatique de "
        "contenus sur les reseaux sociaux avec IA) et GrowPoster. Campagnes "
        "envoyees en son nom propre, pas pour un client tiers."
    ),
    # Adresses d'envoi connues (vues dans les threads). Le bot en apprend d'autres
    # tout seul + tu peux en ajouter. C'est le signal de tri quand tu auras 2+ comptes.
    "mailboxes": ["rviard@hiwebmarketing.fr", "rudy@x-webmarketing.fr"],
    "campaigns": ["98002", "13754"],
    "offer_context": "",   # VIDE volontairement → réponses aux négatifs inchangées
    "signature": "",
    "is_default": True,
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


# Intents qui correspondent à une VRAIE réponse de prospect (≠ bounce/auto, ≠
# entrées système : erreurs, diagnostics, run manuel, orphelins, accusé-réception).
_NEG_INTENTS = {
    "not_interested_polite", "objection_already_have_solution",
    "objection_price", "unsubscribe", "hostile",
}
_POS_INTENTS = {
    "interested_warm", "interested_lukewarm", "ask_more_info", "meeting_confirmed",
}
# objection_timing ("plus tard") et wrong_person_redirect = ni positif ni négatif,
# mais comptent comme une réponse reçue.
_REAL_REPLY_INTENTS = _NEG_INTENTS | _POS_INTENTS | {"objection_timing", "wrong_person_redirect"}


def _perf_30d(actions: list, now=None) -> dict:
    """Suivi de perf sur 30 jours, dédupliqué PAR PROSPECT (email).

    - réponses reçues = nb de prospects uniques ayant répondu (hors bounce/auto).
    - négatives / positives = selon l'intent EFFECTIF du prospect (meeting_confirmed
      s'il existe dans son historique, sinon son dernier intent réel).
    - RDV bookés = prospects avec un meeting_confirmed.
    NB : borné par le log KV (cap MAX_LOG_ENTRIES) — si le volume dépasse ce cap
    sur 30 j, les plus anciennes entrées ne sont plus comptées.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    now = now or _dt.now(_tz.utc)
    cutoff = now - _td(days=30)

    by_email: dict[str, list] = {}
    for a in actions:
        intent = a.get("intent", "")
        if intent not in _REAL_REPLY_INTENTS:
            continue
        email = (a.get("from") or "").lower().strip()
        if not email:
            continue
        at = a.get("at", "")
        try:
            t = _dt.fromisoformat(at)
            if t.tzinfo is None:
                t = t.replace(tzinfo=_tz.utc)
            if t < cutoff:
                continue
        except (ValueError, TypeError):
            pass  # timestamp illisible → on garde par sécurité
        by_email.setdefault(email, []).append((at, intent))

    received = len(by_email)
    negative = positive = meetings = 0
    for email, items in by_email.items():
        items.sort(key=lambda x: x[0], reverse=True)
        has_meeting = any(i[1] == "meeting_confirmed" for i in items)
        effective = "meeting_confirmed" if has_meeting else items[0][1]
        if has_meeting:
            meetings += 1
        if effective in _POS_INTENTS:
            positive += 1
        elif effective in _NEG_INTENTS:
            negative += 1
    return {
        "received": received,
        "negative": negative,
        "positive": positive,
        "meetings": meetings,
    }


ALERT_INTENTS = {
    "interested_warm", "interested_lukewarm", "ask_more_info",
    "meeting_confirmed", "objection_timing",
}


def _render() -> str:
    enabled = kvstore.is_enabled()
    last_run = kvstore.get_last_run()
    last_run_fr = _time_fr(last_run) if last_run else "jamais"
    # Une seule lecture du log COMPLET (capé à MAX_LOG_ENTRIES côté KV). On
    # extrait alertes + envois + erreurs de TOUTE la fenêtre pour qu'une alerte
    # (lead chaud) ne disparaisse jamais juste à cause du volume — seule l'action
    # de Rudy (✕) la retire. Avant : alertes limitées aux 80 dernières actions →
    # un lead pouvait scroller hors vue en < 1 jour (cas jantes-alu-camping-car 16/06).
    actions_full_raw = kvstore.recent_actions(kvstore.MAX_LOG_ENTRIES)

    # FILTRE ANTI-CONTAMINATION : ce dashboard est dédié au bot ManyReach
    # uniquement. Si Rudy a un autre projet (LinkedIn / Prospect Perso) qui
    # partage le même Vercel KV, ses entrées peuvent atterrir dans le même
    # action_log → pollue cette vue. On exclut tout ce qui parle de LinkedIn /
    # cookies / autres signaux du LinkedIn bot. Filtre AVANT _compute_stats
    # pour que les KPIs ne soient pas non plus pollués.
    def _is_foreign_entry(a: dict) -> bool:
        blob = (
            str(a.get("status", ""))
            + " " + str(a.get("subject", ""))
            + " " + str(a.get("response", ""))
            + " " + str(a.get("intent", ""))
        ).lower()
        for token in ("linkedin", "cookies linkedin", "cookie linkedin", "cookies manquants", "prospect_perso", "prospect-perso"):
            if token in blob:
                return True
        return False

    actions_full = [a for a in actions_full_raw if not _is_foreign_entry(a)]
    actions = actions_full  # alertes/envois/erreurs extraits de la fenêtre complète
    perf = _perf_30d(actions_full)
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

    # Multi-clients : liste des comptes + assignations manuelles (triage).
    clients_all = kvstore.get_clients()
    if not clients_all:
        # Bootstrap serveur : crée le compte par défaut "MOI" (Rudy) pré-rempli.
        # S'exécute côté Vercel (le KV y est accessible, contrairement au local).
        # One-shot : dès qu'un client existe, on ne re-seed plus.
        try:
            kvstore.set_clients([DEFAULT_CLIENT_SEED])
            clients_all = [DEFAULT_CLIENT_SEED]
        except Exception:  # noqa: BLE001
            pass
    clients_by_id = {c.get("id"): c for c in clients_all if c.get("id")}
    triage_map = kvstore.get_triage()  # {alert_id: client_id}

    # AUTO-RÉSOLUTION DES ALERTES : pour chaque email, on note le timestamp de la
    # dernière entrée "gérée" (réponse envoyée, action silencieuse, ou reclassée en
    # refus/terminal). Une alerte ANTÉRIEURE à cette résolution est obsolète → on la
    # cache. Évite que de vieilles alertes (misclassif corrigée ensuite, ou cas déjà
    # traité auto) restent affichées maintenant qu'on lit TOUT le log (cas
    # sante-o-centre : alerté par un cron buggé, puis correctement passé NotInterested).
    _RESOLVED_INTENTS = {
        "not_interested_polite", "objection_already_have_solution", "objection_price",
        "unsubscribe", "hostile", "bounce_or_auto", "wrong_person_redirect", "ack_only",
    }
    _resolved_at: dict[str, str] = {}
    for a in actions_full:
        em = (a.get("from") or "").lower().strip()
        if not em:
            continue
        st = str(a.get("status", "")).lower()
        is_resolution = (
            "envoyé" in st or "exécuté" in st or "silencieux" in st
            or a.get("intent") in _RESOLVED_INTENTS
        )
        if is_resolution:
            at = a.get("at", "")
            if at > _resolved_at.get(em, ""):
                _resolved_at[em] = at

    # Seuils d'ancienneté des ERREURS masquées automatiquement :
    #  - erreur générique non résolue > 3 jours = bruit → cachée.
    #  - erreur TRANSITOIRE (timeout / connexion) > 4h → cachée : le bot réessaie
    #    tout seul au cron suivant, donc un timeout ponctuel se répare seul. Une
    #    erreur de timeout PERSISTANTE re-logge à chaque cron (timestamp frais) →
    #    reste visible. Évite que 1-2 timeouts réseau traînent des jours.
    try:
        from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
        _now2 = _dt2.now(_tz2.utc)
        _err_stale_cutoff = (_now2 - _td2(days=3)).isoformat()
        _err_transient_cutoff = (_now2 - _td2(hours=4)).isoformat()
    except Exception:  # noqa: BLE001
        _err_stale_cutoff = _err_transient_cutoff = ""

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
            # Timeout / erreur de connexion = transitoire (le bot réessaie) → seuil
            # court (4h). Autres erreurs → seuil 3 jours.
            _st_low = str(status).lower()
            _transient = ("timed out" in _st_low or "timeout" in _st_low
                          or "connection error" in _st_low or "connection aborted" in _st_low)
            _cutoff = _err_transient_cutoff if _transient else _err_stale_cutoff
            too_old = bool(_cutoff and err_at and err_at < _cutoff)
            if not resolved and not too_old and alert_id not in dismissed:
                error_list.append(a)
        elif intent == "wrong_person_redirect":
            # Plus jamais d'alerte pour ces cas (changement d'adresse / personne
            # partie / autoreply congés) → on les cache rétroactivement aussi
            # pour les entries loggés avant le changement.
            silent_list.append(a)
        elif intent in ALERT_INTENTS or "ALERTE" in status:
            # Cachée si une entrée PLUS RÉCENTE pour le même email montre que le
            # cas a été géré depuis (réponse envoyée / silencieux / refus).
            _em = (a.get("from") or "").lower().strip()
            superseded = bool(_em and _resolved_at.get(_em, "") > a.get("at", ""))
            # FILTRE REFUS : alerte PÉRIMÉE d'avant les fixes classifier (ex. coachs
            # "non + signature promo" mal classés interested/ask_more_info avant
            # qu'on apprenne au classifier à ignorer les signatures). Si le TEXTE du
            # reply est un refus net, ce n'est pas un vrai lead → on cache. Cohérent
            # avec la politique soft-no (refus = réponse auto, jamais d'alerte).
            _rlow = str(a.get("reply", "")).lower()
            _slow = str(a.get("subject", "")).lower()
            _is_refusal = any(ph in _rlow for ph in (
                "ne suis pas intéress", "ne sommes pas intéress", "ne suis pas interess",
                "ne m'intéresse pas", "ne nous intéresse pas", "ne m interesse pas",
                "non merci", "rien besoin", "pas de besoin", "aucun besoin",
                "pas intéressé par votre", "pas intéressée par votre", "pas interesse par votre",
                "pas un sujet pour nous", "pas un sujet chez nous", "n'est pas un sujet",
                "pas d'actualité pour nous",
            ))
            # AUTOREPLY OOO / FERMETURE / CONGÉS mal classé en alerte avant les fixes
            # (sujet "Congés/Fermeture/Absente..." souvent avec corps vide) → pas un
            # vrai lead → on cache. Idem politique : ces cas sont silencieux.
            _is_ooo = (
                any(k in _slow for k in (
                    "congé", "congés", "conges", "absente", "absence", "fermeture",
                    "out of office", "réponse automatique", "maternit", "vacances",
                ))
                or any(k in _rlow for k in (
                    "pour toute urgence", "pour toutes urgences", "sera fermé",
                    "actuellement en congé", "en congés jusqu", "actuellement absent",
                    "exceptionnellement fermé", "exceptionnellement ferme",
                    "fermé du ", "ferme du ", "fermée du ", "fermee du ",
                    "nous répondrons", "nous repondrons",
                    "répondrons à vos demandes", "repondrons a vos demandes",
                ))
            )
            # Reply SANS contenu lisible (corps vide / "[No Body Detail]") : non
            # actionnable + désormais classé bounce_or_auto par le bot → on cache.
            _no_content = (not _rlow.strip()) or ("no body detail" in _rlow)
            if (alert_id not in dismissed and not superseded
                    and not _is_refusal and not _is_ooo and not _no_content):
                alerts.append(a)
        elif "envoyé" in status:
            sent_list.append(a)
        elif intent in ("run_now", "diagnose", "manual_reply",
                         "test_resend", "retry_alerts"):
            # actions manuelles / diagnostics (+ vieux intents Resend obsolètes
            # encore présents dans le log) → ne pas polluer les compteurs
            pass
        elif intent != "bounce_or_auto":
            silent_list.append(a)

    # ENRICHISSEMENT LIVE DES ALERTES via ManyReach (mis en cache KV 30 min).
    # Pour chaque alerte affichée on récupère, depuis la SOURCE DE VÉRITÉ ManyReach :
    #  - le statut courant du prospect → masque l'alerte s'il est TERMINAL
    #    (NotInterested/Unsub/Hostile/Bounce) : couvre les alertes périmées et les
    #    cas traités À LA MAIN (aucune trace KV) — ex. sante-o-centre.
    #  - la campagne D'ORIGINE + le mailbox expéditeur d'origine (1er Sent du thread)
    #    quand le reply n'a pas de campaign_id → permet un VRAI lien ManyReach vers
    #    la conversation de départ (et non un mailto), même pour les alertes créées
    #    avant qu'on stocke ces infos. Cas auberge-grand-maison/aemn/jeanmarc.houel.
    # Fail-open : si l'appel échoue, on garde l'alerte et le lien actuel.
    _TERMINAL_MR = {
        "notinterested", "unsub", "unsubscribed", "hostile",
        "bouncehard", "bounce", "donotcontact", "blacklisted",
    }
    if alerts:
        def _ck(em: str) -> str:
            return "mrenrich:v3:" + em  # v3 = blob inclut we_spoke_last_at (supersede)

        # 1) Charge le cache pour chaque email ; collecte les emails à interroger
        #    en live (cache froid). Dédup par email (plusieurs alertes même prospect).
        enrich: dict[str, dict | None] = {}
        to_fetch: dict[str, bool] = {}  # email -> a-t-il déjà une campagne (classique)
        for a in alerts:
            em = (a.get("prospect_email") or a.get("from") or "").lower().strip()
            if not em or em in enrich or em in to_fetch:
                continue
            _raw = kvstore.cache_get(_ck(em))
            if _raw:
                try:
                    enrich[em] = json.loads(_raw)
                except (json.JSONDecodeError, TypeError):
                    enrich[em] = None
            else:
                to_fetch[em] = bool(a.get("campaign_id") or a.get("campaignId"))

        # 2) Interroge ManyReach EN PARALLÈLE (httpx.Client est thread-safe). Évite
        #    le N×latence séquentiel sur cache froid. Fail-open par email.
        _client = None
        if to_fetch:
            try:
                from src.manyreach import ManyReachClient
                _client = ManyReachClient(timeout=8.0)
            except Exception:  # noqa: BLE001
                _client = None  # fail-open : pas d'enrichissement (ex. clé absente)
        if _client is not None:
            from concurrent.futures import ThreadPoolExecutor

            def _fetch(item):
                em, has_camp = item
                data: dict = {}
                try:
                    p = _client.find_prospect_by_email(em)
                    if p:
                        data["status"] = p.sending_status or ""
                        data["prospect_email"] = p.email or em
                        # On récupère le thread pour TOUTES les alertes (classiques
                        # incluses) : il sert au SUPERSEDE — si on a répondu APRÈS le
                        # reply qui a déclenché l'alerte (bot OU Rudy à la main dans
                        # ManyReach), l'alerte est traitée et doit disparaître. Sans ça
                        # une conversation déjà répondue reste en alerte avec un lien
                        # ManyReach qui ouvre vide (cas veterinairephoenix06).
                        thr = sorted(
                            _client.get_prospect_thread(p.prospect_id),
                            key=lambda m: m.created_at,
                        )
                        # "On a parlé en dernier" = dernier message du thread est un
                        # Sent/SentManual de notre côté → date ISO du dernier message.
                        # Comparée à l'heure de l'alerte en étape 3 pour masquer.
                        if thr and thr[-1].type in ("Sent", "SentManual"):
                            data["we_spoke_last_at"] = thr[-1].created_at.isoformat()
                        # Champs in-app (réponse via API) : UNIQUEMENT pour les
                        # orphelins (hors campagne) — les classiques gardent le lien.
                        if not has_camp:
                            for m in thr:
                                if m.type in ("Sent", "SentManual") and m.campaign_id:
                                    data["campaign"] = str(m.campaign_id)
                                    data["sender"] = m.from_email or ""
                                    break
                            for m in reversed(thr):
                                if m.type == "Reply":
                                    data["reply_msgid"] = m.message_id
                                    # Message COMPLET du prospect → nécessaire pour
                                    # répondre à un orphelin depuis le dashboard
                                    # (le champ KV stocké est tronqué).
                                    try:
                                        from src.classifier import _strip_html, _trim_quoted_history
                                        data["reply_full"] = _trim_quoted_history(
                                            _strip_html(m.body or "")
                                        )[:4000]
                                    except Exception:  # noqa: BLE001
                                        pass
                                    break
                    kvstore.cache_set(_ck(em), json.dumps(data), 1800)
                    return em, data
                except Exception:  # noqa: BLE001
                    return em, None  # fail-open, ne pas cacher l'échec

            try:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    for em, data in ex.map(_fetch, list(to_fetch.items())):
                        enrich[em] = data
            finally:
                try:
                    _client.close()
                except Exception:  # noqa: BLE001
                    pass

        # 3) Applique : masque les prospects TERMINAUX, injecte les champs d'enrichi.
        #    ⚠️ On N'injecte PAS data["campaign"] dans campaign_id : un reply orphelin
        #    n'est dans aucune campagne côté ManyReach → URL inbox scopée campagne
        #    vide. La campagne d'origine reste une INFO (badge), le lien reste mailto.
        _kept = []
        for a in alerts:
            em = (a.get("prospect_email") or a.get("from") or "").lower().strip()
            data = enrich.get(em)
            if data:
                if str(data.get("status", "")).lower().strip() in _TERMINAL_MR:
                    continue  # prospect déjà terminal → masque l'alerte
                # SUPERSEDE : on a répondu (bot ou Rudy) APRÈS le reply qui a créé
                # cette alerte → conversation traitée → masque. On compare le dernier
                # Sent du thread à l'heure de l'alerte (a["at"]). Marge de 2 min pour
                # absorber les petits décalages d'horloge d'ingestion.
                _spoke = str(data.get("we_spoke_last_at") or "").strip()
                _alert_at = str(a.get("at") or "").strip()
                if _spoke and _alert_at:
                    try:
                        from datetime import datetime as _dt, timedelta as _td
                        _s = _dt.fromisoformat(_spoke)
                        _al = _dt.fromisoformat(_alert_at)
                        if _s > _al - _td(minutes=2):
                            continue  # répondu depuis l'alerte → masque
                    except Exception:  # noqa: BLE001
                        pass
                if data.get("prospect_email") and not a.get("prospect_email"):
                    a["prospect_email"] = data["prospect_email"]
                if data.get("campaign") and not a.get("origin_campaign_id"):
                    a["origin_campaign_id"] = data["campaign"]
                if data.get("sender"):
                    a["_sender_mailbox"] = data["sender"]
                if data.get("reply_msgid") and not a.get("message_id"):
                    a["message_id"] = data["reply_msgid"]
                # Message complet (orphelin) → remplace le reply STOCKÉ seulement si
                # le corps live est RÉELLEMENT meilleur (non vide, pas "[No Body
                # Detail]", et plus long que le stocké). ManyReach renvoie souvent un
                # corps vide via le thread alors que le stocké a le vrai texte capté
                # à l'ingestion → ne jamais l'écraser par du vide (cas ruben@lasolas).
                _rf = str(data.get("reply_full") or "").strip()
                if (_rf and "no body detail" not in _rf.lower()
                        and len(_rf) > len(str(a.get("reply") or "").strip())):
                    a["reply"] = _rf
            _kept.append(a)
        alerts = _kept

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
    status_color = "#2e7d52" if enabled else "#d4493f"
    status_txt = "EN MARCHE" if enabled else "EN PAUSE"
    status_pill_cls = "on" if enabled else "off"
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
        intent_label, intent_color = _INTENT_FR.get(intent, (intent, "#8a8579"))
        when = _time_fr(a.get("at", ""))
        frm = html.escape(str(a.get("from", "")))
        # Message COMPLET (plus de troncature à 300) — nécessaire pour répondre,
        # surtout aux orphelins traités depuis le dashboard.
        reply_txt = html.escape(str(a.get("reply", "")))[:4000]
        raw_alert_id = f"{a.get('at', '')}|{(a.get('from') or '').lower()}"
        alert_id = html.escape(raw_alert_id)
        # Lien direct vers l'Unibox ManyReach filtré sur cette conversation.
        # IMPORTANT : on laisse activestatus= VIDE (au lieu de '1') pour que
        # les prospects passés en NotInterested (cas typique des "↪️ Mauvaise
        # personne" où le bot a déjà mis à jour le statut) restent visibles.
        # search=from:{email} reproduit ce que Rudy tape dans la barre d'inbox.
        # Si on a aussi un campaign_id (cas du reply lié à une campagne mais
        # prospect non retrouvé directement), on l'ajoute pour narrower la vue.
        mr_org = os.environ.get("MANYREACH_ORG_ID", "7288")
        reply_from = str(a.get("from", ""))
        # Email INITIAL du prospect (celui que ManyReach connaît / à qui réécrire).
        # 'from' = l'adresse qui A RÉPONDU, parfois orpheline (boîte perso, alias
        # Orange/wanadoo…). On cherche la conversation par l'email initial.
        prospect_email = str(a.get("prospect_email") or "").strip() or reply_from
        mr_email = urllib.parse.quote(prospect_email)
        # mr_camp = campagne DU REPLY (présente = classique). Si le reply EST dans
        # une campagne, le lien inbox scopé campagne ouvre bien la conversation
        # (validé Rudy). Si le reply est ORPHELIN (campaign_id absent), sa
        # conversation n'est dans aucune campagne côté ManyReach → toute URL inbox
        # scopée campagne reste VIDE → on passe au mailto. La campagne d'ORIGINE
        # (origin_campaign_id) n'est gardée que comme info (badge), pas pour le lien.
        mr_camp = a.get("campaign_id") or a.get("campaignId") or ""
        origin_camp = str(a.get("origin_campaign_id") or "").strip()
        mr_url = (
            f"https://app.manyreach.com/e/inbox"
            f"?sender=-1&status=&leadstatus=&autostatus=&list=-1"
            f"&search=from:{mr_email}"
            f"&campaign={mr_camp}&type=campaign&activestatus=&pagesize=25&currentpage=1&o={mr_org}"
        )
        # Adresse ORPHELINE : le prospect a répondu depuis une autre adresse que son
        # email initial → à mettre en COPIE quand Rudy réécrit à l'email initial.
        orphan = reply_from if reply_from.lower().strip() != prospect_email.lower().strip() else ""
        _mailto = "mailto:" + urllib.parse.quote(prospect_email)
        if orphan:
            _mailto += "?cc=" + urllib.parse.quote(orphan)
        # Mailbox expéditeur d'origine (le compte ManyReach qui a contacté ce
        # prospect) → c'est AVEC LUI que la conversation doit se poursuivre.
        sender_mailbox = str(a.get("_sender_mailbox") or "").strip()
        # message_id du reply → on peut répondre via l'API (champ in-app).
        msgid = str(a.get("message_id") or "").strip()
        # ORPHELIN (hors campagne) = le reply n'a pas de campaign_id → ManyReach ne
        # l'affiche nulle part dans l'inbox. C'est le SEUL cas où on propose la
        # réponse in-app via API. Les CLASSIQUES (reply dans une campagne) gardent
        # juste le lien "↗ ManyReach" qui ouvre la vraie conversation.
        hors_campagne = not mr_camp
        _open_box = (
            "var d=this.closest('.alert-row').querySelector('.alert-reply');"
            "if(d){d.open=true;var t=d.querySelector('textarea');if(t)t.focus();}"
            "return false;"
        )
        _btns = []
        if mr_camp:
            # Classique → lien inbox ManyReach (marche).
            _btns.append(
                f'<a class="alert-mr" href="{mr_url}" target="_blank" rel="noopener" '
                f'title="Ouvrir la conversation dans l\'inbox ManyReach (campagne {html.escape(str(mr_camp))})">↗ ManyReach</a>'
            )
        elif msgid:
            # Orphelin → bouton qui OUVRE le champ de réponse in-app (envoi via API).
            _btns.append(
                f'<a class="alert-mr" href="#" onclick="{_open_box}" '
                f'title="Écrire une réponse envoyée dans le fil ManyReach (email orphelin)">✍ Répondre</a>'
            )
        if hors_campagne:
            # Mailto secondaire (surtout pour copier l'orpheline en CC).
            _emhint = "Ouvrir le client mail vers l'email initial"
            if orphan:
                _emhint += f" (copie {html.escape(orphan)} en CC)"
            _btns.append(
                f'<a class="alert-mr alert-mr-sec" href="{html.escape(_mailto)}" '
                f'title="{_emhint}. Peut ne rien faire sans client mail par défaut.">✉ Email</a>'
            )
        mr_link_html = "".join(_btns)
        frm = html.escape(prospect_email)
        orphan_chip = (
            f'<span class="alert-orphan" title="A répondu depuis cette adresse — '
            f'mets-la en copie de ta réponse">↩ {html.escape(orphan)}</span>'
            if orphan else ""
        )
        sender_chip = (
            f'<span class="alert-sender" title="Compte ManyReach qui a contacté ce prospect '
            f'— réponds avec celui-ci">via {html.escape(sender_mailbox)}</span>'
            if sender_mailbox else ""
        )
        # === MULTI-CLIENTS : badge client, triage manuel, mise en relation ===
        # L'assignation manuelle (triage) prime sur le client déduit par le bot.
        _assigned_cid = triage_map.get(raw_alert_id) or a.get("client_id")
        _client = clients_by_id.get(_assigned_cid) if _assigned_cid else None
        # "À trier" seulement si ça reste ambigu (2+ clients ET jamais assigné).
        _needs_triage = (
            bool(a.get("needs_triage"))
            and raw_alert_id not in triage_map
            and len(clients_all) >= 2
        )
        client_chip = (
            f'<span class="alert-client" title="Compte client rattaché">'
            f'👤 {html.escape(str(_client.get("name") or _client.get("id")))}</span>'
            if _client else ""
        )
        triage_html = ""
        if _needs_triage:
            _opts = "".join(
                f'<option value="{html.escape(str(c.get("id")))}">'
                f'{html.escape(str(c.get("name") or c.get("id")))}</option>'
                for c in clients_all if c.get("id")
            )
            triage_html = (
                f'<form method="POST" action="/{keyparam}" style="display:inline" '
                f'onsubmit="this.querySelector(\'button\').disabled=true;return true;">'
                f'<input type="hidden" name="action" value="assign_client">'
                f'<input type="hidden" name="alert_id" value="{alert_id}">'
                f'<input type="hidden" name="sender_mailbox" value="{html.escape(sender_mailbox)}">'
                f'<input type="hidden" name="campaign_id" value="{html.escape(str(mr_camp))}">'
                f'<select name="client_id" class="alert-triage-sel">{_opts}</select>'
                f'<button type="submit" class="alert-mr alert-mr-triage" '
                f'title="Rattacher à ce client (le bot apprend et triera seul ensuite)">à trier ✓</button>'
                f'</form>'
            )
        # "Mettre en relation" : mailto pré-rempli vers l'email du client, récap propre.
        mise_en_relation = ""
        if _client and (_client.get("contact_email") or "").strip():
            _phone = str(a.get("prospect_phone") or "").strip()
            _recap = "\n".join([
                "Bonjour,", "",
                "Voici un lead a reprendre :", "",
                f"- Contact : {prospect_email}",
                f"- Telephone : {_phone or 'non communique'}",
                f"- Recu le : {when}", "",
                "Son dernier message :",
                f"\"{str(a.get('reply') or '')[:900]}\"", "",
                "Bonne prise de contact.",
            ])
            _mer_href = (
                "mailto:" + urllib.parse.quote(_client["contact_email"])
                + "?subject=" + urllib.parse.quote(f"Lead a reprendre : {prospect_email}")
                + "&body=" + urllib.parse.quote(_recap)
            )
            mise_en_relation = (
                f'<a class="alert-mr alert-mr-mer" href="{html.escape(_mer_href)}" '
                f'title="Ouvre ton mail avec un recap propre a envoyer a '
                f'{html.escape(_client["contact_email"])}">🤝 Mettre en relation</a>'
            )
        # Bouton "Réponse auto" : l'IA génère une CLÔTURE POLIE (décline en douceur)
        # et l'envoie dans le fil ManyReach, puis retire l'alerte. Pour les leads que
        # Rudy ne veut pas traiter à la main. Confirmation avant envoi (vrai email).
        _ai_confirm = (
            f"Envoyer une clôture polie générée par IA à {prospect_email} ? "
            f"Le lead sera ensuite retiré des alertes."
        )
        ai_close_btn = (
            f'<form method="POST" action="/{keyparam}" style="display:inline" '
            f'onsubmit="if(!confirm(\'{_ai_confirm}\'))return false;'
            f'var b=this.querySelector(\'button\');b.disabled=true;b.innerHTML=\'⏳ IA…\';return true;">'
            f'<input type="hidden" name="action" value="ai_close">'
            f'<input type="hidden" name="prospect_id" value="{html.escape(str(a.get("prospect_id") or ""))}">'
            f'<input type="hidden" name="email" value="{html.escape(prospect_email)}">'
            f'<input type="hidden" name="alert_id" value="{alert_id}">'
            f'<button type="submit" class="alert-mr alert-mr-ai" '
            f'title="L\'IA rédige une clôture polie (décline en douceur, aucun engagement) '
            f'et l\'envoie dans le fil ManyReach, puis retire l\'alerte">🤖 Réponse auto</button>'
            f'</form>'
        )
        # RÉPONSE DANS MANYREACH (via API) : UNIQUEMENT pour les orphelins (hors
        # campagne) que l'UI ManyReach n'affiche pas. Les classiques se répondent
        # via le lien "↗ ManyReach" (vraie conversation).
        reply_box = ""
        if hors_campagne and msgid:
            ph = "Ta réponse à " + prospect_email + (f" (envoyée via {sender_mailbox})" if sender_mailbox else "")
            reply_box = f"""
          <details class="alert-reply">
            <summary>✍ Répondre dans ManyReach (email orphelin)</summary>
            <form method="POST" action="/{keyparam}" onsubmit="var b=this.querySelector('button');b.disabled=true;b.innerHTML='⏳ Envoi...';return true;">
              <input type="hidden" name="action" value="reply_manyreach">
              <input type="hidden" name="message_id" value="{html.escape(msgid)}">
              <input type="hidden" name="from_email" value="{html.escape(sender_mailbox)}">
              <input type="hidden" name="alert_id" value="{alert_id}">
              <textarea name="body" rows="4" placeholder="{html.escape(ph)}" required></textarea>
              <div class="alert-reply-actions">
                <span class="alert-reply-hint">Envoyé via l'API ManyReach (fil correct{', compte ' + html.escape(sender_mailbox) if sender_mailbox else ''}).{' Copie l’orpheline ' + html.escape(orphan) + ' manuellement si besoin.' if orphan else ''}</span>
                <button type="submit" class="btn-primary">Envoyer</button>
              </div>
            </form>
          </details>"""
        return f"""
        <div class="alert-row">
          <div class="alert-head">
            <span class="alert-intent" style="color:{intent_color};background:{intent_color}1a;border:1px solid {intent_color}33">{html.escape(intent_label)}</span>
            <a class="alert-email" href="{html.escape(_mailto)}">{frm}</a>
            {client_chip}
            {orphan_chip}
            {mr_link_html}
            {mise_en_relation}
            {triage_html}
            {ai_close_btn}
            {sender_chip}
            <span class="alert-when">{when}</span>
            <form method="POST" action="/{keyparam}" style="display:inline" onsubmit="this.querySelector('button').disabled=true;return true;">
              <input type="hidden" name="action" value="dismiss_alert">
              <input type="hidden" name="alert_id" value="{alert_id}">
              <button type="submit" class="alert-dismiss" title="Marquer comme traité (cache cette alerte)">✕</button>
            </form>
          </div>
          <div class="alert-msg">{reply_txt}</div>
          {reply_box}
        </div>"""

    def _sent_row(a: dict) -> str:
        intent = a.get("intent", "")
        intent_label, intent_color = _INTENT_FR.get(intent, (intent, "#64748b"))
        when = _time_fr(a.get("at", ""))
        frm = html.escape(str(a.get("from", "")))
        # Macaron Répondu / Sans réponse : "replied" stocké par le bot ; pour les
        # vieilles entrées, on déduit du champ response ("(pas de réponse…)" = skip).
        replied = a.get("replied")
        if replied is None:
            replied = not str(a.get("response", "")).strip().startswith("(pas de réponse")
        if replied:
            badge = '<span class="sent-tag sent-yes" title="Une réponse a été envoyée au prospect">Répondu</span>'
        else:
            badge = ('<span class="sent-tag sent-no" title="Aucune réponse envoyée '
                     '(le bot a jugé inutile de répondre) — à creuser si le message était cordial">Sans réponse</span>')
        # Lien ManyReach vers la conversation (comme les alertes).
        mr_org = os.environ.get("MANYREACH_ORG_ID", "7288")
        _pemail = urllib.parse.quote(str(a.get("prospect_email") or a.get("from") or ""))
        _camp = a.get("campaign_id") or a.get("campaignId") or ""
        _scope = f"&campaign={_camp}&type=campaign" if _camp else "&campaign=&type="
        mr_url = (
            f"https://app.manyreach.com/e/inbox?sender=-1&status=&leadstatus=&autostatus=&list=-1"
            f"&search=from:{_pemail}{_scope}&activestatus=&pagesize=25&currentpage=1&o={mr_org}"
        )
        mr_link = (
            f'<a class="sent-mr" href="{mr_url}" target="_blank" rel="noopener" '
            f'title="Ouvrir la conversation dans ManyReach">↗</a>'
        )
        return f"""<div class="sent-row">
          <span class="sent-when">{when}</span>
          <span class="sent-pill" style="color:{intent_color};border:1px solid {intent_color}40">{html.escape(intent_label)}</span>
          <span class="sent-email">{frm}</span>
          {badge}
          {mr_link}
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

    # === SECTION CLIENTS (multi-comptes) ===
    def _client_form(c: dict | None) -> str:
        c = c or {}
        cid = str(c.get("id") or "")
        is_new = not cid
        name = html.escape(str(c.get("name") or ""))
        cemail = html.escape(str(c.get("contact_email") or ""))
        desc = html.escape(str(c.get("description") or ""))
        mbx = html.escape("\n".join(c.get("mailboxes") or []))
        cps = html.escape("\n".join(str(x) for x in (c.get("campaigns") or [])))
        offer = html.escape(str(c.get("offer_context") or ""))
        sig = html.escape(str(c.get("signature") or ""))
        is_def = bool(c.get("is_default"))
        title = (
            f'➕ Nouveau client' if is_new
            else f'👤 {name or cid}'
            + (' <span class="cdefault">MOI</span>' if is_def else '')
        )
        # IMPORTANT : l'action est portée par le BOUTON cliqué (name=action), pas
        # par un champ caché — le navigateur n'envoie que le submit activé, donc
        # Enregistrer et Supprimer ne se marchent pas dessus.
        del_btn = "" if is_new else (
            f'<button type="submit" name="action" value="delete_client" class="cdel" '
            f'onclick="return confirm(\'Supprimer ce client ?\')">Supprimer</button>'
        )
        return f"""
      <div class="client-block">
        <h3>{title}</h3>
        <form method="POST" action="/{keyparam}" onsubmit="return true;">
          <input type="hidden" name="client_id" value="{html.escape(cid)}">
          <div class="client-grid">
            <div class="client-field"><label>Nom du client</label>
              <input type="text" name="name" value="{name}" placeholder="Ex. Cabinet Durand" required></div>
            <div class="client-field"><label>Email du client <span class="fhint">(pour « Mettre en relation »)</span></label>
              <input type="email" name="contact_email" value="{cemail}" placeholder="contact@client.com"></div>
            <div class="client-field full"><label>Description de l'offre <span class="fhint">(sert à deviner le bon client quand aucune adresse ne matche)</span></label>
              <textarea name="description" rows="2" placeholder="Ce que ce client vend / propose">{desc}</textarea></div>
            <div class="client-field"><label>Adresses d'envoi <span class="fhint">(une par ligne — quelques exemples suffisent)</span></label>
              <textarea name="mailboxes" rows="3" placeholder="contact@client.com&#10;pro@client.com">{mbx}</textarea></div>
            <div class="client-field"><label>Campagnes <span class="fhint">(ids, une par ligne — optionnel)</span></label>
              <textarea name="campaigns" rows="3" placeholder="98002&#10;55501">{cps}</textarea></div>
            <div class="client-field full"><label>Réponses aux négatifs <span class="fhint">(qui est ce client, quoi pitcher, quels liens — laissé vide pour « MOI » = comportement actuel)</span></label>
              <textarea name="offer_context" rows="3" placeholder="Ex. Tu réponds pour le Cabinet Durand (expertise comptable). En cas de refus, reste courtois et propose la newsletter conseils : https://...">{offer}</textarea></div>
            <div class="client-field full"><label>Signature <span class="fhint">(optionnel)</span></label>
              <textarea name="signature" rows="2" placeholder="Ex. L'équipe Durand&#10;01 23 45 67 89">{sig}</textarea></div>
          </div>
          <div class="client-actions">
            <label class="client-check"><input type="checkbox" name="is_default" value="1" {'checked' if is_def else ''}> C'est mon compte (client par défaut)</label>
            <button class="btn-save" type="submit" name="action" value="save_client">{'Créer' if is_new else 'Enregistrer'}</button>
            {del_btn}
          </div>
        </form>
      </div>"""

    clients_blocks = "".join(_client_form(c) for c in clients_all)
    clients_section_html = clients_blocks + _client_form(None)

    # Bloc perf 30 jours (dédupliqué par prospect)
    stats_html = f"""
    <div class="kpi-caption">Performance · 30 derniers jours</div>
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-v">{perf['received']}</div><div class="kpi-l"><span class="kdot" style="background:#8c8678"></span>Réponses reçues</div></div>
      <div class="kpi"><div class="kpi-v">{perf['positive']}</div><div class="kpi-l"><span class="kdot" style="background:#2e7d52"></span>Réponses positives</div></div>
      <div class="kpi"><div class="kpi-v">{perf['negative']}</div><div class="kpi-l"><span class="kdot" style="background:#d4493f"></span>Réponses négatives</div></div>
      <div class="kpi"><div class="kpi-v">{perf['meetings']}</div><div class="kpi-l"><span class="kdot" style="background:#3b6fd4"></span>Rendez-vous bookés</div></div>
    </div>"""

    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ManyReach Bot</title>
<style>
 *{{box-sizing:border-box;margin:0;padding:0}}
 body{{font-family:-apple-system,'Inter','Segoe UI',Roboto,sans-serif;background:#f7f4ee;color:#2b2823;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}}
 a{{color:#3b6fd4;text-decoration:none}}
 a:hover{{text-decoration:underline}}
 .wrap{{max-width:960px;margin:0 auto;padding:22px 20px 50px}}

 /* HEADER */
 .header{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:12px}}
 .header h1{{font-size:19px;font-weight:700;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}}
 .header h1 .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;background:{status_color};vertical-align:middle;box-shadow:0 0 0 3px {status_color}26}}
 .status-pill{{font-size:10.5px;font-weight:700;letter-spacing:.06em;padding:4px 10px;border-radius:20px}}
 .status-pill.on{{color:#2e7d52;background:#e6f0e9;border:1px solid #c2ddcc}}
 .status-pill.off{{color:#d4493f;background:#fbeae8;border:1px solid #f0cbc6}}
 .header .status{{font-size:12.5px;color:#8c8678;text-align:right;line-height:1.7}}
 .fresh-ok{{color:#2e7d52;font-weight:600}}
 .fresh-warn{{color:#b07a2b;font-weight:600}}
 .fresh-ko{{color:#c0392b;font-weight:600}}

 /* CARDS */
 .card{{background:#fff;border-radius:14px;padding:16px 18px;margin-bottom:14px;border:1px solid #ebe6dc;box-shadow:0 1px 2px rgba(60,50,30,.03)}}
 .card h2{{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#8c8678;margin-bottom:12px;display:flex;align-items:center;gap:9px}}
 .card h2 .badge{{background:#2b2823;color:#fff;font-size:11px;min-width:20px;height:20px;padding:0 6px;border-radius:20px;font-weight:700;letter-spacing:0;display:inline-flex;align-items:center;justify-content:center}}
 .card.alerts{{background:#fdf6e9;border:1px solid #f0e2c2}}
 .card.alerts h2{{color:#a07520}}
 .card.alerts h2 .badge{{background:#c98a2b}}
 .card.errors{{background:#fcefed;border:1px solid #f2cdc8}}
 .card.errors h2{{color:#b03a30}}
 .card.errors h2 .badge{{background:#d4493f}}

 /* KPI GRID */
 .kpi-caption{{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#a39c8c;margin-bottom:10px}}
 .kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}}
 @media (max-width:640px){{.kpi-grid{{grid-template-columns:repeat(2,1fr)}}}}
 .kpi{{background:#fff;border-radius:12px;padding:16px 14px;border:1px solid #ebe6dc;box-shadow:0 1px 2px rgba(60,50,30,.03);text-align:center}}
 .kpi-v{{font-family:Georgia,'Times New Roman',serif;font-size:34px;font-weight:600;color:#b3742f;line-height:1;letter-spacing:-.01em}}
 .kpi-l{{font-size:10.5px;color:#8c8678;margin-top:9px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;display:flex;align-items:center;justify-content:center;gap:6px}}
 .kdot{{display:inline-block;width:6px;height:6px;border-radius:50%}}

 /* ALERTS */
 .alert-row{{background:#fff;border:1px solid #efe7d3;border-radius:10px;padding:9px 12px;margin-top:8px}}
 .alert-head{{display:flex;align-items:center;gap:7px;margin-bottom:4px;flex-wrap:wrap}}
 .alert-intent{{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:6px;white-space:nowrap}}
 .alert-email{{font-weight:700;color:#2b2823;font-size:13px}}
 .alert-orphan{{font-size:10px;font-weight:600;color:#a07520;background:#faefd6;border:1px solid #f0e2c2;padding:1px 6px;border-radius:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
 .alert-sender{{font-size:10px;font-weight:500;color:#8c8678;background:#f3f0e9;border:1px solid #e7e1d5;padding:1px 6px;border-radius:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
 .alert-reply{{margin-top:7px}}
 .alert-reply summary{{font-size:11.5px;font-weight:600;color:#3b6fd4;cursor:pointer;list-style:none;display:inline-block;padding:1px 0}}
 .alert-reply summary::-webkit-details-marker{{display:none}}
 .alert-reply[open] summary{{margin-bottom:8px}}
 .alert-reply textarea{{width:100%;box-sizing:border-box;border:1px solid #e0d9cb;border-radius:9px;padding:10px 12px;font-size:13px;font-family:inherit;color:#2b2823;background:#fff;resize:vertical;line-height:1.5}}
 .alert-reply textarea:focus{{outline:none;border-color:#3b6fd4}}
 .alert-reply-actions{{display:flex;align-items:center;gap:12px;margin-top:8px}}
 .alert-reply-hint{{font-size:11px;color:#8c8678;margin-right:auto;line-height:1.4}}
 .alert-when{{color:#a39c8c;font-size:11px;margin-left:auto;font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}}
 .alert-msg{{color:#5c574c;font-size:13px;line-height:1.5;max-height:240px;overflow-y:auto;word-break:break-word}}
 .alert-mr{{font-size:10.5px;font-weight:700;color:#3b6fd4;background:#edf2fc;padding:2px 8px;border-radius:6px;text-decoration:none;border:1px solid #cfdcf6;transition:all .12s}}
 .alert-mr:hover{{background:#3b6fd4;color:#fff;text-decoration:none;border-color:#3b6fd4}}
 .alert-mr-sec{{color:#8c8678;background:#f3f0e9;border-color:#e7e1d5}}
 .alert-mr-sec:hover{{background:#8c8678;color:#fff;border-color:#8c8678}}
 .alert-mr-ai{{color:#6d4aff;background:#f0ecff;border-color:#d9cefb;cursor:pointer}}
 .alert-mr-ai:hover{{background:#6d4aff;color:#fff;border-color:#6d4aff;opacity:1}}
 .alert-client{{font-size:10px;font-weight:700;color:#0d7a5f;background:#e3f4ee;border:1px solid #bfe6da;padding:1px 7px;border-radius:6px;white-space:nowrap}}
 .alert-mr-mer{{color:#0d7a5f;background:#e3f4ee;border-color:#bfe6da;cursor:pointer}}
 .alert-mr-mer:hover{{background:#0d7a5f;color:#fff;border-color:#0d7a5f;opacity:1}}
 .alert-mr-triage{{color:#a07520;background:#faefd6;border-color:#f0e2c2;cursor:pointer}}
 .alert-mr-triage:hover{{background:#a07520;color:#fff;border-color:#a07520;opacity:1}}
 .alert-triage-sel{{font-size:11px;padding:2px 6px;border:1px solid #e0d9cb;border-radius:6px;background:#fff;color:#2b2823;font-family:inherit;max-width:150px}}
 /* CLIENTS */
 .client-block{{border:1px solid #ebe6dc;border-radius:11px;padding:14px;margin-bottom:12px;background:#fcfbf8}}
 .client-block h3{{font-size:13px;font-weight:700;color:#2b2823;margin-bottom:10px;display:flex;align-items:center;gap:8px}}
 .client-block .cdefault{{font-size:9.5px;font-weight:700;color:#0d7a5f;background:#e3f4ee;border:1px solid #bfe6da;padding:1px 6px;border-radius:5px;letter-spacing:.04em}}
 .client-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
 @media (max-width:640px){{.client-grid{{grid-template-columns:1fr}}}}
 .client-field{{display:flex;flex-direction:column;gap:4px}}
 .client-field.full{{grid-column:1 / -1}}
 .client-field label{{font-size:10.5px;color:#8c8678;font-weight:600;text-transform:uppercase;letter-spacing:.04em}}
 .client-field .fhint{{font-size:10.5px;color:#b8b1a2;font-weight:400;text-transform:none;letter-spacing:0}}
 .client-field input[type=text],.client-field input[type=email],.client-field textarea{{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #e0d9cb;border-radius:8px;font-size:12.5px;font-family:inherit;background:#fff;color:#2b2823;resize:vertical}}
 .client-field textarea{{line-height:1.5}}
 .client-actions{{display:flex;gap:8px;align-items:center;margin-top:10px}}
 .client-actions .cdel{{margin-left:auto;background:#fff;color:#c0392b;border:1px solid #f0cbc6;font-size:11px;padding:7px 12px}}
 .client-actions .cdel:hover{{background:#c0392b;color:#fff;opacity:1}}
 .client-check{{display:flex;align-items:center;gap:6px;font-size:11.5px;color:#5c574c;font-weight:500}}
 .alert-dismiss{{background:transparent;border:1px solid #d8c79a;color:#a07520;width:24px;height:24px;border-radius:50%;padding:0;font-size:13px;font-weight:700;line-height:1;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;transition:all .12s}}
 .alert-dismiss:hover{{background:#d4493f;color:#fff;border-color:#d4493f;transform:scale(1.08)}}
 .alert-explain{{font-size:12px;color:#6e5a2a;background:#faefd6;border:1px solid #f0e2c2;padding:9px 12px;border-radius:9px;margin-bottom:2px;line-height:1.5}}
 .alert-explain b{{color:#4a3c14}}
 .legdot{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:3px;vertical-align:middle}}

 /* ACTIONS GRID — chaque cellule = bouton + description claire */
 .action-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:4px}}
 @media (max-width:900px){{.action-grid{{grid-template-columns:1fr 1fr}}}}
 @media (max-width:600px){{.action-grid{{grid-template-columns:1fr}}}}
 .diag-banner{{margin-bottom:14px;padding:12px 14px;border-radius:9px;background:#edf2fc;color:#2c4a82;font-size:12.5px;font-weight:500;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.55;word-break:break-word;border:1px solid #cfdcf6}}
 .action-cell{{padding:16px;border-radius:12px;border:1px solid #ebe6dc;background:#fcfbf8;display:flex;flex-direction:column;gap:10px}}
 .action-cell form{{display:flex;gap:7px;align-items:stretch;margin:0}}
 .action-cell button{{flex:0 0 auto;white-space:nowrap}}
 .action-cell input[type=email]{{flex:1;min-width:0}}
 .action-cell .action-help{{font-size:12px;color:#8c8678;line-height:1.5}}
 .action-cell .action-help b{{color:#5c574c;font-weight:600}}
 .action-toggle-row{{display:flex;justify-content:flex-end;align-items:center;padding-top:16px;margin-top:16px;border-top:1px solid #efe9dd}}
 .action-toggle-row .toggle-help{{font-size:12px;color:#8c8678;margin-right:auto}}

 /* SENT FEED */
 .sent-row{{display:flex;align-items:center;gap:14px;padding:8px 0;border-bottom:1px solid #f1ece1;font-size:13px}}
 .sent-row:last-child{{border-bottom:0}}
 .sent-when{{color:#a39c8c;font-size:11.5px;font-variant-numeric:tabular-nums;min-width:90px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
 .sent-pill{{font-size:11px;font-weight:600;padding:3px 11px;border-radius:20px;white-space:nowrap;background:#fff}}
 .sent-email{{color:#5c574c;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
 .sent-tag{{font-size:10px;font-weight:700;letter-spacing:.02em;padding:2px 8px;border-radius:20px;white-space:nowrap}}
 .sent-yes{{color:#2e7d52;background:#e6f0e9;border:1px solid #c2ddcc}}
 .sent-no{{color:#a07520;background:#faefd6;border:1px solid #f0e2c2}}
 .sent-mr{{color:#3b6fd4;font-weight:700;text-decoration:none;padding:0 4px;font-size:13px}}
 .sent-mr:hover{{text-decoration:none;color:#26509e}}

 /* ERRORS */
 .error-row{{padding:11px 0;border-bottom:1px solid #f2cdc8}}
 .error-row:last-child{{border-bottom:0}}
 .error-head{{display:flex;gap:10px;font-size:12px;margin-bottom:5px}}
 .error-when{{color:#a14a40;font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
 .error-email{{color:#b03a30;font-weight:700}}
 .error-msg{{font-size:12px;color:#8a3a32;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#fff;padding:7px 11px;border-radius:8px;border:1px solid #f2cdc8}}

 .empty-section{{color:#a39c8c;text-align:center;padding:20px;font-style:italic;font-size:13px}}

 /* BUTTONS & INPUTS */
 button,input[type=submit]{{cursor:pointer;border:0;border-radius:9px;padding:10px 16px;font-size:13px;font-weight:600;font-family:inherit;transition:all .15s}}
 button:hover{{opacity:.88}}
 button:disabled{{opacity:.5;cursor:wait}}
 .btn-primary{{background:#26231e;color:#fff}}
 .btn-outline{{background:#fff;color:#3b6fd4;border:1px solid #cfdcf6}}
 .btn-outline:hover{{background:#edf2fc;opacity:1}}
 .btn-toggle{{background:#e6f0e9;color:#2e7d52;border:1px solid #c2ddcc}}
 .btn-toggle:hover{{background:#d8e9de;opacity:1}}
 .btn-save{{background:#26231e;color:#fff}}
 input[type=email],input[type=number]{{padding:10px 12px;border:1px solid #e0d9cb;border-radius:9px;font-size:13px;font-family:inherit;background:#fff;color:#2b2823}}
 input[type=email]::placeholder{{color:#b8b1a2}}
 input[type=email]{{min-width:180px}}
 input[type=number]{{width:80px}}
 .fields{{display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap}}
 .field label{{display:block;font-size:11px;color:#8c8678;margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em;font-weight:600}}

 details summary{{cursor:pointer;font-size:13px;color:#8c8678;padding:8px 0;list-style:none;font-weight:600}}
 details summary .hint{{color:#b8b1a2;font-weight:400;margin-left:6px}}
 details summary::-webkit-details-marker{{display:none}}
 details summary:before{{content:"▸ ";margin-right:4px}}
 details[open] summary:before{{content:"▾ "}}
</style></head><body>
<div class="wrap">

  <div class="header">
    <h1><span class="dot"></span>ManyReach Bot <span class="status-pill {status_pill_cls}">{status_txt}</span></h1>
    <div class="status">Dernier passage : {html.escape(last_run_fr)}<br>{last_run_freshness}</div>
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
          <button class="btn-primary" type="submit">Forcer</button>
        </form>
        <div class="action-help">Re-traite manuellement <b>UN prospect précis</b> (saisis son email). Utile si une réponse t'a échappé ou si tu veux re-essayer après un fix.</div>
      </div>
      <div class="action-cell">
        <form method="POST" action="/{keyparam}"
              onsubmit="var b=this.querySelector('button'); b.disabled=true; b.innerHTML='⏳ Diag...'; return true;">
          <input type="hidden" name="action" value="diagnose_prospect">
          <input type="email" name="only_email" placeholder="email@prospect.com" required>
          <button class="btn-outline" type="submit">Diagnostic</button>
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
      <b>Réponses de prospects que le bot ne traite pas automatiquement</b> — il te les remonte ici pour que tu décides.<br>
      <span class="legdot" style="background:#16a34a"></span><b>Intéressé chaud</b> ·
      <span class="legdot" style="background:#65a30d"></span><b>Tiède</b> ·
      <span class="legdot" style="background:#0891b2"></span><b>Demande d'infos</b> ·
      <span class="legdot" style="background:#15803d"></span><b>Propose un RDV</b> ·
      <span class="legdot" style="background:#d97706"></span><b>« Plus tard »</b> ·
      <span class="legdot" style="background:#7c3aed"></span><b>Mauvaise personne</b><br>
      Clique <b>↗ ManyReach</b> pour ouvrir la fiche du prospect et répondre · <b>✕</b> pour cacher une ligne une fois traitée.
    </div>
    {alerts_html}
  </div>

  {"<div class='card errors'><h2>❌ Erreurs récentes <span class='badge'>" + str(len(error_list)) + "</span></h2>" + errors_html + "</div>" if error_list else ""}

  <div class="card">
    <h2>✈ Envois automatiques récents <span class="badge">{len(sent_list)}</span></h2>
    {sent_html}
  </div>

  <div style="text-align:right;font-size:11px;color:#94a3b8;margin-bottom:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">
    Version : {html.escape((os.environ.get("VERCEL_GIT_COMMIT_SHA","local") or "local")[:7])}
    · branche {html.escape(os.environ.get("VERCEL_GIT_COMMIT_REF","-"))}
  </div>

  <details>
    <summary>Comptes clients<span class="hint">tri auto des réponses, réponses par client, mise en relation</span></summary>
    <div class="card">
      <div class="alert-explain" style="margin-bottom:12px">
        Chaque client a ses <b>adresses d'envoi</b> (le bot trie les réponses dessus) et sa <b>façon de répondre aux négatifs</b>.
        Tu ne mets que <b>quelques exemples</b> : quand une adresse ou une campagne inconnue arrive, le bot devine le bon client
        et, s'il hésite, l'alerte passe <b>« à trier »</b> — tu choisis une fois, il retient. Le client <b>« MOI »</b> = ton compte actuel.
      </div>
      {clients_section_html}
    </div>
  </details>

  <details>
    <summary>Réglages avancés<span class="hint">horaires d'envoi, délai</span></summary>
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
        elif action == "reply_manyreach":
            # Réponse manuelle de Rudy ENVOYÉE VIA L'API ManyReach (endpoint
            # /messages/reply). Marche même pour les replies orphelins que l'UI
            # ManyReach n'affiche pas. Envoi déclenché explicitement par Rudy.
            from datetime import datetime as _dt, timezone as _tz
            mid = (form.get("message_id") or "").strip()
            body_txt = (form.get("body") or "").strip()
            from_email = (form.get("from_email") or "").strip() or None
            alert_id = (form.get("alert_id") or "").strip()
            log_status = ""
            if not mid or not body_txt:
                log_status = "❌ Réponse ManyReach : message_id ou texte manquant"
            else:
                # texte saisi → HTML (sauts de ligne → <br>), en échappant le HTML.
                body_html = html.escape(body_txt).replace("\n", "<br>")
                try:
                    from src.manyreach import ManyReachClient
                    with ManyReachClient(timeout=12.0) as _mr:
                        _mr.send_reply(
                            message_id=mid,
                            body_html=body_html,
                            send_as_reply=True,
                            from_email=from_email,
                        )
                    log_status = f"✉️ Réponse envoyée via ManyReach{(' (compte ' + from_email + ')') if from_email else ''}"
                    # Une fois répondu → on masque l'alerte.
                    if alert_id:
                        try:
                            kvstore.dismiss_alert(alert_id)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as e:  # noqa: BLE001
                    log_status = f"❌ Réponse ManyReach échouée : {str(e)[:200]}"
            _ok = log_status.startswith("✉️")
            if kvstore.kv_available():
                # 'from' = l'email du prospect (extrait de l'alert_id "at|email")
                _pemail = alert_id.split("|", 1)[1] if "|" in alert_id else ""
                kvstore.log_action({
                    "at": _dt.now(_tz.utc).isoformat(),
                    "from": _pemail,
                    "subject": "✍ Réponse manuelle (dashboard)",
                    # succès → 'manual_reply' (apparaît en envoi) ; échec → 'error'
                    # pour être bien visible dans la carte Erreurs du dashboard.
                    "intent": "manual_reply" if _ok else "error",
                    "status": log_status,
                    "reply": "",
                    "response": body_txt[:1000],
                })
        elif action == "ai_close":
            # RÉPONSE AUTO (IA) : Rudy ne veut pas traiter ce lead à la main → l'IA
            # génère une CLÔTURE POLIE (remercie + décline en douceur, aucun
            # engagement) et l'envoie dans le fil ManyReach. L'alerte est masquée.
            from datetime import datetime as _dt, timezone as _tz
            alert_id = (form.get("alert_id") or "").strip()
            pid = (form.get("prospect_id") or "").strip()
            email = (form.get("email") or "").strip().lower()
            log_status = ""
            resp_preview = ""
            try:
                from src.manyreach import ManyReachClient
                # timeout 40s (budget Vercel = 60s) : l'envoi ManyReach délivre
                # vraiment l'email côté serveur et peut être lent (>20s observé).
                with ManyReachClient(timeout=40.0) as _mr:
                    # draft_polite_close n'utilise PAS les données prospect → on ne
                    # charge QUE le thread (par prospect_id si dispo, sinon via email).
                    # Plus d'appel get_prospect inutile.
                    thread = None
                    if pid:
                        try:
                            thread = _mr.get_prospect_thread(int(pid))
                        except Exception:  # noqa: BLE001
                            thread = None
                    if thread is None and email:
                        _p = _mr.find_prospect_by_email(email)
                        thread = _mr.get_prospect_thread(_p.prospect_id) if _p else []
                    thread = sorted(thread or [], key=lambda mm: mm.created_at)
                    replies = [mm for mm in thread if mm.type == "Reply"]
                    sents = [mm for mm in thread if mm.type in ("Sent", "SentManual")]
                    last_reply = replies[-1] if replies else None
                    # ANTI-DOUBLON : si une réponse (Sent) existe DÉJÀ après le dernier
                    # reply, le fil a déjà été répondu (par le bot, Rudy, ou un envoi
                    # précédent dont le timeout avait masqué le succès) → on ne renvoie
                    # PAS, on retire juste l'alerte. Gère l'ambiguïté d'un timeout d'envoi.
                    already_answered = bool(last_reply and any(
                        mm.created_at > last_reply.created_at for mm in sents
                    ))
                    if not replies:
                        log_status = "❌ Réponse auto : aucun reply trouvé pour ce prospect"
                    elif already_answered:
                        log_status = "✅ Déjà répondu dans le fil ManyReach — alerte retirée (pas de doublon)"
                        if alert_id:
                            try:
                                kvstore.dismiss_alert(alert_id)
                            except Exception:  # noqa: BLE001
                                pass
                    else:
                        original_outreach = sents[0] if sents else None
                        sender = original_outreach.from_email if original_outreach else None
                        # Style guide (voix de Rudy) pour le drafter.
                        try:
                            style_guide = (ROOT / "training_examples.md").read_text(encoding="utf-8")
                        except Exception:  # noqa: BLE001
                            style_guide = ""
                        from src.drafter import Drafter
                        _draft = Drafter().draft_polite_close(
                            reply=last_reply,
                            prospect=None,
                            original_outreach=original_outreach,
                            style_guide=style_guide,
                        )
                        if not _draft.body_html:
                            log_status = "❌ Réponse auto : l'IA n'a pas produit de texte"
                        else:
                            _mr.send_reply(
                                message_id=last_reply.message_id,
                                body_html=_draft.body_html,
                                send_as_reply=True,
                                from_email=sender,
                            )
                            resp_preview = _draft.body_html
                            log_status = f"🤖 Clôture IA envoyée via ManyReach{(' (compte ' + sender + ')') if sender else ''}"
                            if alert_id:
                                try:
                                    kvstore.dismiss_alert(alert_id)
                                except Exception:  # noqa: BLE001
                                    pass
            except Exception as e:  # noqa: BLE001
                log_status = f"❌ Réponse auto échouée : {str(e)[:200]}"
            # Succès = envoi IA (🤖) OU déjà répondu (✅) → pas une erreur.
            _ok = log_status.startswith("🤖") or log_status.startswith("✅")
            if kvstore.kv_available():
                _pemail = email or (alert_id.split("|", 1)[1] if "|" in alert_id else "")
                kvstore.log_action({
                    "at": _dt.now(_tz.utc).isoformat(),
                    "from": _pemail,
                    "subject": "🤖 Réponse auto IA (clôture)",
                    "intent": "manual_reply" if _ok else "error",
                    "status": log_status,
                    "reply": "",
                    "response": resp_preview[:1000],
                })
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
        elif action == "save_client":
            # Créer / mettre à jour un compte client (multi-clients).
            from src.clients import slugify
            name = (form.get("name") or "").strip()
            cid = (form.get("client_id") or "").strip()
            if name:
                clients = kvstore.get_clients()

                def _split(s: str) -> list[str]:
                    return [x.strip() for x in re.split(r"[,\n;]+", s or "") if x.strip()]

                payload = {
                    "name": name,
                    "contact_email": (form.get("contact_email") or "").strip(),
                    "description": (form.get("description") or "").strip(),
                    "mailboxes": _split(form.get("mailboxes", "")),
                    "campaigns": _split(form.get("campaigns", "")),
                    "offer_context": (form.get("offer_context") or "").strip(),
                    "signature": (form.get("signature") or "").strip(),
                    "is_default": form.get("is_default") == "1",
                }
                if not cid:
                    cid = slugify(name)
                    existing = {c.get("id") for c in clients}
                    base, i = cid, 2
                    while cid in existing:
                        cid, i = f"{base}-{i}", i + 1
                payload["id"] = cid
                # Un seul client par défaut : si celui-ci l'est, on retire le flag ailleurs.
                if payload["is_default"]:
                    for c in clients:
                        c["is_default"] = False
                # Upsert par id.
                idx = next((k for k, c in enumerate(clients) if c.get("id") == cid), None)
                if idx is None:
                    clients.append(payload)
                else:
                    clients[idx] = payload
                kvstore.set_clients(clients)
        elif action == "delete_client":
            cid = (form.get("client_id") or "").strip()
            if cid:
                kvstore.set_clients([c for c in kvstore.get_clients() if c.get("id") != cid])
        elif action == "assign_client":
            # Triage manuel d'une alerte "à trier" → on mémorise l'assignation ET
            # on APPREND (mailbox/campagne) pour que le bot trie seul ensuite.
            from src import clients as _cl
            cid = (form.get("client_id") or "").strip()
            alert_id = (form.get("alert_id") or "").strip()
            sender_mb = (form.get("sender_mailbox") or "").strip()
            camp = (form.get("campaign_id") or "").strip()
            if cid and alert_id:
                kvstore.set_triage(alert_id, cid)
                clients = kvstore.get_clients()
                match = next((c for c in clients if c.get("id") == cid), None)
                if match and (sender_mb or camp):
                    upd = _cl.learn(match, sender_mb, camp)
                    kvstore.set_clients([upd if c.get("id") == cid else c for c in clients])
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
