"""Rapport des mises en relation d'un compte (api/index.py).

Rudy 01/10 : au 1er « Copier l'email », la mise en relation est archivée ; un
bouton du dashboard génère le compte rendu du compte — synthèse des leads puis
le détail mois par mois — copiable dans un email ou imprimable en PDF.

Lance sans pytest :  python tests/test_handoff_report.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

import index  # noqa: E402

CLIENT = {"id": "cmaclim", "name": "Cmaclim"}


def _rec(at, company, email, msg="Pouvez-vous m'appeler ?", hist=None):
    return {
        "at": at, "client_id": "cmaclim", "email": email, "company": company,
        "detail": "Crèche, Marseille", "contact": f"Direction - tél 04 91 00 00 00 - {email}",
        "campaign": "Chaleur dans vos locaux", "message": msg,
        "history": hist if hist is not None else [
            {"who": "Vous", "when": "07/09 09:12", "text": "Mesdames, Messieurs, …"},
            {"who": "Prospect", "when": "14/09 06:37", "text": msg},
        ],
    }


def test_month_label():
    assert index._month_label("2026-09-14T06:37:22+00:00") == ("2026-09", "Septembre 2026")
    assert index._month_label("")[1] == "Date inconnue"


def test_report_groups_by_month_newest_first_with_full_cards():
    recs = [
        _rec("2026-10-01T08:00:00+00:00", "Ma Little Crèche", "caroline@malittlecreche.fr"),
        _rec("2026-09-20T08:00:00+00:00", "École Major", "ce.0130741k@ac-aix-marseille.fr"),
        _rec("2026-09-02T08:00:00+00:00", "EVS Chantemerle", "coordo.evs.clef@gmail.com"),
    ]
    out = index._handoff_report_html(CLIENT, recs)
    # Synthèse en tête
    assert "Compte rendu — Cmaclim" in out and "3 prospect(s) transmis" in out
    # Mois du plus récent au plus ancien, avec le compte par mois
    i_oct, i_sep = out.index("Octobre 2026"), out.index("Septembre 2026")
    assert i_oct < i_sep, (i_oct, i_sep)
    assert "2 mise(s) en relation" in out and "1 mise(s) en relation" in out
    # Fiche complète de chaque transfert (société, contact, campagne, conversation)
    assert "Ma Little Crèche" in out and "EVS Chantemerle" in out
    assert "Chaleur dans vos locaux" in out and "La conversation" in out
    assert "caroline@malittlecreche.fr" in out


def test_report_without_records_is_explicit():
    out = index._handoff_report_html(CLIENT, [])
    assert "Aucune mise en relation enregistrée" in out
    assert "0 prospect(s) transmis" in out


def test_report_escapes_prospect_text():
    out = index._handoff_report_html(CLIENT, [_rec(
        "2026-10-01T08:00:00+00:00", "<script>x</script>", "a@b.fr")])
    assert "<script>" not in out and "&lt;script&gt;" in out


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
