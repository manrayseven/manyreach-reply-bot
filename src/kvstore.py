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

_URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")

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
