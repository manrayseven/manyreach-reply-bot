"""Mini-lanceur pour exécuter les tests SANS pytest installé.

Chaque fichier de test fait `from tests._runner import run; run(globals())` dans
son bloc __main__ → lance toutes les fonctions test_* et affiche un résumé.
(Les mêmes fonctions sont découvertes par pytest si tu l'installes un jour.)
"""
from __future__ import annotations

import sys
import traceback


def run(ns: dict) -> int:
    tests = sorted((n, f) for n, f in ns.items() if n.startswith("test_") and callable(f))
    ok = 0
    fails = []
    for name, fn in tests:
        try:
            fn()
            ok += 1
            print(f"  ✓ {name}")
        except Exception as e:  # noqa: BLE001
            fails.append((name, e))
            print(f"  ✗ {name} : {e}")
            traceback.print_exc()
    print(f"\n{ok}/{len(tests)} OK" + (f", {len(fails)} échec(s)" if fails else ""))
    return 1 if fails else 0


def main(ns: dict) -> None:
    sys.exit(run(ns))
