"""Lance TOUS les tests (sans pytest).  Usage : python tests/run_all.py"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._runner import run  # noqa: E402

total_fail = 0
for f in sorted(Path(__file__).parent.glob("test_*.py")):
    name = f"tests.{f.stem}"
    print(f"\n=== {f.stem} ===")
    mod = importlib.import_module(name)
    total_fail += run(vars(mod))

print("\n" + ("❌ DES TESTS ONT ÉCHOUÉ" if total_fail else "✅ TOUS LES TESTS PASSENT"))
sys.exit(1 if total_fail else 0)
