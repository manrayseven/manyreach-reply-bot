"""Endpoint admin pour des actions ponctuelles : marquer un sender orphan
comme déjà traité (stoppe immédiatement un loop de duplicate sends).

Usage : GET /api/admin?action=mark_orphan&email=foo@bar.com
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import kvstore  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        action = query.get("action", [""])[0]
        result: dict = {"ok": True, "action": action}

        if action == "mark_orphan":
            email = query.get("email", [""])[0].strip().lower()
            if not email:
                result = {"ok": False, "error": "missing ?email="}
            else:
                kvstore.force_mark_orphan(email)
                result["email"] = email
                result["marked"] = True
        elif action == "mark_orphan_bulk":
            emails = query.get("emails", [""])[0]
            marked = []
            for e in emails.split(","):
                e = e.strip().lower()
                if e:
                    kvstore.force_mark_orphan(e)
                    marked.append(e)
            result["marked"] = marked
        else:
            result = {"ok": False, "error": "unknown action", "available": ["mark_orphan", "mark_orphan_bulk"]}

        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
