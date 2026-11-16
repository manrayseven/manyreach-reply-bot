# -*- coding: utf-8 -*-
"""Rester ou l'on etait apres un clic (Rudy 16/11).

« Dans le dashboard, des que j'applique un ordre avec un bouton, la page se
recharge tout en haut de la page. »

Chaque bouton POSTe, le serveur repond 303 vers le dashboard : le navigateur
recharge et revient en haut. Sur une liste d'alertes de plusieurs ecrans, on
perd sa place a chaque clic.

Le piege de ce correctif : le HTML est une f-string Python. Une accolade de
JavaScript ecrite SIMPLE y est lue comme un champ a remplacer — la page ne se
rend plus du tout. Ce test verifie donc que le script sort du rendu avec des
accolades simples, et qu'il est bien dans la page.

Lance sans pytest :  python tests/test_position_page.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

IDX = (ROOT / "api" / "index.py").read_text(encoding="utf-8")


def _script_source() -> str:
    i = IDX.index("dash:scroll")
    return IDX[IDX.rindex("<script>", 0, i):IDX.index("</script>", i)]


def test_le_script_est_present_et_ecoute_les_envois():
    s = _script_source()
    assert "addEventListener('submit'" in s
    # Capture : les formulaires desactivent leur bouton dans leur propre
    # onsubmit. En phase de capture on enregistre AVANT eux.
    assert "}}, true);" in s
    assert "scrollTo(" in s


def test_les_accolades_sont_doublees_dans_la_f_string():
    """Sinon Python lit `{ try {` comme un champ et le rendu explose."""
    s = _script_source()
    # Chaque accolade du JS doit etre doublee dans le source Python.
    sans_doubles = s.replace("{{", "").replace("}}", "")
    assert "{" not in sans_doubles, "accolade simple dans la f-string"
    assert "}" not in sans_doubles, "accolade simple dans la f-string"


def test_tout_est_protege_si_sessionStorage_est_refuse():
    """Navigation privee, cookies bloques : la page doit marcher comme avant,
    pas afficher une erreur et cesser de repondre."""
    s = _script_source()
    assert s.count("try {{") >= 3
    assert s.count("catch") >= 3


def test_la_position_est_bornee_a_la_hauteur_de_la_page():
    """Une alerte traitee disparait, la page raccourcit : sans borne, le
    navigateur ignore la consigne et nous laisse en haut."""
    s = _script_source()
    assert "scrollHeight" in s and "Math.min" in s


def test_le_rendu_sort_du_javascript_valide():
    """Le vrai filet : on rend la page et on relit le script obtenu."""
    import api.index as index  # noqa: PLC0415
    from tests.test_render_smoke import _fake_kv  # noqa: PLC0415

    fake = _fake_kv([])
    reel = index.kvstore
    index.kvstore = fake
    try:
        page = index._render()
    finally:
        index.kvstore = reel
    i = page.index("dash:scroll")
    js = page[page.rindex("<script>", 0, i) + len("<script>"):page.index("</script>", i)]
    assert "{{" not in js and "}}" not in js
    assert js.count("{") == js.count("}")
    assert js.count("(") == js.count(")")


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
