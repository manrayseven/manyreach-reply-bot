"""Apprentissage progressif du tri (Rudy 14/11).

« J'aimerais que tu apprennes de toi-même progressivement à détecter intérêt /
pas d'intérêt / simple objection, car c'est fatigant. »

Cinq cas du 12/11, tous mal triés :

  Éliane Zimmer    « nous ne ressentons pas le besoin »      → refus,   alerté
  Linda Kortylak   « je suis déjà accompagnée »              → refus,   alerté
  Jean Duboc       « ce n'est pas d'actualité »              → refus,   alerté
  Yannick Guerton  « ma fille gère mes media »               → refus,   alerté
  Kalli Tantra     « je dois filtrer les clients… »          → objection, étiquetée refus

Quatre portaient le tag bot:info-requested : ce n'est PAS le classifieur qui a
dérapé, c'est le garde-fou anti-devinette du 06/11. Faute de pouvoir confirmer
un refus explicite, il réécrivait l'intention en « demande d'infos » — une
fausse alerte doublée d'un faux libellé.

Lance sans pytest :  python tests/test_apprentissage.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

RUN_BOT = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")


def _tuple_literal(nom: str) -> tuple[str, ...]:
    tree = ast.parse(RUN_BOT)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == nom
                and isinstance(node.value, ast.Tuple)):
            return tuple(e.value for e in node.value.elts
                         if isinstance(e, ast.Constant))
    raise AssertionError(f"{nom} introuvable")


def _regex_literal(nom: str):
    """Le filet ne se limite plus a des morceaux de phrase colles : une forme
    (negation + ce qui est nie, meme separes) couvre les intercalations."""
    import re as _re
    tree = ast.parse(RUN_BOT)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == nom
                and isinstance(node.value, ast.Call)):
            return _re.compile(ast.literal_eval(node.value.args[0]), _re.IGNORECASE)
    raise AssertionError(f"{nom} introuvable")


def test_les_quatre_refus_du_12_11_sont_reconnus():
    """Filet de sécurité : même sans le classifieur, ces tournures sont des
    refus. Aucune ne contient de formule de refus toute faite."""
    plats = _tuple_literal("_FLAT_REFUSAL_MARKERS") + _tuple_literal("_CLEAR_NO_MARKERS")
    forme = _regex_literal("_REFUS_FORME_RE")
    for msg in (
        "je vous remercie pour votre sollicitation mais nous ne ressentons pas "
        "pour l'instant le besoin de faire appel à vos services",
        "merci pour ce message, mais je suis déjà accompagnée pour mon site",
        "ce n'est pas d'actualité pour l'instant",
        "merci pour votre proposition. ma fille qui est directrice artistique "
        "gère mes media",
    ):
        assert any(m in msg for m in plats) or forme.search(msg), msg


def test_la_confiance_est_graduee_et_non_plus_conditionnee_a_une_negation():
    """« Je suis déjà accompagnée » et « ma fille gère mes media » ne
    contiennent aucun mot de négation : exiger « pas/non/aucun » les envoyait
    systématiquement en alerte."""
    i = RUN_BOT.index("CONFIANCE GRADUÉE")
    bloc = RUN_BOT[i:i + 1200]
    assert "classification.confidence >= 0.90" in bloc
    assert "classification.confidence >= 0.85" in bloc
    # Le seuil haut ne doit PAS exiger de négation…
    haut = bloc.index("0.90")
    neg = bloc.index(r"\b(pas|non|aucun|rien|jamais)\b")
    assert haut < neg, "la négation conditionne encore le seuil haut"
    # …et la longueur reste une condition dans les deux cas.
    assert "len(_clean_body) < 250" in bloc


def test_les_lecons_sont_injectees_dans_le_classifieur():
    """Le vrai correctif : des cas tranchés relus à chaque passage, et des
    corrections cliquées qui s'ajoutent sans toucher au code."""
    clf = (ROOT / "src" / "classifier.py").read_text(encoding="utf-8")
    assert "LECONS_PATH" in clf and "lecons.md" in clf
    assert "_format_lecons" in clf
    # Collées au prompt SYSTÈME (donc mises en cache, coût quasi nul).
    assert "self.system_prompt +=" in clf

    lecons = (ROOT / "src" / "prompts" / "lecons.md").read_text(encoding="utf-8")
    for nom in ("Éliane Zimmer", "Linda Kortylak", "Jean Duboc",
                "Yannick Guerton", "Kalli Tantra"):
        assert nom in lecons, nom
    # Les trois issues voulues par Rudy sont nommées.
    assert "not_interested_polite" in lecons and "alerte" in lecons


def test_les_deux_boutons_d_apprentissage_existent():
    idx = (ROOT / "api" / "index.py").read_text(encoding="utf-8")
    assert 'name="verdict" value="refus"' in idx      # sur une alerte
    assert 'name="verdict" value="alerte"' in idx     # sur un envoi/silencieux
    assert 'elif action == "lecon":' in idx
    kv = (ROOT / "src" / "kvstore.py").read_text(encoding="utf-8")
    assert "def add_lecon(" in kv and "def lecons(" in kv
    # Une correction « refus » referme aussi la ligne : c'est le geste normal.
    assert "kvstore.dismiss_alert(_lalert)" in idx


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
