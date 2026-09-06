"""Vercel entry point — traite les ESPACES secondaires (workspaces ManyReach).

Pourquoi un endpoint separe de /api/cron : un workspace ManyReach a ses propres
donnees ET sa propre cle API, donc il faut un passage complet du bot par espace.
Les enchainer dans /api/cron ferait depasser le timeout de 30 s du cron externe
et ferait passer le passage principal pour un echec. Ici chaque espace a son
propre budget, sans jamais ralentir le compte principal.

Un espace est traite si la variable MANYREACH_API_KEY_<ID_DU_COMPTE> existe
(ex. MANYREACH_API_KEY_CMACLIM -> compte "cmaclim" du dashboard).

Securite : identique a /api/cron — si CRON_SECRET est defini, exige
l'en-tete Authorization: Bearer <secret>.
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

# Marge sous le timeout du cron externe (30 s) : on ne LANCE un espace de plus
# que s'il reste de quoi faire une iteration lourde complete (~12 s).
TOTAL_BUDGET_S = 24.0
MIN_SLICE_S = 12.0


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        secret = os.environ.get("CRON_SECRET")
        auth = self.headers.get("Authorization", "")
        if secret and auth != f"Bearer {secret}":
            self._json(401, {"ok": False, "error": "unauthorized"})
            return

        started = time.time()
        os.environ.setdefault("LOG_DIR", "/tmp/mr-logs")
        os.environ.setdefault("LIST_MAX_PAGES", "8")

        try:
            from src.manyreach import workspace_api_keys
            spaces = sorted(workspace_api_keys())
        except Exception as e:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"cles espaces illisibles: {e}"})
            return

        result: dict = {"ok": True, "spaces": {}}
        if not spaces:
            result["note"] = "aucune variable MANYREACH_API_KEY_<ID> definie"
            self._json(200, result)
            return

        # RUN_BUDGET_SECONDS est lu par run_bot a chaque appel : on le pose par
        # espace PUIS on restaure, sinon un lambda "warm" garderait notre valeur
        # et raccourcirait le passage principal du cron suivant.
        old_budget = os.environ.get("RUN_BUDGET_SECONDS")
        old_argv = sys.argv
        limit = os.environ.get("SPACES_LIMIT", "10")
        since_days = os.environ.get("SPACES_SINCE_DAYS", "10")
        try:
            import run_bot  # scripts/run_bot.py

            for space in spaces:
                left = TOTAL_BUDGET_S - (time.time() - started)
                if left < MIN_SLICE_S:
                    result["spaces"][space] = "reporte (budget temps)"
                    continue
                os.environ["RUN_BUDGET_SECONDS"] = str(int(min(20.0, left)))
                sys.argv = [
                    "run_bot",
                    "--no-dry-run",
                    "--limit", limit,
                    "--since-days", since_days,
                    "--space", space,
                ]
                try:
                    result["spaces"][space] = run_bot.main()
                except SystemExit as e:
                    result["spaces"][space] = e.code
                except Exception as e:  # noqa: BLE001
                    # Un espace en echec ne doit jamais empecher les suivants.
                    result["ok"] = False
                    result["spaces"][space] = f"erreur: {str(e)[:300]}"
        except Exception as e:  # noqa: BLE001
            import traceback
            result = {"ok": False, "error": str(e), "trace": traceback.format_exc()[-1500:]}
        finally:
            sys.argv = old_argv
            if old_budget is None:
                os.environ.pop("RUN_BUDGET_SECONDS", None)
            else:
                os.environ["RUN_BUDGET_SECONDS"] = old_budget

        result["elapsed_s"] = round(time.time() - started, 1)
        self._json(200, result)

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
