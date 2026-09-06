"""Passage du bot sur les ESPACES secondaires (workspaces ManyReach).

Un workspace ManyReach a ses propres donnees ET sa propre cle API : la cle du
compte principal ne voit pas ses campagnes ni ses reponses. Il faut donc un
passage complet du bot par espace.

Ce module contient la boucle partagee par les deux declencheurs :
  - /api/spaces  (cron externe, protege par CRON_SECRET)
  - le bouton "Lancer les espaces" du dashboard
afin que les deux se comportent EXACTEMENT pareil.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Marge sous le timeout du cron externe (30 s) : on ne LANCE un espace de plus
# que s'il reste de quoi faire une iteration lourde complete (~12 s).
DEFAULT_TOTAL_BUDGET_S = 24.0
MIN_SLICE_S = 12.0


def list_spaces() -> list[str]:
    """Ids des comptes pour lesquels une cle d'espace est definie."""
    try:
        from src.manyreach import workspace_api_keys
        return sorted(workspace_api_keys())
    except Exception:  # noqa: BLE001
        return []


def run_spaces(
    total_budget_s: float = DEFAULT_TOTAL_BUDGET_S,
    limit: str | None = None,
    since_days: str | None = None,
) -> dict:
    """Traite chaque espace, un passage par espace. Ne leve jamais : renvoie un
    rapport {espace: code|message}. Un espace en echec n'empeche pas les suivants.
    """
    started = time.time()
    os.environ.setdefault("LOG_DIR", "/tmp/mr-logs")
    os.environ.setdefault("LIST_MAX_PAGES", "8")

    spaces = list_spaces()
    result: dict = {"ok": True, "spaces": {}}
    if not spaces:
        result["note"] = "aucune variable MANYREACH_API_KEY_<ID> definie"
        return result

    limit = limit or os.environ.get("SPACES_LIMIT", "10")
    since_days = since_days or os.environ.get("SPACES_SINCE_DAYS", "10")

    # RUN_BUDGET_SECONDS est relu par run_bot a chaque appel : on le pose par
    # espace PUIS on restaure, sinon un lambda "warm" garderait notre valeur et
    # raccourcirait le passage principal du cron suivant.
    old_budget = os.environ.get("RUN_BUDGET_SECONDS")
    old_argv = sys.argv
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        import run_bot  # scripts/run_bot.py

        for space in spaces:
            left = total_budget_s - (time.time() - started)
            if left < MIN_SLICE_S:
                result["spaces"][space] = "reporte (budget temps)"
                continue
            os.environ["RUN_BUDGET_SECONDS"] = str(int(min(20.0, left)))
            sys.argv = [
                "run_bot",
                "--no-dry-run",
                "--limit", str(limit),
                "--since-days", str(since_days),
                "--space", space,
            ]
            try:
                result["spaces"][space] = run_bot.main()
            except SystemExit as e:
                result["spaces"][space] = e.code
            except Exception as e:  # noqa: BLE001
                result["ok"] = False
                result["spaces"][space] = f"erreur: {str(e)[:300]}"
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": str(e)[:300], "spaces": result.get("spaces", {})}
    finally:
        sys.argv = old_argv
        if old_budget is None:
            os.environ.pop("RUN_BUDGET_SECONDS", None)
        else:
            os.environ["RUN_BUDGET_SECONDS"] = old_budget

    result["elapsed_s"] = round(time.time() - started, 1)
    # Trace persistante : un espace vide ne produit ni alerte ni envoi, donc
    # sans ca on ne distingue pas "a tourne, rien a faire" de "n'a pas tourne".
    try:
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        from src import kvstore as _kv
        if _kv.kv_available():
            _kv.set_spaces_last_run(_json.dumps({
                "at": _dt.now(_tz.utc).isoformat(),
                "spaces": result.get("spaces", {}),
                "note": result.get("note"),
                "ok": result.get("ok", True),
                "elapsed_s": result.get("elapsed_s"),
            }, ensure_ascii=False)[:900])
    except Exception:  # noqa: BLE001
        pass
    return result
