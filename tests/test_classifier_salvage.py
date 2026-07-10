"""Tests de robustesse du parsing classifier (JSON malformé / intent invalide).

Capture le bug du 09/07 : reply = juste un numéro de tél → le modèle sort un JSON
tronqué avec intent="contact_phone" (nom de champ, pas un intent) → crash
"Classifier returned non-JSON" en boucle. Doit maintenant être récupéré + fallback.

Lance sans pytest :  python tests/test_classifier_salvage.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.classifier import Classification, VALID_INTENTS, _salvage_classification  # noqa: E402


# La sortie exacte (tronquée) observée le 09/07.
MALFORMED = ('{ "intent": "contact_phone", "confidence": 0.92, '
             '"key_phrase": "0659860714", "redirected_email&q')


def _finalize(raw):
    """Réplique la logique de classify() après l'appel modèle (salvage + fallback)."""
    import json
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = _salvage_classification(raw)
    if data.get("intent") not in VALID_INTENTS:
        data = {**data, "intent": "interested_warm",
                "confidence": min(float(data.get("confidence", 0.5) or 0.5), 0.7)}
    return Classification.from_json(data)


def test_salvage_extracts_fields():
    d = _salvage_classification(MALFORMED)
    assert d["intent"] == "contact_phone"      # extrait tel quel (invalide, filtré après)
    assert d["confidence"] == 0.92
    assert d["key_phrase"] == "0659860714"


def test_invalid_intent_falls_back_to_interested_warm():
    c = _finalize(MALFORMED)
    assert c.intent == "interested_warm"       # fallback sûr (→ alerte, review)
    assert c.confidence <= 0.7


def test_truncated_valid_intent_is_recovered():
    raw = '{ "intent": "not_interested_polite", "confidence": 0.9, "key_phrase": "pas inter'
    c = _finalize(raw)
    assert c.intent == "not_interested_polite"


def test_valid_json_still_parses():
    import json
    raw = json.dumps({"intent": "meeting_confirmed", "confidence": 0.95,
                      "key_phrase": "mardi 14h", "language": "fr"})
    c = _finalize(raw)
    assert c.intent == "meeting_confirmed" and c.confidence == 0.95


if __name__ == "__main__":
    from tests._runner import main
    main(dict(globals()))
