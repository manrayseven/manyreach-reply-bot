"""Garde-fou de portée sur scripts/run_bot.py.

Le corps de la boucle de traitement vit dans la fonction imbriquée
`_process_one` (traitement parallèle, 07/09). Une variable de `main()`
RÉASSIGNÉE dedans sans `nonlocal` y devient locale → UnboundLocalError à la
première lecture. C'est arrivé avec `sends_done` / `error_count` : plus aucun
envoi auto du 07/09 au 14/09, et chaque refus re-classifié + re-rédigé par l'IA
à chaque cron. Ce test l'attrape par analyse statique (sans exécuter le bot).
"""
from __future__ import annotations

import ast
from pathlib import Path

RUN_BOT = Path(__file__).resolve().parents[1] / "scripts" / "run_bot.py"


def _find_func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"fonction {name} introuvable dans run_bot.py")


def _assigned_names(fn: ast.FunctionDef) -> set[str]:
    """Noms liés directement dans `fn` (hors fonctions/lambdas imbriquées)."""
    out: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            # Portées propres : fonctions, classes ET compréhensions (Python 3).
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
                                  ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                out.add(child.id)
            visit(child)

    visit(fn)
    return out


def test_process_one_declares_nonlocal_for_main_variables():
    tree = ast.parse(RUN_BOT.read_text(encoding="utf-8"))
    main = _find_func(tree, "main")
    proc = _find_func(main, "_process_one")
    main_names = _assigned_names(main)
    proc_params = {a.arg for a in proc.args.args}
    declared = {
        n for node in ast.walk(proc) if isinstance(node, ast.Nonlocal) for n in node.names
    }
    # Tout nom de main() réassigné dans _process_one doit être nonlocal (le
    # paramètre `reply` mis à part). Si un homonyme volontaire apparaît un jour,
    # renomme-le plutôt que de l'exclure ici.
    shadowed = (_assigned_names(proc) & main_names) - proc_params - declared
    assert not shadowed, (
        "variables de main() réassignées dans _process_one sans nonlocal "
        f"(UnboundLocalError garanti) : {sorted(shadowed)}"
    )


def _import_names(nodes) -> set[str]:
    out: set[str] = set()
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                out.add((al.asname or al.name).split(".")[0])
    return out


def test_process_one_does_not_reimport_module_names():
    # Un `from src.actions import ALERT_ONLY` LOCAL dans _process_one rend le nom
    # local à TOUTE la fonction : l'utiliser plus haut → UnboundLocalError (piège
    # rencontré le 16/09 en ajoutant la règle « piste en cours »).
    tree = ast.parse(RUN_BOT.read_text(encoding="utf-8"))
    module_names = _import_names(tree.body)
    proc = _find_func(_find_func(tree, "main"), "_process_one")
    local_imports = _import_names(ast.walk(proc))
    clash = local_imports & module_names
    assert not clash, f"imports locaux qui masquent un nom du module : {sorted(clash)}"


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
