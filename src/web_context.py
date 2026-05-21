"""Récupère un court contexte sur l'entreprise du prospect depuis son SITE WEB.

But : personnaliser la réponse avec UN détail réel (moins "IA générique", plus précis),
sans coûter cher en tokens (on ne garde que ~800 caractères : titre + meta description
+ début du texte de la home).

Sûr : on ne fetch QUE des sites web publics (pas de LinkedIn — contre leurs CGU et
souvent bloqué). Tolérant aux erreurs : si le fetch échoue, on renvoie "" et le bot
rédige normalement (jamais de blocage).
"""
from __future__ import annotations

import re

import httpx

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_company_context(url: str | None, max_chars: int = 800, timeout: float = 6.0) -> str:
    """Renvoie un court résumé textuel de la home du site, ou '' si indisponible."""
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", u):
        return ""
    try:
        r = httpx.get(
            u,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept-Language": "fr,en;q=0.8"},
        )
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", "").lower():
            return ""
        html = r.text
    except Exception:
        return ""

    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    desc_m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html,
        re.IGNORECASE,
    )
    if not desc_m:
        desc_m = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
            html,
            re.IGNORECASE,
        )

    body = re.sub(r"(?is)<(script|style|noscript|head|svg)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"&[a-z#0-9]+;", " ", body)
    body = re.sub(r"\s+", " ", body).strip()

    parts = []
    if title_m:
        parts.append("Titre du site : " + re.sub(r"\s+", " ", title_m.group(1)).strip())
    if desc_m:
        parts.append("Description : " + re.sub(r"\s+", " ", desc_m.group(1)).strip())
    if body:
        parts.append("Extrait : " + body[:500])
    out = " | ".join(parts)
    return out[:max_chars]
