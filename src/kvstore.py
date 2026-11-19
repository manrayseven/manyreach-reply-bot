"""Client minimal pour Vercel KV (Upstash Redis REST API).

Sert d'état cloud pour l'app Vercel (le serverless n'a pas de fichiers persistants) :
- bot_enabled : flag on/off (piloté par le bouton du dashboard)
- action log : liste des dernières actions (pour le suivi dans le dashboard)
- settings_overrides : réglages édités via le dashboard (overlay sur settings.yaml)
- last_run : timestamp du dernier passage

Variables d'env (fournies par Vercel quand tu connectes une base KV) :
  KV_REST_API_URL + KV_REST_API_TOKEN   (ou UPSTASH_REDIS_REST_URL/TOKEN)

Si aucune base KV n'est configurée, toutes les méthodes sont des no-op sûrs
(le bot fonctionne quand même, juste sans état cloud — utile en local).
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx


def _find_env(*exact_names: str, suffix: str | None = None) -> str | None:
    """Trouve une variable d'env par nom exact, sinon par suffixe (tolère
    n'importe quel préfixe ajouté par l'intégration Upstash/Vercel, ex.
    STORAGE_REST_API_URL, KV_REST_API_URL, UPSTASH_REDIS_REST_URL...)."""
    for name in exact_names:
        v = os.environ.get(name)
        if v:
            return v
    if suffix:
        for k, v in os.environ.items():
            if k.endswith(suffix) and v and "READ_ONLY" not in k:
                return v
    return None


_URL = _find_env("KV_REST_API_URL", "UPSTASH_REDIS_REST_URL", suffix="REST_API_URL")
_TOKEN = _find_env("KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_TOKEN", suffix="REST_API_TOKEN")

ACTION_LOG_KEY = "bot:action_log"
ENABLED_KEY = "bot:enabled"
SETTINGS_KEY = "bot:settings_overrides"
LAST_RUN_KEY = "bot:last_run"
MAX_LOG_ENTRIES = 500


def kv_available() -> bool:
    return bool(_URL and _TOKEN)


def _cmd(*args: Any) -> Any:
    if not kv_available():
        return None
    try:
        resp = httpx.post(
            _URL,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            json=[str(a) for a in args],
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("result")
    except Exception:
        return None


def is_enabled(default: bool = True) -> bool:
    val = _cmd("GET", ENABLED_KEY)
    if val is None:
        return default
    return str(val) not in ("0", "false", "off", "")


def set_enabled(enabled: bool) -> None:
    _cmd("SET", ENABLED_KEY, "1" if enabled else "0")


ALERTS_KEY = "bot:alerts"
MAX_ALERTS = 500
# Même liste que ALERT_INTENTS du dashboard (api/index.py) — dupliquée ici pour ne
# pas importer src.actions (et donc le SDK Anthropic) dans le client KV.
_ALERT_INTENTS = frozenset({
    "interested_warm", "interested_lukewarm", "ask_more_info",
    "meeting_confirmed", "objection_timing", "objection_reasoned",
})


def _is_alert_entry(entry: dict) -> bool:
    return (entry.get("intent") in _ALERT_INTENTS
            or "ALERTE" in str(entry.get("status", "")))


def log_action(entry: dict) -> None:
    """Ajoute une action au journal (capé). Les ALERTES sont aussi copiées dans
    une liste dédiée : le journal (envois, silencieux…) tourne vite et les
    faisait disparaître du dashboard (Rudy 16/09 : afficher TOUTES les alertes)."""
    if not kv_available():
        return
    payload = json.dumps(entry, ensure_ascii=False)
    _cmd("LPUSH", ACTION_LOG_KEY, payload)
    _cmd("LTRIM", ACTION_LOG_KEY, "0", str(MAX_LOG_ENTRIES - 1))
    if _is_alert_entry(entry):
        _cmd("LPUSH", ALERTS_KEY, payload)
        _cmd("LTRIM", ALERTS_KEY, "0", str(MAX_ALERTS - 1))


def recent_alerts(n: int = MAX_ALERTS, stats: dict | None = None) -> list[dict]:
    """Alertes conservées dans la liste dédiée (plus récentes d'abord).

    Lecture PAR TRANCHES : _cmd avale toute erreur (timeout, réponse trop
    grosse) en renvoyant None → une seule grosse LRANGE qui échoue faisait
    disparaître TOUTES les alertes anciennes, sans aucune trace (cas
    malittlecreche, 21/09). Par tranches, un échec ne coûte que sa tranche.
    """
    out: list[dict] = []
    _CH = 50   # tranches courtes : les anciennes alertes contiennent encore l'historique
    _ok = _ko = _bad = 0
    for start in range(0, n, _CH):
        raw = _cmd("LRANGE", ALERTS_KEY, str(start), str(min(start + _CH, n) - 1))
        if not isinstance(raw, list):
            # Tranche en échec (timeout, réponse trop grosse) OU fin de liste —
            # _cmd avale tout et renvoie None, impossible de distinguer ici. On
            # le COMPTE (Rudy 19/10) : une alerte écrite mais jamais affichée
            # reste autrement invisible, et on cherche la cause à l'aveugle.
            _ko += 1
            break
        _ok += 1
        for item in raw:
            try:
                out.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                _bad += 1
        if len(raw) < _CH:
            break  # fin de la liste
    if stats is not None:
        stats.update({"chunks_ok": _ok, "chunks_ko": _ko,
                      "illisibles": _bad, "lues": len(out),
                      "total": alerts_count()})
    return out


def alerts_count() -> int:
    """Nombre d'alertes RÉELLEMENT stockées (LLEN) — à comparer à ce que la
    lecture par tranches rend. L'écart dit si on perd des alertes à la lecture."""
    res = _cmd("LLEN", ALERTS_KEY)
    try:
        return int(res)
    except (TypeError, ValueError):
        return -1


# CONTEXTE D'ALERTE (historique complet des échanges) : stocké À PART, une clé
# par alerte. Dans l'entrée de journal, il pesait jusqu'à ~12 Ko × 500 entrées →
# lectures de plusieurs Mo qui échouaient en silence.
ALERT_CTX_PREFIX = "bot:alertctx:"
ALERT_CTX_TTL = 60 * 24 * 3600  # 60 jours


def set_alert_context(alert_id: str, ctx: dict) -> None:
    if not kv_available() or not alert_id or not ctx:
        return
    _cmd("SET", ALERT_CTX_PREFIX + alert_id,
         json.dumps(ctx, ensure_ascii=False), "EX", str(ALERT_CTX_TTL))


def get_alert_contexts(alert_ids: list[str]) -> dict[str, dict]:
    """Contextes des alertes demandées, en MGET par lots de 50."""
    out: dict[str, dict] = {}
    if not kv_available() or not alert_ids:
        return out
    # Lots COURTS (Rudy 10/11) : les historiques ne sont plus tronqués, donc un
    # contexte pèse jusqu'à ~40 Ko. Par 50, un MGET ramenait 2 Mo et échouait en
    # silence — c'est exactement ce qui faisait disparaître des alertes en
    # septembre. Par 12, une réponse reste sous le demi-méga.
    _CH = 12
    for _i in range(0, len(alert_ids), _CH):
        batch = alert_ids[_i:_i + _CH]
        res = _cmd("MGET", *[ALERT_CTX_PREFIX + a for a in batch])
        if not isinstance(res, list):
            continue
        for aid, val in zip(batch, res):
            if not val:
                continue
            try:
                out[aid] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    return out


def recent_actions(n: int = 50) -> list[dict]:
    """Journal des actions, lu PAR TRANCHES (même raison que recent_alerts :
    une lecture trop grosse échoue en silence et vide le dashboard)."""
    out: list[dict] = []
    _CH = 100
    for start in range(0, n, _CH):
        raw = _cmd("LRANGE", ACTION_LOG_KEY, str(start), str(min(start + _CH, n) - 1))
        if not isinstance(raw, list):
            break
        for item in raw:
            try:
                out.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                pass
        if len(raw) < _CH:
            break
    return out


def cache_get(key: str):
    """Lecture cache simple (renvoie None si absent/KV indispo)."""
    if not kv_available():
        return None
    return _cmd("GET", key)


def cache_set(key: str, value: str, ttl_seconds: int = 1800) -> None:
    """Écrit une valeur avec TTL (SET key value EX ttl). No-op si KV indispo."""
    if not kv_available():
        return
    _cmd("SET", key, value, "EX", str(int(ttl_seconds)))


def get_settings_overrides() -> dict:
    raw = _cmd("GET", SETTINGS_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def set_settings_overrides(overrides: dict) -> None:
    _cmd("SET", SETTINGS_KEY, json.dumps(overrides, ensure_ascii=False))


CLIENTS_KEY = "bot:clients"


def get_clients() -> list[dict]:
    """Liste des comptes clients (multi-clients). [] si non configuré/KV indispo."""
    raw = _cmd("GET", CLIENTS_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def set_clients(clients: list[dict]) -> None:
    _cmd("SET", CLIENTS_KEY, json.dumps(clients, ensure_ascii=False))


HANDOFFS_KEY = "bot:handoffs"  # SET des transferts déjà comptés (client|email)


def all_handoffs() -> set[str]:
    """Toutes les clés « client|prospect » déjà comptées comme transmises."""
    if not kv_available():
        return set()
    res = _cmd("SMEMBERS", HANDOFFS_KEY)
    return {str(x) for x in res} if isinstance(res, list) else set()


def record_handoff(key: str) -> bool:
    """Enregistre un transfert « Mettre en relation ». Dédupliqué : renvoie True
    seulement la 1re fois pour ce (client|prospect) → 1 comptage par conversation."""
    if not kv_available() or not key:
        return False
    res = _cmd("SADD", HANDOFFS_KEY, key.lower())
    try:
        return int(res) == 1
    except (TypeError, ValueError):
        return bool(res)


# FICHES des mises en relation (Rudy 01/10) : au 1er « Copier l'email », on
# garde TOUT le transfert (société, contact, campagne, message, conversation)
# pour pouvoir générer plus tard le rapport mois par mois d'un compte.
HANDOFF_LOG_KEY = "bot:handoff_log"
MAX_HANDOFF_RECORDS = 1000


def add_handoff_record(rec: dict) -> None:
    if not kv_available() or not rec:
        return
    _cmd("LPUSH", HANDOFF_LOG_KEY, json.dumps(rec, ensure_ascii=False))
    _cmd("LTRIM", HANDOFF_LOG_KEY, "0", str(MAX_HANDOFF_RECORDS - 1))


def handoff_records(n: int = MAX_HANDOFF_RECORDS) -> list[dict]:
    """Fiches des mises en relation, plus récentes d'abord (lecture par tranches)."""
    out: list[dict] = []
    _CH = 50
    for start in range(0, n, _CH):
        raw = _cmd("LRANGE", HANDOFF_LOG_KEY, str(start), str(min(start + _CH, n) - 1))
        if not isinstance(raw, list):
            break
        for item in raw:
            try:
                out.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                pass
        if len(raw) < _CH:
            break
    return out


TRIAGE_KEY = "bot:triage"  # HASH alert_id -> client_id (assignations manuelles)


def set_triage(alert_id: str, client_id: str) -> None:
    """Mémorise l'assignation manuelle d'une alerte 'à trier' à un client."""
    if not kv_available() or not alert_id or not client_id:
        return
    _cmd("HSET", TRIAGE_KEY, alert_id, client_id)


def get_triage() -> dict:
    """Renvoie {alert_id: client_id} des assignations manuelles. Tolère les deux
    formats de retour Upstash (dict ou liste plate [k, v, k, v, ...])."""
    res = _cmd("HGETALL", TRIAGE_KEY)
    if isinstance(res, dict):
        return {str(k): str(v) for k, v in res.items()}
    if isinstance(res, list):
        out: dict = {}
        for i in range(0, len(res) - 1, 2):
            out[str(res[i])] = str(res[i + 1])
        return out
    return {}


def set_last_run(iso: str) -> None:
    _cmd("SET", LAST_RUN_KEY, iso)


def get_last_run() -> str | None:
    return _cmd("GET", LAST_RUN_KEY)


SPACES_LAST_KEY = "bot:spaces:last_run"


def set_spaces_last_run(payload: str) -> None:
    """Memorise le resultat du dernier passage sur les espaces (JSON compact).

    Sans ca, un espace vide et un espace en panne se ressemblent : les deux ne
    produisent ni alerte ni envoi. Le dashboard affiche cette trace pour qu'un
    echec silencieux se voie.
    """
    _cmd("SET", SPACES_LAST_KEY, payload)


def get_spaces_last_run() -> str | None:
    return _cmd("GET", SPACES_LAST_KEY)


HELD_SEEN_TTL = 86400  # 24h — couvre une nuit + un week-end max raisonnable


def mark_held_seen(message_id: str) -> bool:
    """Marque ce message_id comme déjà 'gardé pour la fenêtre d'envoi'.

    Renvoie True si on l'avait DÉJÀ vu (donc on NE doit PAS re-logger dans le
    dashboard et NE doit PAS re-classifier/re-drafter). Atomique via SET NX.
    """
    if not kv_available():
        return False
    key = f"bot:held_seen:{message_id}"
    # SET key 1 EX 86400 NX → "OK" si créé, null si la clé existait déjà.
    res = _cmd("SET", key, "1", "EX", str(HELD_SEEN_TTL), "NX")
    return res is None  # null = NX a échoué = déjà vu


def peek_held_seen(message_id: str) -> bool:
    """Comme mark_held_seen mais SANS poser le flag : juste lecture.

    Utilisé en TOUT DÉBUT de boucle pour décider d'écarter un reply (économie
    de tokens) avant même de payer le find_prospect + classifier. Renvoie True
    si le flag existe déjà (= reply déjà gardé précédemment).
    """
    if not kv_available():
        return False
    return _cmd("GET", f"bot:held_seen:{message_id}") is not None


def clear_held_seen(message_id: str) -> None:
    """À appeler quand le reply est finalement envoyé / traité (pour libérer)."""
    if not kv_available():
        return
    _cmd("DEL", f"bot:held_seen:{message_id}")


ORPHAN_TTL = 30 * 24 * 3600  # 30 jours — bot ne renvoie qu'une fois aux orphans


def mark_orphan_sent(sender_email: str) -> bool:
    """Marque qu'on a tenté de répondre à un sender ORPHAN (pas de prospect lié).

    Pour ces replies sans prospect dans ManyReach, la thread idempotence
    classique ne marche pas. Sans ce flag KV, le bot re-renvoie à chaque run
    (vu pour sekou : 10+ envois sur la journée parce que ManyReach peut avoir
    PLUSIEURS replies orphelins du même sender, chacun avec un message_id
    différent → message_id-based lock laisse passer). On verrouille par EMAIL.

    Renvoie True si on l'avait DÉJÀ vu (= skip), False si c'est nouveau.
    """
    if not kv_available():
        return False
    key = f"bot:orphan_sent_email:{sender_email.lower().strip()}"
    res = _cmd("SET", key, "1", "EX", str(ORPHAN_TTL), "NX")
    return res is None  # null = NX échoué = déjà vu


def force_mark_orphan(sender_email: str) -> None:
    """Force le marquage d'un sender orphan comme déjà traité — utilisé en
    urgence pour arrêter un loop en cours (sekou-like)."""
    if not kv_available():
        return
    key = f"bot:orphan_sent_email:{sender_email.lower().strip()}"
    _cmd("SET", key, "1", "EX", str(ORPHAN_TTL))


DISMISSED_ALERTS_KEY = "bot:dismissed_alerts"


def dismiss_alert(alert_id: str) -> None:
    """Marque une alerte comme 'traitée' (Rudy a déjà géré). Affecte juste
    l'affichage du dashboard, l'entrée reste dans le log historique.
    alert_id = '{at_iso}|{email}' pour identifier uniquement."""
    if not kv_available() or not alert_id:
        return
    _cmd("SADD", DISMISSED_ALERTS_KEY, alert_id)


def get_dismissed_alerts() -> set[str]:
    """Renvoie l'ensemble des alert_id déjà 'traitées' par Rudy."""
    if not kv_available():
        return set()
    res = _cmd("SMEMBERS", DISMISSED_ALERTS_KEY)
    if isinstance(res, list):
        return {str(x) for x in res}
    return set()


def restore_alert(alert_id: str) -> None:
    """Annule un dismiss (au cas où Rudy clique par erreur)."""
    if not kv_available() or not alert_id:
        return
    _cmd("SREM", DISMISSED_ALERTS_KEY, alert_id)


PROCESSED_TTL = 14 * 24 * 3600  # 14 jours — remplace /tmp processed_ids (éphémère sur Vercel)


def is_kv_processed(message_id: str) -> bool:
    """True si ce reply a déjà atteint une décision TERMINALE (envoyé, silencieux,
    ou déjà-traité-manuellement). Permet de skip CHEAP en tout début de boucle,
    AVANT les appels coûteux find_prospect + get_thread. Persistant (KV) →
    survit aux cold starts Vercel, contrairement à /tmp processed_ids."""
    if not kv_available():
        return False
    return _cmd("GET", f"bot:processed:{message_id}") is not None


def filter_processed(message_ids: list[str]) -> set[str]:
    """Renvoie le sous-ensemble des message_ids DÉJÀ traités, en UN SEUL appel
    MGET (au lieu de N appels GET). Critique pour la perf : éviter ~200 round-trips
    KV par run de cron (cause de runs à 44s)."""
    if not kv_available() or not message_ids:
        return set()
    # DÉCOUPAGE EN LOTS : on liste desormais jusqu'a ~800 replies par run
    # (anti-famine, cf. cron.py). Un MGET de 800 cles d'un coup depasse les
    # limites de payload de l'API REST → on decoupe par 200.
    done: set[str] = set()
    _CH = 200
    for _i in range(0, len(message_ids), _CH):
        _batch = message_ids[_i:_i + _CH]
        keys = [f"bot:processed:{m}" for m in _batch]
        try:
            res = _cmd("MGET", *keys)
        except Exception:  # noqa: BLE001
            continue  # lot en echec → on re-traitera, jamais de perte de reply
        if isinstance(res, list):
            for mid, val in zip(_batch, res):
                if val is not None:
                    done.add(mid)
    return done


def mark_kv_processed(message_id: str) -> None:
    """Marque ce reply comme terminé (TTL 14j). À NE PAS appeler pour les
    différés (hors fenêtre) ni les holds (cap) → ceux-là doivent réessayer."""
    if not kv_available():
        return
    _cmd("SET", f"bot:processed:{message_id}", "1", "EX", str(PROCESSED_TTL))


REPLIED_TTL = 30 * 24 * 3600  # 30 jours


def already_replied(message_id: str) -> bool:
    """True si on a DÉJÀ envoyé une réponse à ce reply (flag permanent KV).

    Garde-fou anti-double-envoi INDÉPENDANT de l'indexation ManyReach : même si
    le Sent met du temps à apparaître dans le thread (donc l'idempotence thread
    ne le voit pas encore), ce flag bloque tout 2e envoi. Posé APRÈS un envoi
    réussi via mark_replied(). TTL 30 jours.
    """
    if not kv_available():
        return False
    return _cmd("GET", f"bot:replied:{message_id}") is not None


def mark_replied(message_id: str) -> None:
    """À appeler APRÈS un envoi réussi → bloque tout futur envoi sur ce reply."""
    if not kv_available():
        return
    _cmd("SET", f"bot:replied:{message_id}", "1", "EX", str(REPLIED_TTL))


def acquire_send_lock(message_id: str, ttl_seconds: int = 600) -> bool:
    """Acquiert un verrou exclusif pour envoyer une réponse à ce reply.

    Empêche un duplicate send quand deux runs parallèles (cron + clic manuel,
    ou deux crons qui se chevauchent à cause de timeouts précédents) tentent
    de répondre au même message avant que le Sent ne soit visible dans le
    thread ManyReach. TTL = 90s : couvre largement le temps d'envoi + de
    propagation dans la thread.

    Renvoie True si le lock a été acquis (= on peut envoyer), False si déjà
    pris par un autre run (= on doit skip cet envoi).
    """
    if not kv_available():
        return True  # pas de KV → pas de protection (on tente quand même)
    key = f"bot:send_lock:{message_id}"
    res = _cmd("SET", key, "1", "EX", str(ttl_seconds), "NX")
    return res is not None  # "OK" = créé = on a le lock


# PROSPECTS DÉJÀ REMONTÉS EN ALERTE (Rudy 13/10, cas bieresicsas). Une alerte et
# une réponse auto sont EXCLUSIVES : si le bot a alerté sur un prospect, il ne
# doit plus jamais lui envoyer de modèle. `is_lead_in_progress` le faisait déjà,
# mais en relisant les tags du prospect — donc aveugle si la fiche lue est en
# retard d'une passe (ce qui s'est produit : même réponse traitée deux fois, une
# fois en alerte, une fois en refus poli). Cette marque-ci est écrite par le bot
# lui-même au moment de l'alerte : elle ne peut pas être en retard.
ALERTED_PREFIX = "bot:alerted:"
ALERTED_TTL = 365 * 24 * 3600


def mark_alerted(*emails: str) -> None:
    """Mémorise qu'une alerte a été levée pour ce(s) prospect(s)."""
    if not kv_available():
        return
    for em in emails:
        e = (em or "").lower().strip()
        if e:
            _cmd("SET", ALERTED_PREFIX + e, "1", "EX", str(ALERTED_TTL))


def was_alerted(*emails: str) -> bool:
    """True si l'un de ces emails a déjà fait l'objet d'une alerte."""
    if not kv_available():
        return False
    for em in emails:
        e = (em or "").lower().strip()
        if e and _cmd("GET", ALERTED_PREFIX + e) is not None:
            return True
    return False


# APPRENTISSAGE PROGRESSIF (Rudy 14/11). Chaque fois qu'il corrige le bot d'un
# clic dans le dashboard — « ce n'était pas une alerte », « ça aurait dû
# remonter » — on garde l'extrait et la bonne réponse. Le classifieur les relit
# au démarrage de chaque passage et les colle à son prompt. Personne ne touche
# au code : la correction d'aujourd'hui change le comportement de demain.
LECONS_KEY = "bot:lecons"
MAX_LECONS = 60


def add_lecon(extrait: str, intent: str, email: str = "") -> None:
    if not kv_available():
        return
    txt = " ".join(str(extrait or "").split())[:400]
    if not txt or not intent:
        return
    from datetime import datetime as _d, timezone as _t
    _cmd("LPUSH", LECONS_KEY, json.dumps({
        "at": _d.now(_t.utc).isoformat(), "extrait": txt,
        "intent": intent, "from": (email or "").lower().strip(),
    }, ensure_ascii=False))
    _cmd("LTRIM", LECONS_KEY, "0", str(MAX_LECONS - 1))


def lecons(n: int = MAX_LECONS) -> list[dict]:
    """Corrections les plus récentes d'abord."""
    out: list[dict] = []
    if not kv_available():
        return out
    raw = _cmd("LRANGE", LECONS_KEY, "0", str(max(0, n - 1)))
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            out.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# AGENDA DE RELANCE (Rudy 16/11) : « quand le prospect dit lui-même quand le
# relancer, intègre cela dans ton plan de relance ».
#
# Eric Edouard, 16/11 : « Les éléments que j'attendais ne sont pas encore
# disponibles. Je pense, 1er trimestre 2027. » Le classifieur lisait déjà cette
# date (`recontact_datetime`) — personne ne s'en servait. Et le moteur de
# relance ne pouvait pas le rattraper : il refuse de relancer un fil dont le
# DERNIER message est une réponse du prospect. Autrement dit, celui qui dit
# quand le rappeler était précisément celui qu'on ne rappelait jamais.
#
# D'où cet agenda : une date, la phrase du prospect, et de quoi retrouver le
# fil. Un seul rendez-vous par adresse — le dernier dit remplace le précédent.
RELANCES_KEY = "bot:relances"  # HASH email -> {le, dit, pose_le, campaign_id…}


def plan_relance(email: str, le: str, dit: str = "", extra: dict | None = None) -> None:
    """Note (ou déplace) le rendez-vous de relance d'un prospect.

    `le` est une date ISO (YYYY-MM-DD ou datetime complet) ; `dit` la phrase
    du prospect, qu'on lui citera le jour venu.
    """
    if not kv_available():
        return
    em = (email or "").lower().strip()
    jour = str(le or "")[:10]
    if not em or len(jour) != 10:
        return
    from datetime import datetime as _d, timezone as _t
    rec = dict(extra or {})
    rec.update({
        "email": em,
        "le": jour,
        "dit": " ".join(str(dit or "").split())[:300],
        "pose_le": _d.now(_t.utc).isoformat(),
    })
    _cmd("HSET", RELANCES_KEY, em, json.dumps(rec, ensure_ascii=False))


def relances() -> list[dict]:
    """Tous les rendez-vous de relance, du plus proche au plus lointain."""
    res = _cmd("HGETALL", RELANCES_KEY)
    brut: list[str] = []
    if isinstance(res, dict):
        brut = [str(v) for v in res.values()]
    elif isinstance(res, list):
        brut = [str(res[i + 1]) for i in range(0, len(res) - 1, 2)]
    out: list[dict] = []
    for item in brut:
        try:
            out.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            pass
    return sorted(out, key=lambda r: str(r.get("le", "9999")))


def relances_dues(aujourdhui: str) -> list[dict]:
    """Les rendez-vous arrivés à échéance (date <= aujourd'hui, AAAA-MM-JJ)."""
    jour = str(aujourdhui or "")[:10]
    return [r for r in relances() if str(r.get("le", "9999")) <= jour]


def drop_relance(email: str) -> None:
    """Retire un rendez-vous : relance envoyée, ou Rudy l'a jugé caduc."""
    if not kv_available():
        return
    em = (email or "").lower().strip()
    if em:
        _cmd("HDEL", RELANCES_KEY, em)


# ─────────────────────────────────────────────────────────────────────────────
# MOTEUR DE RELANCE : MÉMOIRE DE PASSAGE (Rudy 19/11)
#
# Relevé du 19/11 sur 324 fils Interested/MaybeLater : 10 relances parties,
# 149 dues. Le cron tournait pourtant tous les jours. La cause n'était ni le
# cron ni la fenêtre de lecture, mais le BUDGET : 45 s, 55 requêtes/minute, et
# deux requêtes par prospect (sa fiche, son fil) — soit une vingtaine de
# prospects par passage. Comme la liste est triée du plus récent au plus
# ancien, c'étaient TOUJOURS les vingt mêmes, justement les plus frais, donc
# ceux qu'il est trop tôt pour relancer. Le 21e n'était jamais atteint.
#
# D'où cette mémoire : on note qui a été examiné et quand. Le passage suivant
# saute ces adresses SANS dépenser d'appel API, et avance donc dans la liste.
# En une semaine le moteur balaie tout, au lieu de piétiner.
BUMP_SEEN_KEY = "bot:bump_seen"       # HASH email -> date d'examen (ISO)
BUMP_DAY_KEY = "bot:bump_day"         # "AAAA-MM-JJ|n" : envois du jour
BUMP_RUN_KEY = "bot:bump_last_run"    # JSON du dernier passage
PID_PREFIX = "bot:pid:"               # cache email -> prospectId
MAX_BUMP_SEEN = 5000


def bump_seen() -> dict:
    """{email: date d'examen} — lu en UN appel au début du passage."""
    res = _cmd("HGETALL", BUMP_SEEN_KEY)
    if isinstance(res, dict):
        return {str(k): str(v) for k, v in res.items()}
    if isinstance(res, list):
        return {str(res[i]): str(res[i + 1]) for i in range(0, len(res) - 1, 2)}
    return {}


def mark_bump_seen_many(vus: dict) -> None:
    """Un seul HSET pour tout le passage : le budget est en secondes."""
    if not kv_available() or not vus:
        return
    plat: list = []
    for em, iso in vus.items():
        plat += [str(em).lower().strip(), str(iso)]
    _cmd("HSET", BUMP_SEEN_KEY, *plat)
    # La mémoire ne sert qu'à faire avancer le balayage : passé un certain
    # volume elle ne protège plus rien, elle coûte. On repart à zéro.
    try:
        if int(_cmd("HLEN", BUMP_SEEN_KEY) or 0) > MAX_BUMP_SEEN:
            _cmd("DEL", BUMP_SEEN_KEY)
    except (TypeError, ValueError):
        pass


def bump_sent_today(jour: str) -> int:
    """Combien de relances déjà parties aujourd'hui (tous passages confondus).

    Le plafond est JOURNALIER, pas par passage : c'est ce qui permet d'appeler
    /api/bumps toutes les heures sans risquer d'inonder les prospects.
    """
    raw = str(_cmd("GET", BUMP_DAY_KEY) or "")
    if "|" not in raw:
        return 0
    j, n = raw.split("|", 1)
    if j != jour:
        return 0
    try:
        return int(n)
    except ValueError:
        return 0


def add_bump_sent(jour: str, n: int = 1) -> int:
    if not kv_available():
        return 0
    total = bump_sent_today(jour) + n
    _cmd("SET", BUMP_DAY_KEY, f"{jour}|{total}", "EX", "172800")
    return total


def set_bump_run(payload: dict) -> None:
    """Trace du passage. Sans elle, le tableau de bord ne peut pas dire si les
    relances tournent — c'est précisément ce qui a laissé le moteur affamé
    pendant des semaines sans que personne ne le voie."""
    if not kv_available():
        return
    _cmd("SET", BUMP_RUN_KEY, json.dumps(payload, ensure_ascii=False),
         "EX", str(15 * 86400))


def get_bump_run() -> dict:
    raw = _cmd("GET", BUMP_RUN_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def cache_prospect_id(email: str, pid: int) -> None:
    """Une recherche de fiche coûte un appel API. Mise en cache, elle ne coûte
    plus rien au passage suivant : deux fois plus de prospects examinés."""
    if not kv_available() or not email or not pid:
        return
    _cmd("SET", f"{PID_PREFIX}{email.lower().strip()}", str(int(pid)),
         "EX", str(30 * 86400))


def cached_prospect_id(email: str) -> int | None:
    raw = _cmd("GET", f"{PID_PREFIX}{(email or '').lower().strip()}")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None
