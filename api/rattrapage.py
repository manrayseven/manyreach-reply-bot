"""Rattrapage des réponses perdues — un tour sur deux du cron (api/cron.py).

INCIDENT DU 12-13/11/2026. La recherche du prospect échouait sur une limite de
débit ManyReach (429, quota partagé avec la chaîne Lead Finder) ; le bot en
concluait « expéditeur inconnu », classait la réponse en orpheline et la
marquait traitée pour toujours. Des dizaines de vraies réponses (« vous offrez
quoi ? », « vous pouvez toujours me montrer ») n'ont ainsi été ni répondues ni
alertées. Le bug est corrigé dans run_bot (on reporte au lieu de conclure).

Ce module retrouve SEUL les victimes : les réponses classées par ManyReach
(intéressé, neutre, plus tard, pas intéressé) depuis le RATTRAPAGE_DEPUIS dont
l'expéditeur porte la marque « orphelin » du KV — un vrai prospect ne devrait
jamais l'avoir. La liste est calculée une fois et gardée dans le KV (privé).
Il les retraite une ou deux par tour, en mode --rattrapage : un passage NORMAL du bot (alerte pour une
piste, réponse type seulement sur un refus net), sans l'envoi forcé du bouton
« Pour cet email ». Idempotence du fil conservée : si une réponse est déjà
partie après le message du prospect, rien ne repart. L'avancement est gardé
dans le KV (ensemble bot:rattrapage:fait) : une adresse n'est jamais retraitée
deux fois. Liste épuisée = plus aucun tour dédié, le cron reprend son rythme.

Aucune adresse dans le dépôt : il est public.

Pas de Vercel Cron dédié : sur l'offre Hobby, un cron Vercel ne peut tourner
qu'une fois par jour (un « chaque minute » fait échouer le déploiement).
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

FAIT = "bot:rattrapage:fait"
LISTE = "bot:rattrapage:liste"
LISTE_TTL = 7 * 24 * 3600
DEPUIS = os.environ.get("RATTRAPAGE_DEPUIS", "2026-11-09T00:00:00+00:00")
STATUTS = ("Interested", "Neutral", "MaybeLater", "NotInterested")  # pistes d'abord
TOUR = "bot:rattrapage:tour"
PAR_TOUR = int(os.environ.get("RATTRAPAGE_PAR_TOUR", "2"))
# Un tour dédié au plus toutes les 9 min : le passage principal garde au moins
# un tour sur deux (cron toutes les 5 min).
ECART_S = int(os.environ.get("RATTRAPAGE_ECART_S", "540"))


def _calculer() -> list[str]:
    """Les expéditeurs de réponses classées depuis DEPUIS qui portent la marque
    « orphelin ». Une douzaine d'appels ManyReach, une seule fois."""
    from datetime import datetime
    from src import kvstore
    from src.manyreach import ManyReachClient

    depuis = datetime.fromisoformat(DEPUIS)
    emails: list[str] = []
    with ManyReachClient() as mr:
        for st in STATUTS:
            for m in mr.list_replies(since=depuis, confirmed_statuses=(st,), max_per_status=300):
                e = (m.from_email or "").lower().strip()
                if e and "@" in e and e not in emails:
                    emails.append(e)
    out: list[str] = []
    for i in range(0, len(emails), 200):   # MGET par lots (limite de l'API REST)
        lot = emails[i:i + 200]
        marques = kvstore._cmd("MGET", *[f"bot:orphan_sent_email:{e}" for e in lot])
        if not isinstance(marques, list):
            # Lot illisible : surtout ne pas enregistrer une liste incomplète,
            # on recalculera au tour suivant.
            raise RuntimeError("lecture KV des marques orphelin en échec")
        out += [e for e, v in zip(lot, marques) if v]
    return out


def _liste() -> list[str] | None:
    """None = pas encore calculée."""
    from src import kvstore
    raw = kvstore._cmd("GET", LISTE)
    if not raw:
        return None
    try:
        return list(json.loads(raw))
    except (TypeError, ValueError):
        return None


def _restants() -> list[str]:
    from src import kvstore
    emails = _liste() or []
    if not emails:
        return []
    fait = set(kvstore._cmd("SMEMBERS", FAIT) or [])
    return [e for e in emails if e not in fait]


def rattrapage_du() -> bool:
    """True si la liste reste à calculer ou à traiter ET que le dernier tour
    dédié date de plus de ECART_S. Le SET NX sert aussi de verrou. Sur erreur :
    False (on ne pénalise jamais le passage principal)."""
    try:
        from src import kvstore
        if not kvstore.kv_available():
            return False
        if _liste() is not None and not _restants():
            return False
        return kvstore._cmd("SET", TOUR, "1", "EX", str(ECART_S), "NX") == "OK"
    except Exception:  # noqa: BLE001
        return False


def run_rattrapage(enveloppe_s: float = 24.0) -> dict:
    from src import kvstore
    import run_bot

    if _liste() is None:
        # Premier tour : on ne fait que calculer la liste (le traitement
        # commence au tour suivant, l'enveloppe de temps reste tenue).
        emails = _calculer()
        kvstore._cmd("SET", LISTE, json.dumps(emails), "EX", str(LISTE_TTL))
        return {"liste_calculee": len(emails)}

    debut = time.time()
    out: dict = {"traites": []}
    old_argv = sys.argv
    old_budget = os.environ.get("RUN_BUDGET_SECONDS")
    try:
        for email in _restants():
            reste = enveloppe_s - (time.time() - debut)
            if len(out["traites"]) >= PAR_TOUR or reste < 12:
                break
            # SADD renvoie 1 si l'adresse est nouvelle dans l'ensemble : c'est
            # aussi le verrou contre deux tours qui se chevauchent.
            if str(kvstore._cmd("SADD", FAIT, email)) != "1":
                continue
            os.environ["RUN_BUDGET_SECONDS"] = str(int(max(8, reste - 3)))
            sys.argv = ["run_bot", "--no-dry-run", "--rattrapage", email, "--ignore-window"]
            statut = "ok"
            try:
                run_bot.main()
            except SystemExit:
                pass
            except Exception as e:  # noqa: BLE001
                statut = f"erreur : {e}"
                # Échec : on retire l'adresse de l'ensemble, elle repassera.
                kvstore._cmd("SREM", FAIT, email)
            out["traites"].append({"email": email, "statut": statut})
    finally:
        sys.argv = old_argv
        # Le lambda reste chaud : ne pas laisser notre budget au passage suivant.
        if old_budget is None:
            os.environ.pop("RUN_BUDGET_SECONDS", None)
        else:
            os.environ["RUN_BUDGET_SECONDS"] = old_budget
    out["restants"] = len(_restants())
    return out


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
