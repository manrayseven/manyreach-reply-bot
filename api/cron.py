"""Vercel Cron entry point — exécute le bot une fois.

Déclenché périodiquement par Vercel Cron (ou un cron externe type cron-job.org)
qui fait un GET sur /api/cron. Réutilise tout le code existant (scripts/run_bot).

Sécurité : si CRON_SECRET est défini, exige l'en-tête Authorization: Bearer <secret>
(Vercel Cron l'envoie automatiquement).
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        secret = os.environ.get("CRON_SECRET")
        auth = self.headers.get("Authorization", "")
        if secret and auth != f"Bearer {secret}":
            self._json(401, {"ok": False, "error": "unauthorized"})
            return

        # FS read-only sur Vercel sauf /tmp
        os.environ.setdefault("LOG_DIR", "/tmp/mr-logs")
        # Budget de temps serré : on vise < 28s pour que cron-job.org (timeout 30s)
        # reçoive un "Succès" et que set_last_run tourne toujours. Listing borné à
        # 2 pages (LIST_MAX_PAGES) → phase liste rapide.
        os.environ.setdefault("RUN_BUDGET_SECONDS", "26")
        os.environ.setdefault("LIST_MAX_PAGES", "2")
        result: dict = {"ok": True}
        try:
            import run_bot  # scripts/run_bot.py

            # Quota d'itérations LOURDES (draft+send Sonnet). Chaque heavy ~12s →
            # avec budget 26s on en fait 1-2 par run. Cron 5 min = 12-24/h en
            # autonome. La file FIFO garantit que rien ne starve (le plus vieux
            # non-répondu passe toujours en premier). --since-days 1 = moins de
            # data à lister (plus rapide). Au-delà : bouton "Pour cet email".
            limit = os.environ.get("CRON_LIMIT", "6")
            sys.argv = [
                "run_bot",
                "--no-dry-run",
                "--limit", limit,
                "--since-days", "1",
            ]
            code = run_bot.main()
            result["exit_code"] = code
        except SystemExit as e:
            result["exit_code"] = e.code
        except Exception as e:  # noqa: BLE001
            import traceback
            result = {"ok": False, "error": str(e), "trace": traceback.format_exc()[-1500:]}
        self._json(200, result)

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
