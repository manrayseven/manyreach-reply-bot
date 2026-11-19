# -*- coding: utf-8 -*-
"""Le moteur de relance avance-t-il vraiment dans la liste ? (Rudy 19/11)

Relevé du 19/11 sur 324 fils Interested / MaybeLater :

    relances automatiques parties ......  10
    fils qui en attendaient une ........ 149   (silence median 13,7 j)
    maquettes en attente ...............  24   (dont 44 j et 37 j)

Le cron tournait pourtant tous les jours — la derniere relance datait du 18/11.
La cause n'etait ni le cron ni la fenetre de lecture (100 reponses par statut),
mais le BUDGET : 50 s, 55 requetes/minute, deux requetes par prospect. Soit une
vingtaine de prospects par passage — et comme la liste est triee du plus recent
au plus ancien, TOUJOURS les vingt memes, justement les plus frais, donc ceux
qu'il est trop tot pour relancer. Le 21e n'etait jamais atteint.

Trois correctifs, trois raisons de les tester :
  1. une memoire de passage, pour avancer au lieu de rabacher ;
  2. un plafond JOURNALIER, pour que 149 relances ne partent pas d'un coup ;
  3. une trace, parce que ce trou a tenu des semaines sans que rien ne le dise.

Lance sans pytest :  python tests/test_moteur_relance.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

BUMPS = (ROOT / "scripts" / "run_bumps.py").read_text(encoding="utf-8")


def _faux_kv():
    """kvstore en memoire : les fonctions reelles, un Redis simule."""
    from src import kvstore  # noqa: PLC0415

    base: dict = {}

    def _cmd(*a):
        op = a[0]
        if op == "HSET":
            base.setdefault(a[1], {}).update(
                {a[i]: a[i + 1] for i in range(2, len(a) - 1, 2)})
            return 1
        if op == "HGETALL":
            return dict(base.get(a[1], {}))
        if op == "HLEN":
            return len(base.get(a[1], {}))
        if op == "DEL":
            base.pop(a[1], None)
            return 1
        if op == "SET":
            base[a[1]] = a[2]
            return "OK"
        if op == "GET":
            return base.get(a[1])
        return None

    return kvstore, _cmd, base


def test_la_memoire_de_passage_fait_avancer_le_balayage():
    kv, _cmd, _ = _faux_kv()
    vrai, dispo = kv._cmd, kv.kv_available
    kv._cmd, kv.kv_available = _cmd, (lambda: True)
    try:
        maintenant = datetime(2026, 11, 19, 10, tzinfo=timezone.utc)
        kv.mark_bump_seen_many({
            "a@x.fr": (maintenant + timedelta(days=3)).isoformat(),
            "b@x.fr": (maintenant - timedelta(days=1)).isoformat(),
        })
        vus = kv.bump_seen()
        assert set(vus) == {"a@x.fr", "b@x.fr"}
        # La memoire ne dit pas « vu le X » mais « a revoir le Y ».
        def plus_tard(em):
            return datetime.fromisoformat(vus[em]) > maintenant
        assert plus_tard("a@x.fr") and not plus_tard("b@x.fr")
        # L'ecriture se fait en UN appel : le budget est en secondes.
        assert "mark_bump_seen_many(vus_ce_passage)" in BUMPS
    finally:
        kv._cmd, kv.kv_available = vrai, dispo


def test_le_plafond_est_journalier_et_non_par_passage():
    """C'est ce qui rend sur d'appeler /api/bumps toutes les heures : dix
    passages dans la journee ne peuvent pas envoyer dix fois le plafond."""
    kv, _cmd, _ = _faux_kv()
    vrai, dispo = kv._cmd, kv.kv_available
    kv._cmd, kv.kv_available = _cmd, (lambda: True)
    try:
        assert kv.bump_sent_today("2026-11-19") == 0
        for _ in range(7):
            kv.add_bump_sent("2026-11-19")
        assert kv.bump_sent_today("2026-11-19") == 7
        # Le compteur est remis a zero par le changement de jour, pas efface.
        assert kv.bump_sent_today("2026-11-20") == 0
    finally:
        kv._cmd, kv.kv_available = vrai, dispo


def test_le_plafond_garde_les_deux_chemins_d_envoi():
    """Il y a deux endroits qui envoient : les rendez-vous promis et la cadence
    J+2/J+5. Un plafond pose sur un seul des deux ne protege rien."""
    assert BUMPS.count("plafond and not dry_run") >= 3, BUMPS.count("plafond")
    i_agenda = BUMPS.index("for rdv in dues:")
    i_cadence = BUMPS.index("for reply in _flux:")
    assert "plafond" in BUMPS[i_agenda:i_cadence]
    assert "plafond" in BUMPS[i_cadence:]


def test_l_identifiant_de_fiche_est_mis_en_cache():
    """Deux requetes par prospect, c'etait la moitie du probleme. En cache,
    la recherche de fiche n'en coute plus qu'une."""
    kv, _cmd, _ = _faux_kv()
    vrai, dispo = kv._cmd, kv.kv_available
    kv._cmd, kv.kv_available = _cmd, (lambda: True)
    try:
        assert kv.cached_prospect_id("Z@X.FR") is None
        kv.cache_prospect_id("Z@X.FR", 1234)
        assert kv.cached_prospect_id("z@x.fr") == 1234
    finally:
        kv._cmd, kv.kv_available = vrai, dispo
    assert "cached_prospect_id(email)" in BUMPS
    assert "cache_prospect_id(email, prospect.prospect_id)" in BUMPS


def test_le_passage_laisse_une_trace():
    kv, _cmd, _ = _faux_kv()
    vrai, dispo = kv._cmd, kv.kv_available
    kv._cmd, kv.kv_available = _cmd, (lambda: True)
    try:
        assert kv.get_bump_run() == {}
        kv.set_bump_run({"at": "2026-11-19T10:00:00+00:00", "examines": 38,
                         "envoyees": 12, "du_jour": 12, "plafond": 25})
        t = kv.get_bump_run()
        assert t["examines"] == 38 and t["envoyees"] == 12
    finally:
        kv._cmd, kv.kv_available = vrai, dispo
    assert "set_bump_run(" in BUMPS


def test_rien_n_est_ecrit_en_simulation():
    """Un dry-run doit pouvoir etre lance a tout moment sans consommer le
    plafond du jour ni marquer les prospects comme examines."""
    i = BUMPS.index("if not dry_run:\n        try:\n            kvstore.mark_bump_seen_many")
    assert i > 0


def test_le_tableau_de_bord_dit_si_le_moteur_tourne():
    idx = (ROOT / "api" / "index.py").read_text(encoding="utf-8")
    assert "get_bump_run()" in idx
    assert "moteur de relance" in idx
    # Et il crie quand rien ne tourne, au lieu de n'afficher rien du tout.
    assert "aucun passage enregistr" in idx
    assert "le cron ne tourne plus" in idx


def test_chaque_issue_fixe_sa_propre_echeance():
    """Toute l'economie est la : un fil ou le prospect a parle en dernier, ou
    deja relance trois fois, ne doit pas couter un appel API par jour. Apres un
    premier tour, le quota ne sert plus qu'aux prospects reellement dus."""
    assert "_revoir_dans(7)" in BUMPS    # le prospect a parle en dernier
    assert "_revoir_dans(90)" in BUMPS   # trois messages sans reponse : stop
    assert "_revoir_dans(30)" in BUMPS   # fiche introuvable
    assert "_seuil - _age" in BUMPS      # pas encore du : on revient a sa date


def test_un_429_n_emporte_plus_le_passage_entier():
    """Constate le 19/11 en simulation : le quota est celui du COMPTE, et
    /api/cron tourne toutes les 5 minutes sur le meme seau. Un 429 remontait
    jusqu'en haut et tuait le passage — tout le travail deja fait etait perdu,
    trace comprise, donc invisible."""
    assert "except ManyReachError" in BUMPS
    assert BUMPS.count('getattr(_e, "status", None) == 429') >= 3
    # Le piege : l'attribut s'appelle `status`, pas `status_code`.
    assert "status_code" not in BUMPS
    mr = (ROOT / "src" / "manyreach.py").read_text(encoding="utf-8")
    assert "self.status = status" in mr
    # La lecture de la liste elle-meme est gardee, sinon pas de trace du tout.
    assert "liste des reponses illisible" in BUMPS


def test_les_relances_sont_intercalees_dans_le_cron_existant():
    """Constate le 19/11 avec Rudy : la liste cron-job.org ne contenait QUE
    /api/cron et un job Grow Pulser. /api/bumps existait depuis des mois et
    personne ne l'appelait — d'ou 10 relances parties pour 149 dues. Exiger un
    second job, c'est exiger qu'on y repense."""
    cron = (ROOT / "api" / "cron.py").read_text(encoding="utf-8")
    assert "_bumps_due()" in cron and "import run_bumps" in cron
    # En DERNIER dans l'ordre de priorite : jamais au detriment des reponses.
    assert cron.index("_spaces_due()") < cron.index("elif _bumps_due():")
    assert cron.index("_rattrapage_due()") < cron.index("elif _bumps_due():")
    # Au plus une fois par heure, et jamais sans KV (le plafond vit la-bas).
    assert "BUMPS_EVERY_MIN" in cron
    _i = cron.index("def _bumps_due()")
    _b = cron[_i:_i + 900]
    assert "kvstore.kv_available()" in _b and "return False" in _b
    # sys.argv restaure : le passage principal des tours suivants en depend.
    _j = cron.index("elif _bumps_due():")
    _c = cron[_j:_j + 1600]
    assert "sys.argv = _argv" in _c
    # Un echec ne doit pas devenir une boucle : sans trace, le passage
    # resterait « du » et prendrait un tour toutes les 5 minutes.
    assert "except Exception as _be" in _c and "set_bump_run" in _c


def test_une_sortie_anticipee_laisse_quand_meme_une_trace():
    """Sinon /api/cron croit qu'un passage reste du et sacrifie un tour toutes
    les 5 minutes pour rien. La nuit, hors fenetre d'envoi, ca affamerait le
    traitement des reponses entrantes."""
    assert BUMPS.count("_trace(") >= 3
    i = BUMPS.index("Hors fen")
    assert "_trace(" in BUMPS[i:i + 200]
    j = BUMPS.index("Plafond du jour atteint (")
    assert "_trace(" in BUMPS[j:j + 200]
    # Rien en simulation.
    k = BUMPS.index("def _trace(")
    _t = BUMPS[k:k + 700]
    assert "if dry_run:" in _t and "return" in _t


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
