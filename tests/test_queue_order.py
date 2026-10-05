"""Ordre de la file des réponses fraîches (scripts/run_bot.py).

Rudy 05/10, école Château Gombert : réponse reçue à 9h52, alerte seulement à
18h. La fenêtre fraîche était triée « plus récente d'abord » → une réponse du
matin repassait derrière chaque nouvelle arrivée et n'avançait jamais.

Lance sans pytest :  python tests/test_queue_order.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from run_bot import _interleave_fresh  # noqa: E402


def test_alternates_newest_and_oldest():
    # ["10h", "9h", "8h"] = du plus récent au plus ancien.
    assert _interleave_fresh(["10h", "9h", "8h"]) == ["10h", "8h", "9h"]
    assert _interleave_fresh(["4", "3", "2", "1"]) == ["4", "1", "3", "2"]


def test_edge_cases():
    assert _interleave_fresh([]) == []
    assert _interleave_fresh(["seule"]) == ["seule"]
    # Aucune perte ni doublon, quelle que soit la taille.
    src = list(range(25))
    out = _interleave_fresh(src)
    assert sorted(out) == src and len(out) == len(src)


def test_oldest_fresh_is_served_second_not_last():
    # Le point du 05/10 : la plus ancienne de la fenêtre ne doit plus attendre
    # que toutes les autres soient passées.
    by_recent = [f"r{i}" for i in range(50)]      # r0 = la plus récente
    out = _interleave_fresh(by_recent)
    assert out[1] == "r49", out[:3]
    assert out.index("r48") <= 3, out[:5]


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
