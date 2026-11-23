# -*- coding: utf-8 -*-
"""Le prospect dit lui-même quand le relancer (Rudy 16/11).

    « Merci de m'avoir relancé. Pour répondre a votre question, oui.
      Les éléments que j'attendais, je sont pas encore disponibles.
      Je pense, 1er trimestre 2027. »
    — Eric Edouard (lia-securite.com), 16/11

Deux choses manquaient, pas une :

1. Le classifieur lisait DÉJÀ cette date (`recontact_datetime`) depuis des
   mois. Personne ne s'en servait : elle était lue, puis jetée.
2. Et même notée, elle n'aurait rien changé : `decide_bump` refuse de relancer
   un fil dont le dernier message est une réponse du prospect. Or c'est
   exactement la forme de ces messages-là. Celui qui dit quand le rappeler
   était précisément celui qu'on ne rappelait jamais.

Lance sans pytest :  python tests/test_relance_promise.py
"""
from __future__ import annotations

import ast
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

RUN_BOT = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
RUN_BUMPS = (ROOT / "scripts" / "run_bumps.py").read_text(encoding="utf-8")

MAINTENANT = datetime(2026, 11, 16, tzinfo=timezone.utc)


def _date_relance():
    """Isole le normalisateur sans importer run_bot (dépendances réseau)."""
    tree = ast.parse(RUN_BOT)
    bouts = []
    for node in tree.body:
        nom = getattr(node, "name", None)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            nom = node.targets[0].id
        if nom in ("_RELANCE_MAX_JOURS", "_date_relance"):
            bouts.append(ast.get_source_segment(RUN_BOT, node))
    espace: dict = {"datetime": datetime, "timezone": timezone}
    exec("\n".join(bouts), espace)  # noqa: S102 — code du dépôt
    return espace["_date_relance"]


DATE = _date_relance()


def test_le_premier_trimestre_2027_est_retenu():
    assert DATE("2027-01-01", MAINTENANT) == "2027-01-01"
    assert DATE("2027-01-01T00:00:00+00:00", MAINTENANT) == "2027-01-01"


def test_une_date_passee_n_est_pas_un_rendez_vous():
    assert DATE("2026-11-16", MAINTENANT) is None   # aujourd'hui
    assert DATE("2025-03-01", MAINTENANT) is None


def test_une_date_absurde_est_ignoree():
    """Au-delà de trois ans, c'est une lecture ratée, pas une intention."""
    assert DATE("2099-01-01", MAINTENANT) is None
    assert DATE("plus tard", MAINTENANT) is None
    assert DATE("", MAINTENANT) is None
    assert DATE(None, MAINTENANT) is None
    assert DATE("2027-13-45", MAINTENANT) is None


def test_le_message_cite_le_prospect_et_sa_periode():
    from src.bumps import build_promise_body, mois_fr  # noqa: PLC0415

    assert mois_fr("2027-01-01") == "en janvier 2027"
    assert mois_fr("n'importe quoi") == "à cette période"

    class _P:
        first_name = "Eric"

    corps = build_promise_body(_P(), "2027-01-01", "Je pense, 1er trimestre 2027")
    assert "Bonjour Eric" in corps
    assert "en janvier 2027" in corps
    assert "Je pense, 1er trimestre 2027" in corps
    # Court : Rudy a mesuré que les relances longues convertissent moins.
    import re as _re  # noqa: PLC0415
    mots = len(_re.sub(r"<[^>]+>", " ", corps).split())
    assert mots < 70, mots

    # Sans phrase exploitable, on ne cite rien plutôt qu'une citation vide.
    assert "« »" not in build_promise_body(_P(), "2027-01-01", "ok")


def test_l_agenda_garde_un_seul_rendez_vous_par_adresse():
    from src import kvstore  # noqa: PLC0415

    faux: dict = {}

    def _cmd(*a):
        if a[0] == "HSET":
            faux[a[2]] = a[3]
            return 1
        if a[0] == "HGETALL":
            return dict(faux)
        if a[0] == "HDEL":
            faux.pop(a[2], None)
            return 1
        return None

    vrai_cmd, vrai_dispo = kvstore._cmd, kvstore.kv_available
    kvstore._cmd, kvstore.kv_available = _cmd, (lambda: True)
    try:
        kvstore.plan_relance("A@Exemple.FR", "2027-01-01", "1er trimestre 2027")
        kvstore.plan_relance("a@exemple.fr", "2027-04-01", "finalement avril")
        kvstore.plan_relance("b@exemple.fr", "2026-12-01", "debut decembre")
        tous = kvstore.relances()
        assert len(tous) == 2, tous
        # Le dernier dit remplace le precedent, et l'adresse est normalisee.
        a = [r for r in tous if r["email"] == "a@exemple.fr"][0]
        assert a["le"] == "2027-04-01"
        # Tri par echeance : le plus proche d'abord.
        assert [r["le"] for r in tous] == ["2026-12-01", "2027-04-01"]
        assert [r["email"] for r in kvstore.relances_dues("2026-12-15")] == ["b@exemple.fr"]
        assert kvstore.relances_dues("2026-11-16") == []
        kvstore.drop_relance("B@exemple.fr")
        assert len(kvstore.relances()) == 1
        # Une date illisible ne cree pas de rendez-vous fantome.
        kvstore.plan_relance("c@exemple.fr", "plus tard", "")
        assert len(kvstore.relances()) == 1
    finally:
        kvstore._cmd, kvstore.kv_available = vrai_cmd, vrai_dispo


def test_le_bot_note_la_date_au_moment_de_l_alerte():
    i = RUN_BOT.index("if classification.intent in ALERT_ONLY:")
    bloc = RUN_BOT[i:i + 1400]
    assert "_date_relance(classification.recontact_datetime" in bloc
    assert "kvstore.plan_relance(" in bloc
    # Jamais en simulation : un dry-run ne doit rien laisser derriere lui.
    assert "not dry_run" in bloc


def test_le_moteur_traite_l_agenda_avant_la_cadence_fixe():
    """Ces fils finissent par une reponse du prospect : la boucle normale les
    saute toujours. S'ils ne passent pas AVANT, ils ne passent jamais."""
    i_ag = RUN_BUMPS.index("agenda_complet = kvstore.relances()")
    i_boucle = RUN_BUMPS.index("for _statut, reply in _flux:")
    assert i_ag < i_boucle, "l'agenda doit etre traite avant la boucle"
    assert "build_promise_body(" in RUN_BUMPS


def test_un_rendez_vous_est_consomme_dans_tous_les_cas():
    """Relance envoyee, statut terminal, prospect revenu de lui-meme, fiche
    disparue : chaque sortie retire le rendez-vous. Sinon l'agenda accumule
    des dettes qu'on ne solde pas."""
    deb = RUN_BUMPS.index("agenda_complet = kvstore.relances()")
    fin = RUN_BUMPS.index("for _statut, reply in _flux:")
    bloc = RUN_BUMPS[deb:fin]
    assert bloc.count("kvstore.drop_relance(email)") >= 5, bloc.count("drop_relance")
    # Et on ne relance pas quelqu'un qui a repris contact entre-temps — ni
    # quelqu'un a qui RUDY a repondu depuis (23/11) : c'est src/agenda.py qui
    # relit le fil, a partir de `pose_le`.
    assert "agenda.juger(" in bloc
    AG = (ROOT / "src" / "agenda.py").read_text(encoding="utf-8")
    assert "pose_le" in AG and 'm.type == "Reply"' in AG


def test_le_tableau_de_bord_montre_et_permet_d_annuler():
    idx = (ROOT / "api" / "index.py").read_text(encoding="utf-8")
    assert "Relances programmées" in idx
    assert 'value="drop_relance"' in idx
    assert 'elif action == "drop_relance":' in idx


def test_on_peut_programmer_une_relance_a_la_main():
    """Le bot ne note que ce qu'il lit APRES ce correctif, et une date donnee
    au telephone n'arrive jamais par email. Sans ce champ, Eric Edouard — le
    cas qui a motive tout ceci, deja traite — serait reste sur le carreau."""
    idx = (ROOT / "api" / "index.py").read_text(encoding="utf-8")
    assert 'value="plan_relance"' in idx
    assert 'elif action == "plan_relance":' in idx
    assert 'type="date" name="le"' in idx
    # La carte reste visible meme vide, sinon le champ d'ajout est inatteignable.
    assert '_rdv_rows) or _rdv_vide' in idx


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
