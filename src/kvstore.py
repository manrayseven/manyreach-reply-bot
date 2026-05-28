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
MAX_LOG_ENTRIES = 200


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


def log_action(entry: dict) -> None:
    """Ajoute une action au journal (capé)."""
    if not kv_available():
        return
    _cmd("LPUSH", ACTION_LOG_KEY, json.dumps(entry, ensure_ascii=False))
    _cmd("LTRIM", ACTION_LOG_KEY, "0", str(MAX_LOG_ENTRIES - 1))


def recent_actions(n: int = 50) -> list[dict]:
    raw = _cmd("LRANGE", ACTION_LOG_KEY, "0", str(n - 1))
    out: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                out.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                pass
    return out


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


def set_last_run(iso: str) -> None:
    _cmd("SET", LAST_RUN_KEY, iso)


def get_last_run() -> str | None:
    return _cmd("GET", LAST_RUN_KEY)


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


PROCESSED_TTL = 14 * 24 * 3600  # 14 jours — remplace /tmp processed_ids (éphémère sur Vercel)


def is_kv_processed(message_id: str) -> bool:
    """True si ce reply a déjà atteint une décision TERMINALE (envoyé, silencieux,
    ou déjà-traité-manuellement). Permet de skip CHEAP en tout début de boucle,
    AVANT les appels coûteux find_prospect + get_thread. Persistant (KV) →
    survit aux cold starts Vercel, contrairement à /tmp processed_ids."""
    if not kv_available():
        return False
    return _cmd("GET", f"bot:processed:{message_id}") is not None


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
