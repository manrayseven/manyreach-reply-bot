"""Rattrapage des réponses jamais remontées — tours dédiés du cron (api/cron.py).

INCIDENT DU 12-13/11/2026. La recherche du prospect échouait sur une limite de
débit ManyReach (429, quota partagé avec la chaîne Lead Finder) ; le bot en
concluait « expéditeur inconnu », classait la réponse en orpheline et la
marquait traitée pour toujours. Des dizaines de vraies réponses (« vous offrez
quoi ? », « vous pouvez toujours me montrer ») n'ont été ni répondues ni
alertées. Le bug est corrigé dans run_bot (on reporte au lieu de conclure).

Rudy (13/11) : « refais le point sur les réponses pertinentes non remontées
depuis 1 mois et fais-les apparaître dans le dashboard ». Ce module le fait,
seul, en trois temps, un tour de cron chacun (l'enveloppe de 30 s est tenue) :

  1. LISTE   : les réponses que ManyReach a classées PISTE (intéressé, plus
               tard, neutre, collègue) sur RATTRAPAGE_JOURS jours, la plus
               récente par expéditeur ; plus les refus nets classés orphelins
               depuis le 09/11 (ils recevront la réponse type).
  2. TRI     : on retire ce qui a déjà eu une réponse (message SentManual
               postérieur, de Rudy ou du bot) et ce qui figure déjà dans les
               alertes du dashboard.
  3. ALERTES : chaque piste restante devient une alerte du dashboard, d'office,
               sans IA ni envoi (« 🔔 ALERTE — réponse non remontée du JJ/MM »).
               Les refus orphelins repassent dans le bot en mode --rattrapage
               (réponse type sur un refus net, alerte sinon, jamais d'envoi
               forcé).

Tout l'état vit dans le KV (privé). Aucune adresse dans le dépôt : il est public.
Pas de Vercel Cron dédié : sur l'offre Hobby, un cron Vercel ne peut tourner
qu'une fois par jour.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

ETAT = "bot:rattrapage2:etat"          # "liste" | "tri" | "alertes" | "fini"
CANDIDATS = "bot:rattrapage2:candidats"
FAIT = "bot:rattrapage2:fait"
TOUR = "bot:rattrapage2:tour"
TTL = 14 * 24 * 3600
JOURS = int(os.environ.get("RATTRAPAGE_JOURS", "31"))
ORPHELINS_DEPUIS = os.environ.get("RATTRAPAGE_DEPUIS", "2026-11-09T00:00:00+00:00")
PISTES = ("Interested", "MaybeLater", "Neutral", "CollegueReplied")
PAR_TOUR_BOT = int(os.environ.get("RATTRAPAGE_PAR_TOUR", "2"))
# Un tour dédié au plus toutes les 6 min : le passage principal garde au moins
# un tour sur deux (cron toutes les 5 min).
ECART_S = int(os.environ.get("RATTRAPAGE_ECART_S", "360"))
STATUTS_FR = {"Interested": "intéressé", "MaybeLater": "plus tard", "Neutral": "neutre",
              "CollegueReplied": "collègue", "NotInterested": "refus"}


def _kv():
    from src import kvstore
    return kvstore


def _get_json(cle: str, defaut):
    raw = _kv()._cmd("GET", cle)
    if not raw:
        return defaut
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return defaut


def _set_json(cle: str, val) -> None:
    _kv()._cmd("SET", cle, json.dumps(val, ensure_ascii=False), "EX", str(TTL))


def _page(mr, params: dict, budget_fin: float | None = None) -> list:
    """Une page de /messages ; patiente sur un 429 (quota partagé avec la
    chaîne Lead Finder) au lieu d'échouer."""
    from src.manyreach import ManyReachError
    for essai in range(6):
        try:
            return ((mr._request("GET", "/messages", params=params) or {}).get("items") or [])
        except ManyReachError as e:
            if getattr(e, "status_code", None) != 429 and "429" not in str(e):
                raise
            if budget_fin is not None and time.time() + 5 > budget_fin:
                raise
            time.sleep(4 + 2 * essai)
    raise RuntimeError("ManyReach : limite de débit persistante")


def _replies_depuis(mr, statut: str, depuis, budget_fin: float | None = None) -> list:
    """Réponses d'un statut depuis une date. Les pages arrivent de la plus
    récente à la plus ancienne : on s'arrête à la première page entièrement
    plus vieille (list_replies, lui, lit jusqu'à max_per_status)."""
    from src.manyreach import Message
    out, page = [], 1
    while page <= 60:
        items = _page(mr, {"type": "Reply", "confirmedStatus": statut, "limit": 100, "page": page}, budget_fin)
        if not items:
            break
        vieux = 0
        for it in items:
            if it.get("type") != "Reply":
                continue
            m = Message.from_api(it)
            if m.created_at < depuis:
                vieux += 1
                continue
            out.append(m)
        if vieux == len(items) or len(items) < 100:
            break
        page += 1
    return out


def rattrapage_du() -> bool:
    """True tant que le rattrapage n'est pas fini ET que le dernier tour dédié
    date de plus de ECART_S (le SET NX sert de verrou). Sur erreur : False."""
    try:
        kv = _kv()
        if not kv.kv_available():
            return False
        if ((kv._cmd("GET", ETAT) or "liste") == "fini" and not _complement_du()
                and not _espace_du()):
            return False
        return kv._cmd("SET", TOUR, "1", "EX", str(ECART_S), "NX") == "OK"
    except Exception:  # noqa: BLE001
        return False


def _relire(cands: list[dict]) -> list[dict]:
    """LIRE avant d'alerter (Rudy 14/11, cas ELA Assistant et humy.be).

    Le statut ManyReach se trompe : « je ne suis pas intéressé » classé
    « Interested » finissait en alerte. Deux cas ne vont plus au dashboard :
      - refus net / stop lisible sans IA : passage normal du bot (réponse type) ;
      - contact recommandé (« écrivez à office@… ») : Lead Finder l'inscrit
        dans la campagne (src/recommandation.py), rien à faire pour Rudy.
    """
    from src.fixed_replies import quick_intent
    from src.recommandation import lire_recommandation, refus_lisible

    out = []
    for c in cands:
        if c["statut"] == "NotInterested":
            out.append(c)
            continue
        if lire_recommandation(c.get("reply_brut") or c["reply"], c["email"]):
            continue
        if (quick_intent(c["reply"], c.get("subject") or "") in ("not_interested_polite", "unsubscribe")
                or refus_lisible(c.get("reply_brut") or c["reply"])):
            c = {**c, "statut": "NotInterested", "relu": True}
        out.append(c)
    for c in out:
        c.pop("reply_brut", None)
    return out


def _etape_liste() -> dict:
    from datetime import datetime, timedelta, timezone
    from src.classifier import _strip_html, _trim_quoted_history
    from src.manyreach import ManyReachClient, is_bounce_or_auto

    kv = _kv()
    kv._cmd("DEL", "bot:rattrapage2:tri")   # curseur du cycle précédent
    fin = time.time() + 50   # la fonction Vercel s'arrête à 60 s
    maintenant = datetime.now(timezone.utc)
    depuis = maintenant - timedelta(days=JOURS)
    # Le passage normal a 6 h pour traiter une réponse avant que le contrôle
    # ne la considère comme perdue (évite les alertes en double).
    trop_recent = (maintenant - timedelta(hours=6)).isoformat()
    orph_depuis = datetime.fromisoformat(ORPHELINS_DEPUIS)
    par_email: dict[str, dict] = {}
    with ManyReachClient() as mr:
        for st in PISTES + ("NotInterested",):
            since = orph_depuis if st == "NotInterested" else depuis
            for m in _replies_depuis(mr, st, since, fin):
                e = (m.from_email or "").lower().strip()
                if not e or "@" not in e or is_bounce_or_auto(m):
                    continue
                if "webmarketing" in e.split("@")[-1]:
                    continue  # nos propres boîtes (transferts, tests)
                if m.created_at.isoformat() > trop_recent:
                    continue
                if e in par_email and par_email[e]["at"] >= m.created_at.isoformat():
                    continue
                par_email[e] = {
                    "email": e, "statut": st, "at": m.created_at.isoformat(),
                    "message_id": m.message_id, "campaign_id": m.campaign_id,
                    "subject": m.subject,
                    "reply": _trim_quoted_history(_strip_html(m.body or ""))[:2000],
                    "reply_brut": (m.body or "")[:6000],
                }
    cands = _relire(list(par_email.values()))
    # Refus : seulement ceux que le bug du 429 a classés orphelins (et ceux que
    # _relire vient de reconnaître comme refus : ils n'ont pas de marque).
    refus = [c for c in cands if c["statut"] == "NotInterested"]
    garder_refus: set[str] = set()
    for i in range(0, len(refus), 200):
        lot = refus[i:i + 200]
        marques = kv._cmd("MGET", *[f"bot:orphan_sent_email:{c['email']}" for c in lot])
        if not isinstance(marques, list):
            raise RuntimeError("lecture KV des marques orphelin en échec")
        garder_refus |= {c["email"] for c, v in zip(lot, marques) if v}
    cands = [c for c in cands if c["statut"] != "NotInterested" or c["email"] in garder_refus
             or c.get("relu")]
    _set_json(CANDIDATS, cands)
    kv._cmd("SET", ETAT, "tri", "EX", str(TTL))
    return {"etape": "liste", "candidats": len(cands)}


def _empreinte(message_id: str) -> str:
    import hashlib
    return hashlib.sha256(str(message_id).encode()).hexdigest()[:20]


def _tri_pre_calcule() -> set[str] | None:
    """Tri fait sur le poste de Rudy le 13/11 (config/rattrapage_ids.txt) :
    utilisé UNE fois, pour le premier cycle, afin que les alertes arrivent tout
    de suite au lieu de ~2 h de lecture des envois depuis Vercel."""
    f = ROOT / "config" / "rattrapage_ids.txt"
    if not f.exists() or _kv()._cmd("GET", "bot:rattrapage2:pre_utilise"):
        return None
    ids = {l.strip() for l in f.read_text(encoding="utf-8").splitlines()
           if l.strip() and not l.startswith("#")}
    return ids or None


def _etape_tri(enveloppe_s: float) -> dict:
    from datetime import datetime
    from src.manyreach import ManyReachClient, Message

    kv = _kv()
    debut = time.time()
    cands = _get_json(CANDIDATS, [])
    pre = _tri_pre_calcule()
    if pre is not None:
        kv._cmd("SET", "bot:rattrapage2:pre_utilise", "1")
        # Réponses de Rudy envoyées depuis le tri : le contrôle quotidien les
        # écartera ; les refus orphelins gardent leur passage par le bot.
        deja: dict[str, str] = {}
        for a in kv.recent_alerts():
            e = str(a.get("from") or "").lower().strip()
            if e and str(a.get("at") or "") > deja.get(e, ""):
                deja[e] = str(a.get("at") or "")
        restants = [c for c in cands
                    if c["statut"] == "NotInterested"
                    or (_empreinte(c["message_id"]) in pre and deja.get(c["email"], "") < c["at"])]
        _set_json(CANDIDATS, restants)
        kv._cmd("SET", ETAT, "alertes", "EX", str(TTL))
        return {"etape": "tri", "pre_calcule": True, "a_remonter": len(restants)}
    plus_ancien = min((c["at"] for c in cands), default=None)
    # Lecture des réponses envoyées REPRISE d'un tour à l'autre : un mois de
    # SentManual ne tient pas dans une enveloppe de 24 s.
    curseur = _get_json("bot:rattrapage2:tri", {"page": 1, "repondu": {}})
    repondu: dict[str, str] = curseur["repondu"]
    page = int(curseur["page"])
    termine = not plus_ancien
    with ManyReachClient() as mr:
        while not termine and page <= 400:
            if time.time() - debut > enveloppe_s - 5:
                _set_json("bot:rattrapage2:tri", {"page": page, "repondu": repondu})
                return {"etape": "tri", "page_atteinte": page, "a_suivre": True}
            try:
                items = _page(mr, {"type": "SentManual", "limit": 100, "page": page}, debut + enveloppe_s)
            except Exception:  # noqa: BLE001
                # Limite de débit en fin d'enveloppe : on garde la progression.
                _set_json("bot:rattrapage2:tri", {"page": page, "repondu": repondu})
                return {"etape": "tri", "page_atteinte": page, "a_suivre": True, "pause": "429"}
            if not items:
                break
            vieux = 0
            for it in items:
                try:
                    msg = Message.from_api(it)
                except Exception:  # noqa: BLE001
                    continue
                at = msg.created_at.isoformat()
                if at < plus_ancien:
                    vieux += 1
                    continue
                t = (msg.to_email or "").lower().strip()
                if at > repondu.get(t, ""):
                    repondu[t] = at
            if vieux == len(items):
                break
            page += 1
    deja_alerte: dict[str, str] = {}
    for a in kv.recent_alerts():
        e = str(a.get("from") or "").lower().strip()
        at = str(a.get("at") or "")
        if e and at > deja_alerte.get(e, ""):
            deja_alerte[e] = at
    restants = []
    for c in cands:
        if repondu.get(c["email"], "") > c["at"]:
            continue  # déjà une réponse après la sienne
        if c["statut"] != "NotInterested" and deja_alerte.get(c["email"], "") >= c["at"]:
            continue  # déjà dans le dashboard
        restants.append(c)
    _set_json(CANDIDATS, restants)
    kv._cmd("SET", ETAT, "alertes", "EX", str(TTL))
    return {"etape": "tri", "pages_lues": page, "a_remonter": len(restants)}


def _pousser_pistes(pistes: list[dict], fait: str = FAIT) -> int:
    """Pistes en UN SEUL appel (pipeline Upstash) : une alerte = 4 commandes
    KV, et 90 alertes une par une ne tenaient pas dans un tour."""
    from datetime import datetime, timezone
    import httpx

    kv = _kv()
    maintenant = datetime.now(timezone.utc).isoformat()
    cmds = []
    for c in pistes:
        jour = c["at"][8:10] + "/" + c["at"][5:7]
        entree = json.dumps({
            "at": maintenant,
            "from": c["email"],
            "subject": c["subject"],
            "intent": "ask_more_info",
            "status": f"🔔 ALERTE — réponse non remontée du {jour} "
                      f"(ManyReach : {STATUTS_FR.get(c['statut'], c['statut'])})",
            "reply": c["reply"],
            "response": "(rien d'envoyé — à traiter par Rudy)",
            "replied": False,
            "campaign_id": c["campaign_id"],
            "prospect_email": c["email"],
            "message_id": c["message_id"],
            "client_id": c.get("client_id"),
            "client_name": c.get("client_name"),
        }, ensure_ascii=False)
        cmds += [["LPUSH", kv.ACTION_LOG_KEY, entree], ["LPUSH", kv.ALERTS_KEY, entree],
                 ["SADD", fait, c["email"]],
                 ["SET", kv.ALERTED_PREFIX + c["email"], "1", "EX", str(kv.ALERTED_TTL)]]
    cmds += [["LTRIM", kv.ACTION_LOG_KEY, "0", str(kv.MAX_LOG_ENTRIES - 1)],
             ["LTRIM", kv.ALERTS_KEY, "0", str(kv.MAX_ALERTS - 1)]]
    r = httpx.post(kv._URL.rstrip("/") + "/pipeline",
                   headers={"Authorization": f"Bearer {kv._TOKEN}"},
                   json=cmds, timeout=20.0)
    r.raise_for_status()
    return len(pistes)


# Complément du 13/11 (Rudy : « tu peux récupérer celles de moins de 2 mois ? ») :
# les pistes de 31 à 62 jours, triées sur le poste (config/rattrapage_ids_62.txt,
# empreintes seulement : le dépôt est public). Passe UNE fois, sans attendre la
# fin du cycle en cours.
COMPLEMENT = "bot:rattrapage2:complement62"


def _complement_du() -> bool:
    f = ROOT / "config" / "rattrapage_ids_62.txt"
    return f.exists() and not _kv()._cmd("GET", COMPLEMENT)


def _complement_62() -> dict:
    from datetime import datetime, timedelta, timezone
    from src.classifier import _strip_html, _trim_quoted_history
    from src.manyreach import ManyReachClient

    kv = _kv()
    ids = {l.strip() for l in (ROOT / "config" / "rattrapage_ids_62.txt").read_text(encoding="utf-8").splitlines()
           if l.strip() and not l.startswith("#")}
    fin = time.time() + 45
    depuis = datetime.now(timezone.utc) - timedelta(days=63)
    fait = set(kv._cmd("SMEMBERS", FAIT) or [])
    trouves: dict[str, dict] = {}
    with ManyReachClient() as mr:
        for st in PISTES:
            for m in _replies_depuis(mr, st, depuis, fin):
                if _empreinte(m.message_id) not in ids:
                    continue
                e = (m.from_email or "").lower().strip()
                if not e or e in fait:
                    continue
                trouves[e] = {
                    "email": e, "statut": st, "at": m.created_at.isoformat(),
                    "message_id": m.message_id, "campaign_id": m.campaign_id,
                    "subject": m.subject,
                    "reply": _trim_quoted_history(_strip_html(m.body or ""))[:2000],
                }
    n = _pousser_pistes(list(trouves.values())) if trouves else 0
    kv._cmd("SET", COMPLEMENT, str(n), "EX", str(90 * 24 * 3600))
    return {"etape": "complement62", "attendus": len(ids), "alertes": n}


# ESPACES (Rudy 14/11 : « repasse au crible Laeris, et les mêmes règles aux
# sous-comptes présents et futurs »). Chaque workspace ManyReach a sa propre clé
# (MANYREACH_API_KEY_<ID>) et ses propres données : la clé principale ne voit
# rien de Laeris. Un espace = un tour, une fois par jour ; la 1re fois sur
# 62 jours, ensuite sur 31. Tout nouvel espace entre seul dès que sa clé existe.
ESPACE_JOUR = "bot:rattrapage2:espace_jour:"     # posé pour 24 h après un contrôle
ESPACE_INIT = "bot:rattrapage2:espace_init:"     # premier contrôle (62 j) fait
ESPACE_FAIT = "bot:rattrapage2:fait:"


def _espaces() -> dict[str, str]:
    try:
        from src.manyreach import workspace_api_keys
        exclus = {x.strip().lower() for x in
                  os.environ.get("RATTRAPAGE_ESPACES_EXCLUS", "cmaclim").split(",") if x.strip()}
        return {k: v for k, v in workspace_api_keys().items() if k not in exclus}
    except Exception:  # noqa: BLE001
        return {}


def _espace_du() -> str | None:
    kv = _kv()
    for sp in sorted(_espaces()):
        if not kv._cmd("GET", ESPACE_JOUR + sp):
            return sp
    return None


def _controle_espace(sp: str, enveloppe_s: float) -> dict:
    from datetime import datetime, timedelta, timezone
    from src.classifier import _strip_html, _trim_quoted_history
    from src.manyreach import ManyReachClient, Message, is_bounce_or_auto
    from src.fixed_replies import quick_intent
    from src.recommandation import lire_recommandation, refus_lisible

    kv = _kv()
    debut = time.time()
    fin = debut + max(10.0, enveloppe_s - 4)
    cle = _espaces().get(sp)
    premier = not kv._cmd("GET", ESPACE_INIT + sp)
    jours = 62 if premier else JOURS
    maintenant = datetime.now(timezone.utc)
    depuis = maintenant - timedelta(days=jours)
    trop_recent = (maintenant - timedelta(hours=6)).isoformat()
    fait_cle = ESPACE_FAIT + sp
    fait = set(kv._cmd("SMEMBERS", fait_cle) or [])
    par_email: dict[str, dict] = {}
    repondu: dict[str, str] = {}
    with ManyReachClient(api_key=cle) as mr:
        for st in PISTES:
            for m in _replies_depuis(mr, st, depuis, fin):
                e = (m.from_email or "").lower().strip()
                if not e or "@" not in e or e in fait or is_bounce_or_auto(m):
                    continue
                dom = e.split("@")[-1]
                if "webmarketing" in dom or sp.replace("-", "") in dom.replace("-", ""):
                    continue   # nos boîtes et celles du client
                at = m.created_at.isoformat()
                if at > trop_recent or (e in par_email and par_email[e]["at"] >= at):
                    continue
                propre = _trim_quoted_history(_strip_html(m.body or ""))
                if lire_recommandation(m.body or "", e):
                    continue   # Lead Finder inscrit le contact recommandé
                if (quick_intent(propre, m.subject or "") in ("not_interested_polite", "unsubscribe")
                        or refus_lisible(m.body or "")):
                    continue   # refus net : rien à traiter
                par_email[e] = {
                    "email": e, "statut": st, "at": at, "message_id": m.message_id,
                    "campaign_id": m.campaign_id, "subject": m.subject,
                    "reply": propre[:2000], "client_id": sp, "client_name": sp.capitalize(),
                }
        plus_ancien = min((c["at"] for c in par_email.values()), default=None)
        page = 1
        while plus_ancien and page <= 60:
            if time.time() > fin:
                return {"etape": "espace", "espace": sp, "a_suivre": "temps"}
            items = _page(mr, {"type": "SentManual", "limit": 100, "page": page}, fin)
            if not items:
                break
            vieux = 0
            for it in items:
                try:
                    msg = Message.from_api(it)
                except Exception:  # noqa: BLE001
                    continue
                at = msg.created_at.isoformat()
                if at < plus_ancien:
                    vieux += 1
                    continue
                t = (msg.to_email or "").lower().strip()
                repondu[t] = max(repondu.get(t, ""), at)
            if vieux == len(items) or len(items) < 100:
                break
            page += 1
    deja: dict[str, str] = {}
    for a in kv.recent_alerts():
        e = str(a.get("from") or "").lower().strip()
        if e:
            deja[e] = max(deja.get(e, ""), str(a.get("at") or ""))
    restants = []
    for c in par_email.values():
        if repondu.get(c["email"], "") > c["at"] or deja.get(c["email"], "") >= c["at"]:
            continue
        if kv.was_alerted(c["email"]):
            continue   # déjà montré une fois (alerte peut-être sortie des 500)
        restants.append(c)
    n = _pousser_pistes(restants, fait_cle) if restants else 0
    kv._cmd("SET", ESPACE_JOUR + sp, "1", "EX", str(int(os.environ.get("RATTRAPAGE_CYCLE_S", "86400"))))
    kv._cmd("SET", ESPACE_INIT + sp, maintenant.isoformat())
    kv._cmd("SET", "bot:rattrapage2:espace_bilan:" + sp,
            json.dumps({"at": maintenant.isoformat(), "jours": jours,
                        "lus": len(par_email), "alertes": n}), "EX", str(30 * 86400))
    return {"etape": "espace", "espace": sp, "jours": jours, "lus": len(par_email), "alertes": n}


def _etape_alertes(enveloppe_s: float) -> dict:
    from datetime import datetime, timezone

    kv = _kv()
    debut = time.time()
    cands = _get_json(CANDIDATS, [])
    fait = set(kv._cmd("SMEMBERS", FAIT) or [])
    out = {"etape": "alertes", "alertes": 0, "bot": []}
    old_argv = sys.argv
    old_budget = os.environ.get("RUN_BUDGET_SECONDS")
    try:
        # PISTES EN UN SEUL APPEL (pipeline Upstash) : une alerte = 4 commandes
        # KV, et 90 alertes une par une ne tenaient pas dans un tour.
        pistes = [c for c in cands if c["statut"] != "NotInterested" and c["email"] not in fait]
        if pistes:
            out["alertes"] = _pousser_pistes(pistes)
            fait |= {c["email"] for c in pistes}
        for c in cands:
            if c["email"] in fait:
                continue
            if time.time() - debut > enveloppe_s - 3:
                break   # le reste au tour suivant
            if c["statut"] != "NotInterested":
                # Piste : alerte d'office, sans IA ni envoi. Rien ne doit pouvoir
                # la faire taire une seconde fois.
                if str(kv._cmd("SADD", FAIT, c["email"])) != "1":
                    continue
                jour = c["at"][8:10] + "/" + c["at"][5:7]
                kv.log_action({
                    "at": datetime.now(timezone.utc).isoformat(),
                    "from": c["email"],
                    "subject": c["subject"],
                    "intent": "ask_more_info",
                    "status": f"🔔 ALERTE — réponse non remontée du {jour} "
                              f"(ManyReach : {STATUTS_FR.get(c['statut'], c['statut'])})",
                    "reply": c["reply"],
                    "response": "(rien d'envoyé — à traiter par Rudy)",
                    "replied": False,
                    "campaign_id": c["campaign_id"],
                    "prospect_email": c["email"],
                    "message_id": c["message_id"],
                })
                out["alertes"] += 1
                continue
            # Refus orphelin : passage normal du bot (réponse type si refus net).
            reste = enveloppe_s - (time.time() - debut)
            if len(out["bot"]) >= PAR_TOUR_BOT or reste < 12:
                continue
            if str(kv._cmd("SADD", FAIT, c["email"])) != "1":
                continue
            import run_bot
            os.environ["RUN_BUDGET_SECONDS"] = str(int(max(8, reste - 3)))
            sys.argv = ["run_bot", "--no-dry-run", "--rattrapage", c["email"], "--ignore-window"]
            statut = "ok"
            try:
                run_bot.main()
            except SystemExit:
                pass
            except Exception as e:  # noqa: BLE001
                statut = f"erreur : {e}"
                kv._cmd("SREM", FAIT, c["email"])
            out["bot"].append({"email": c["email"], "statut": statut})
    finally:
        sys.argv = old_argv
        # Le lambda reste chaud : ne pas laisser notre budget au passage suivant.
        if old_budget is None:
            os.environ.pop("RUN_BUDGET_SECONDS", None)
        else:
            os.environ["RUN_BUDGET_SECONDS"] = old_budget
    fait = set(kv._cmd("SMEMBERS", FAIT) or [])
    out["restants"] = sum(1 for c in cands if c["email"] not in fait)
    if out["restants"] == 0:
        # CONTRÔLE PERMANENT (Rudy 13/11 : « ça ne doit plus JAMAIS se
        # répéter ») : l'état « fini » expire au bout de 24 h, et le cycle
        # liste → tri → alertes recommence. Tout ce qui a déjà une réponse,
        # une alerte ou un passage de rattrapage (FAIT) est écarté : seule une
        # piste réellement perdue remonte.
        kv._cmd("SET", ETAT, "fini", "EX", str(int(os.environ.get("RATTRAPAGE_CYCLE_S", "86400"))))
    return out


def run_rattrapage(enveloppe_s: float = 24.0) -> dict:
    if _complement_du():
        return _complement_62()
    sp = _espace_du()
    if sp:
        return _controle_espace(sp, enveloppe_s)
    etat = _kv()._cmd("GET", ETAT) or "liste"   # absent = nouveau cycle
    if etat == "liste":
        return _etape_liste()
    if etat == "tri":
        return _etape_tri(enveloppe_s)
    if etat == "alertes":
        return _etape_alertes(enveloppe_s)
    return {"etape": "fini"}


class handler(BaseHTTPRequestHandler):
    """Appel manuel possible (même secret que /api/cron) : force un tour."""

    def do_GET(self):
        # /api/rattrapage?statut : chiffres seulement (aucune adresse, aucun
        # texte), pour vérifier que le contrôle tourne sans exposer de secret.
        if "statut" in (self.path or ""):
            try:
                kv = _kv()
                self._json(200, {
                    "etape": kv._cmd("GET", ETAT) or "liste",
                    "candidats": len(_get_json(CANDIDATS, [])),
                    "traites_total": kv._cmd("SCARD", FAIT),
                    "tri_pre_calcule_utilise": bool(kv._cmd("GET", "bot:rattrapage2:pre_utilise")),
                    "complement_62j": kv._cmd("GET", COMPLEMENT),
                    "espaces": {sp: _get_json("bot:rattrapage2:espace_bilan:" + sp, None)
                                for sp in sorted(_espaces())},
                    "alertes_dashboard": kv.alerts_count(),
                })
            except Exception as e:  # noqa: BLE001
                self._json(200, {"ok": False, "error": type(e).__name__})
            return
        secret = os.environ.get("CRON_SECRET")
        if secret and self.headers.get("Authorization", "") != f"Bearer {secret}":
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        os.environ.setdefault("LOG_DIR", "/tmp/mr-logs")
        try:
            self._json(200, {"ok": True, **run_rattrapage()})
        except Exception as e:  # noqa: BLE001
            self._json(200, {"ok": False, "error": str(e)})

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
