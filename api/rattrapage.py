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
        if (kv._cmd("GET", ETAT) or "liste") == "fini":
            return False
        return kv._cmd("SET", TOUR, "1", "EX", str(ECART_S), "NX") == "OK"
    except Exception:  # noqa: BLE001
        return False


def _etape_liste() -> dict:
    from datetime import datetime, timedelta, timezone
    from src.classifier import _strip_html, _trim_quoted_history
    from src.manyreach import ManyReachClient, is_bounce_or_auto

    kv = _kv()
    fin = time.time() + 50   # la fonction Vercel s'arrête à 60 s
    depuis = datetime.now(timezone.utc) - timedelta(days=JOURS)
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
                if e in par_email and par_email[e]["at"] >= m.created_at.isoformat():
                    continue
                par_email[e] = {
                    "email": e, "statut": st, "at": m.created_at.isoformat(),
                    "message_id": m.message_id, "campaign_id": m.campaign_id,
                    "subject": m.subject,
                    "reply": _trim_quoted_history(_strip_html(m.body or ""))[:2000],
                }
    cands = list(par_email.values())
    # Refus : seulement ceux que le bug du 429 a classés orphelins.
    refus = [c for c in cands if c["statut"] == "NotInterested"]
    garder_refus: set[str] = set()
    for i in range(0, len(refus), 200):
        lot = refus[i:i + 200]
        marques = kv._cmd("MGET", *[f"bot:orphan_sent_email:{c['email']}" for c in lot])
        if not isinstance(marques, list):
            raise RuntimeError("lecture KV des marques orphelin en échec")
        garder_refus |= {c["email"] for c, v in zip(lot, marques) if v}
    cands = [c for c in cands if c["statut"] != "NotInterested" or c["email"] in garder_refus]
    _set_json(CANDIDATS, cands)
    kv._cmd("SET", ETAT, "tri", "EX", str(TTL))
    return {"etape": "liste", "candidats": len(cands)}


def _etape_tri(enveloppe_s: float) -> dict:
    from datetime import datetime
    from src.manyreach import ManyReachClient, Message

    kv = _kv()
    debut = time.time()
    cands = _get_json(CANDIDATS, [])
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
        for c in cands:
            if c["email"] in fait:
                continue
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
        kv._cmd("SET", ETAT, "fini", "EX", str(TTL))
    return out


def run_rattrapage(enveloppe_s: float = 24.0) -> dict:
    etat = _kv()._cmd("GET", ETAT) or "liste"
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
