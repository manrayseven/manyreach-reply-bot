"""Historique COMPLET d'une conversation prospect, pour l'email de transfert.

Rudy 16/09 : « tu n'as pas mis l'historique complet des échanges (et ça vaut
pour tous) ». Deux obstacles :
  1. Le dashboard récupérait l'historique À L'AFFICHAGE (14 prospects par
     chargement, cache 30 min) → avec toutes les alertes affichées, il n'arrivait
     jamais pour tout le monde. → Le bot le construit et le STOCKE avec l'alerte.
  2. Dans un ESPACE (workspace ManyReach, ex. Cmaclim), le fil d'un prospect ne
     contient QUE ses réponses : nos envois de campagne n'y sont pas rattachés et
     leur corps est vide via l'API. → On reconstitue nos emails depuis la
     CITATION que chaque réponse contient (« Le …, Romain Viard a écrit : … »).

Fonctions PURES (aucun appel réseau) → testables.
"""
from __future__ import annotations

import re
from datetime import datetime

from .classifier import _QUOTE_END_RE, _strip_html, _trim_quoted_history

MAX_TEXT = 700      # réponses du prospect
MAX_OURS = 400      # nos emails (contexte ; le transfert en montre 240)
MAX_ITEMS = 16

_MONTHS = {
    "janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "août": 8, "aout": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12,
}
_ISO_DT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})")
_FR_DT_RE = re.compile(
    r"(\d{1,2})\s+(janv|févr|fevr|mars|avr|mai|juin|juil|août|aout|sept|oct|nov|déc|dec)"
    r"[a-zéû]*\.?\s+(\d{4})(?:\D{1,6}(\d{1,2})[:h](\d{2}))?",
    re.IGNORECASE,
)


def _header_dt(header: str, ref: datetime | None) -> datetime | None:
    """Date lue dans un en-tête de citation (« Le 2026-09-07 09:12, … » ou « Le
    lun. 7 sept. 2026 à 09:12, … »), au fuseau de `ref`. None si illisible."""
    tz = ref.tzinfo if ref else None
    m = _ISO_DT_RE.search(header)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, h, mi, tzinfo=tz)
        except ValueError:
            return None
    m = _FR_DT_RE.search(header)
    if m:
        key = m.group(2).lower()
        month = _MONTHS.get(key[:4]) or _MONTHS.get(key[:3])
        if month:
            try:
                return datetime(int(m.group(3)), month, int(m.group(1)),
                                int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=tz)
            except ValueError:
                return None
    return None


def quoted_message(clean_body: str, ref: datetime | None = None) -> tuple[str, datetime | None]:
    """(texte cité, date de l'email cité ou None) pour le PREMIER niveau de
    citation d'une réponse nettoyée ; ("", None) s'il n'y en a pas."""
    m = _QUOTE_END_RE.search(clean_body or "")
    if not m:
        return "", None
    header = clean_body[max(0, m.start() - 160):m.start()]
    quoted = _trim_quoted_history(clean_body[m.end():].strip(), MAX_OURS).strip()
    quoted = re.sub(r"\s*--\s*$", "", quoted).strip()  # « -- » de signature
    return quoted, _header_dt(header, ref)


def build_history(thread, current_reply=None) -> list[dict]:
    """Liste [{who, when, text}] du plus ancien au plus récent.

    `thread` : messages ManyReach (Sent/SentManual/Reply) du prospect.
    `current_reply` : la réponse qui déclenche l'alerte (ajoutée si absente du fil).
    Quand le fil ne contient AUCUN de nos envois lisibles (cas des espaces), nos
    emails sont reconstitués depuis les citations des réponses du prospect.
    """
    msgs = [m for m in (thread or []) if getattr(m, "created_at", None)]
    if current_reply is not None and all(
        m.message_id != current_reply.message_id for m in msgs
    ):
        msgs.append(current_reply)
    msgs.sort(key=lambda m: m.created_at)
    has_sent = any(m.type in ("Sent", "SentManual") and (m.body or "").strip() for m in msgs)

    rows: list[tuple[datetime, int, dict]] = []
    seen_ids: set[str] = set()
    seen_texts: set[tuple[str, str]] = set()

    def _add(who: str, dt: datetime | None, fallback: datetime, text: str) -> None:
        text = (text or "").strip()
        key = (who, " ".join(text.split())[:180].lower())
        if not text or key in seen_texts:
            return
        seen_texts.add(key)
        when = dt.strftime("%d/%m %H:%M") if dt else ""
        rows.append((dt or fallback, len(rows), {"who": who, "when": when, "text": text}))

    for m in msgs:
        if m.message_id in seen_ids:  # Sent + SentManual en double (même msgId)
            continue
        seen_ids.add(m.message_id)
        clean = _strip_html(m.body or "")
        if m.type == "Reply":
            if not has_sent:
                q_text, q_dt = quoted_message(clean, m.created_at)
                if q_text:
                    # Sans date lisible : juste avant la réponse qui le cite.
                    _add("Vous", q_dt, m.created_at, q_text)
            _add("Prospect", m.created_at, m.created_at, _trim_quoted_history(clean, MAX_TEXT))
        else:
            _add("Vous", m.created_at, m.created_at, _trim_quoted_history(clean, MAX_OURS))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in rows][-MAX_ITEMS:]
