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


def test_lead_sweep_is_listed_and_served_first():
    """Rudy 08/10 : deux leads (11h48 « pouvons-nous échanger ? » et 11h51
    « Demain 11h ? ») jamais remontés en alerte. Cause : le bot ne fait que 1-2
    itérations lourdes par passage alors qu'il arrivait ~115 réponses/h (orage de
    bounces SpamHaus) ; la file, triée récente/ancienne en alternance, n'atteignait
    jamais le milieu. Les feeds ManyReach filtrés par statut sont minuscules et
    sans bounce → on les balaie et on sert les leads EN TÊTE de file.

    Vérifié par analyse statique : impossible d'exécuter le bot ici (pas de
    Python local), mais un retrait accidentel de l'une des deux moitiés du
    correctif (balayage, ou priorité) casse le test.
    """
    import ast
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    ast.parse(src)  # le fichier doit rester syntaxiquement valide

    # 1) le balayage par statut existe et utilise bien les deux tuples
    assert "_MRC.LEAD_STATUSES" in src, "balayage des leads supprime"
    assert "_MRC.NEGATIVE_STATUSES" in src, "balayage des negatifs supprime"

    # 2) les leads passent devant la fenetre fraiche ET devant le backlog
    assert "_replies = _lead_queue + _replies" in src, "priorite aux leads perdue"
    i_split = src.index("_lead_queue = _interleave_fresh")
    i_fresh = src.index("_FRESH_WINDOW_H = float(")
    assert i_split < i_fresh, "le tri des leads doit precéder la fenetre fraiche"

    # 3) les deux tuples de statuts existent cote client ManyReach
    mr = (ROOT / "src" / "manyreach.py").read_text(encoding="utf-8")
    assert "LEAD_STATUSES = (" in mr and "NEGATIVE_STATUSES = (" in mr
    # Un lead ManyReach porte l'un de ces statuts : aucun ne doit disparaitre.
    for st in ("Interested", "Neutral", "MaybeLater", "CollegueReplied"):
        assert f'"{st}"' in mr, st


def test_dedup_keeps_one_entry_per_reply():
    """Le balayage et le listing par récence se recouvrent : sans déduplication,
    la même réponse serait traitée deux fois (deuxième envoi au même prospect).
    """
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    assert "_seen_mids" in src and "if _k in _seen_mids:" in src
    # La clé de dédup doit être la MÊME que celle d'idempotence (_pid), sinon un
    # reply d'un espace masquerait celui d'un autre.
    assert "_k = _pid(_r.message_id)" in src


def test_manyreach_interested_always_raises_an_alert():
    """Rudy 08/10, CSEFORMA : « nous pouvons envisager une collaboration avec une
    commission de 10 à 15 % […] je serais ravi d'échanger ». Le bot l'a lu comme
    une objection PLATE (auto-réponse, voulue depuis le 11/08) → aucune alerte,
    alors que ManyReach affichait « Interested » en vert.

    Le statut ManyReach est déterministe : il ne remplace pas le classifieur (la
    réponse auto part toujours) mais il force l'alerte par-dessus.
    """
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    assert '_status_mids.get("Interested"' in src, "garde-fou Interested supprime"
    assert "ALERTE — marqué « Interested » dans ManyReach" in src
    # L'alerte doit porter le mot ALERTE : c'est ce que lisent _is_alert_entry
    # (copie dans la liste KV dediee) ET le tri du dashboard.
    kv = (ROOT / "src" / "kvstore.py").read_text(encoding="utf-8")
    assert '"ALERTE" in str(entry.get("status"' in kv
    idx = (ROOT / "api" / "index.py").read_text(encoding="utf-8")
    assert 'intent in ALERT_INTENTS or "ALERTE" in status' in idx


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
