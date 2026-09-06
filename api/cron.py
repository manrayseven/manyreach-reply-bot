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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


# Les ESPACES (workspaces ManyReach) ont leur propre cle API, donc leur propre
# passage du bot. Plutot que d'exiger un second cron, on l'intercale ici, au plus
# une fois toutes les SPACES_EVERY_MIN minutes. Leur temps est RESERVE A L'AVANCE
# sur le budget du passage principal : ajouter un passage APRES coup ferait
# depasser le timeout de 30 s du cron externe, et le passage principal serait
# alors compte comme un echec.
SPACES_EVERY_MIN = float(os.environ.get("SPACES_EVERY_MIN", "30"))
SPACES_RESERVE_S = 12.0   # temps reserve aux espaces sur les tours concernes
CRON_ENVELOPE_S = 26.0    # enveloppe totale visee (cron externe : timeout 30 s)


def _spaces_due() -> bool:
    """True s'il existe au moins un espace configure ET que le dernier passage
    date de plus de SPACES_EVERY_MIN. Sur erreur : False (on ne penalise jamais
    le passage principal a cause des espaces)."""
    try:
        from src.spaces import list_spaces
        if not list_spaces():
            return False
        from datetime import datetime, timezone
        from src import kvstore
        if not kvstore.kv_available():
            return True
        raw = kvstore.get_spaces_last_run()
        if not raw:
            return True
        at = json.loads(raw).get("at")
        if not at:
            return True
        age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds() / 60.0
        return age_min >= SPACES_EVERY_MIN
    except Exception:  # noqa: BLE001
        return False


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
        # ANTI-FAMINE (incident 26/08) : avec 2 pages on ne voyait que les 200
        # replies les plus RECENTS. Au volume actuel (~25 replies/h), toute reponse
        # non traitee dans les ~8 h sortait definitivement de la fenetre et n'etait
        # JAMAIS traitee (cas aline.kinesio39 : 481 replies arrives apres le sien,
        # repondu hors fenetre a 22h58 puis perdu). La phase de listing est tres
        # rapide (~0,2 s/page mesure) : on passe a 8 pages = 800 replies (~32 h de
        # volume). Le tri FIFO (le plus ANCIEN d'abord) fait le reste.
        os.environ.setdefault("LIST_MAX_PAGES", "8")
        result: dict = {"ok": True}
        try:
            import run_bot  # scripts/run_bot.py

            # Quota d'itérations LOURDES (draft+send Sonnet). Chaque heavy ~12s →
            # avec budget 26s on en fait 1-2 par run. Cron 5 min = 12-24/h en
            # autonome. La file FIFO garantit que rien ne starve (le plus vieux
            # non-répondu passe toujours en premier). --since-days 1 = moins de
            # data à lister (plus rapide). Au-delà : bouton "Pour cet email".
            # Quota augmenté : 6 → 20 pour résorber un backlog rapidement.
            # FENÊTRE : since-days 10 (au lieu de 2). CRITIQUE — avec une fenêtre
            # de 2j, tout reply de plus de 2 jours n'était JAMAIS listé donc ni
            # répondu ni alerté (cause des négatifs sans réponse + alertes ratées
            # vues le 2026-06-15 : replies du 8-12 juin jamais traités). Le
            # pré-filtre MGET (bot:processed:*) + l'idempotence thread font qu'un
            # reply déjà géré est sauté instantanément → élargir la fenêtre est
            # sûr, ça ne re-traite rien, ça rattrape juste les oubliés.
            limit = os.environ.get("CRON_LIMIT", "20")
            since_days = os.environ.get("CRON_SINCE_DAYS", "10")
            sys.argv = [
                "run_bot",
                "--no-dry-run",
                "--limit", limit,
                "--since-days", since_days,
            ]
            started = time.time()
            due = _spaces_due()
            prev_budget = os.environ.get("RUN_BUDGET_SECONDS")
            if due:
                # On raccourcit CE passage principal pour laisser la place aux
                # espaces. La file FIFO garantit que rien n'est perdu : ce qui
                # n'est pas traite ici passe au tour suivant (5 min plus tard).
                os.environ["RUN_BUDGET_SECONDS"] = str(
                    int(max(10.0, CRON_ENVELOPE_S - SPACES_RESERVE_S))
                )
            try:
                code = run_bot.main()
            finally:
                if prev_budget is None:
                    os.environ.pop("RUN_BUDGET_SECONDS", None)
                else:
                    os.environ["RUN_BUDGET_SECONDS"] = prev_budget
            result["exit_code"] = code
            if due:
                from src.spaces import run_spaces
                left = CRON_ENVELOPE_S - (time.time() - started)
                result["spaces"] = run_spaces(total_budget_s=max(0.0, left))
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
