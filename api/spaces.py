"""Vercel entry point — traite les ESPACES secondaires (workspaces ManyReach).

Endpoint SEPARE de /api/cron : un workspace a sa propre cle API donc son propre
passage complet, et les enchainer dans /api/cron ferait depasser le timeout de
30 s du cron externe — le passage principal passerait pour un echec.

La logique vit dans src/spaces.py, partagee avec le bouton "Lancer les espaces"
du dashboard pour que les deux se comportent a l'identique.

Securite : identique a /api/cron — si CRON_SECRET est defini, exige l'en-tete
Authorization: Bearer <secret>.
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
        from src.spaces import run_spaces
        self._json(200, run_spaces())

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
