# -*- coding: utf-8 -*-
"""Une relance programmee tient-elle encore ? (Rudy, 23/11)

    « Tu dois regarder regulierement si je n'ai pas repondu entre-temps a ces
      gens (et si du coup la relance doit toujours etre celle-la, ou si
      carrement il doit y avoir relance). Par exemple [un prospect] : je lui ai
      repondu tout a l'heure et on a bloque un rdv. »

Avant, l'agenda ne regardait qu'une chose a l'echeance : le PROSPECT a-t-il
reecrit ? Ce que Rudy avait fait depuis etait invisible. Un rendez-vous cale a
la main n'empechait pas la relance de partir, en citant une phrase caduque.

Lance sans pytest :  python tests/test_agenda.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from src import agenda  # noqa: E402

T0 = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)


@dataclass
class M:
    type: str
    created_at: datetime
    body: str = ""
    message_id: str = "m"


def _entree(**k):
    e = {"email": "p@x.fr", "le": "2026-12-07", "dit": "Je rentre debut decembre",
         "pose_le": (T0 + timedelta(minutes=5)).isoformat()}
    e.update(k)
    return e


FIL = [M("Sent", T0 - timedelta(days=3), "Bonjour, ..."),
       M("Reply", T0, "Je rentre en France debut decembre.", "r1")]


def test_personne_n_a_ecrit_depuis_on_garde():
    v = agenda.juger(FIL, _entree(), appeler_modele=None)
    assert v.decision == agenda.GARDER


def test_le_prospect_a_reecrit_on_annule_sans_modele():
    fil = FIL + [M("Reply", T0 + timedelta(days=1), "Finalement appelez-moi demain", "r2")]
    appels = []
    v = agenda.juger(fil, _entree(), appeler_modele=lambda s, u: appels.append(1) or "{}")
    assert v.decision == agenda.ANNULER and not appels


def test_rudy_a_cale_un_rendez_vous_on_annule():
    """Le cas montre par Rudy : il a repondu, un rendez-vous est bloque."""
    fil = FIL + [M("SentManual", T0 + timedelta(days=2),
                   "Parfait, je vous appelle lundi 7 a 10h.", "s9")]
    recu = {}

    def modele(systeme, utilisateur):
        recu["s"], recu["u"] = systeme, utilisateur
        return '{"decision": "annuler", "raison": "rendez-vous fixe lundi 7"}'

    v = agenda.juger(fil, _entree(), appeler_modele=modele)
    assert v.decision == agenda.ANNULER and v.juge_sur == "s9"
    # Le modele voit ce que Rudy a ecrit ET ce que le prospect avait dit.
    assert "je vous appelle lundi 7" in recu["u"]
    assert "Je rentre debut decembre" in recu["u"]
    assert "RUDY" in recu["u"] and "LE PROSPECT" in recu["u"]


def test_un_simple_accuse_de_reception_laisse_la_relance():
    fil = FIL + [M("SentManual", T0 + timedelta(days=1), "Bien note, bonnes fetes.", "s2")]
    v = agenda.juger(fil, _entree(),
                     appeler_modele=lambda s, u: '```json\n{"decision":"garder","raison":"accuse"}\n```')
    assert v.decision == agenda.GARDER and v.juge_sur == "s2"


def test_un_e_mail_de_campagne_ne_compte_pas_comme_rudy():
    """Une sequence automatique n'a que le type « Sent » : ce n'est pas Rudy
    qui reprend la main, et il ne faut pas payer un jugement pour ca."""
    fil = FIL + [M("Sent", T0 + timedelta(days=2), "Relance de sequence", "c1")]
    appels = []
    v = agenda.juger(fil, _entree(), appeler_modele=lambda s, u: appels.append(1) or "{}")
    assert v.decision == agenda.GARDER and not appels


def test_si_le_modele_echoue_on_ne_decide_rien():
    """Ni suppression ni envoi a l'aveugle : la relance est RETENUE."""
    fil = FIL + [M("SentManual", T0 + timedelta(days=1), "...", "s3")]

    def casse(s, u):
        raise RuntimeError("529 overloaded")

    assert agenda.juger(fil, _entree(), appeler_modele=casse).decision == agenda.INCONNU
    assert agenda.juger(fil, _entree(), appeler_modele=lambda s, u: "pas du json").decision == agenda.INCONNU
    assert agenda.juger(fil, _entree(), appeler_modele=None).decision == agenda.INCONNU


def test_on_ne_rejuge_pas_le_meme_message_chaque_heure():
    fil = FIL + [M("SentManual", T0 + timedelta(days=1), "Bien note.", "s4")]
    appels = []
    e = _entree(juge_sur="s4", verdict="garder", raison_verdict="accuse")
    v = agenda.juger(fil, e, appeler_modele=lambda s, u: appels.append(1) or "{}")
    assert v.decision == agenda.GARDER and not appels
    # Rudy reecrit : nouvel identifiant, la question se repose.
    fil2 = fil + [M("SentManual", T0 + timedelta(days=2), "RDV jeudi 14h ?", "s5")]
    agenda.juger(fil2, e, appeler_modele=lambda s, u: appels.append(1) or '{"decision":"annuler","raison":"rdv"}')
    assert appels == [1]


def test_le_moteur_retient_au_lieu_d_envoyer_a_l_aveugle():
    src = (ROOT / "scripts" / "run_bumps.py").read_text(encoding="utf-8")
    assert "agenda.juger(thread, rdv, appeler_modele=_modele_agenda)" in src
    i = src.index("if verdict.decision == agenda.INCONNU:")
    j = src.index("mr.send_reply(message_id=last_reply.message_id, body_html=body", i)
    assert "continue" in src[i:j], "INCONNU doit sortir avant l'envoi"
    # Toute l'agenda est relue, pas seulement les echeances du jour.
    assert "kvstore.relances()" in src and "_a_relire" in src


def test_la_mise_a_jour_ne_deplace_jamais_pose_le():
    """`pose_le` dit a partir de quand un message compte comme « ecrit
    depuis ». Le reecrire a chaque verification rendrait invisible ce que Rudy
    a repondu entre-temps — exactement le bug qu'on corrige."""
    from src import kvstore  # noqa: PLC0415
    import json  # noqa: PLC0415

    base = {}

    def _cmd(*a):
        if a[0] == "HGET":
            return base.get(a[2])
        if a[0] == "HSET":
            base[a[2]] = a[3]
            return 1
        return None

    vrai, dispo = kvstore._cmd, kvstore.kv_available
    kvstore._cmd, kvstore.kv_available = _cmd, (lambda: True)
    try:
        base["p@x.fr"] = json.dumps({"email": "p@x.fr", "le": "2026-12-07",
                                     "dit": "debut decembre", "pose_le": "2026-11-20T10:05:00+00:00"})
        kvstore.maj_relance("P@x.fr", {"verifie_le": "2026-11-23T19:00:00+00:00",
                                       "pose_le": "2099-01-01", "le": "2099-01-01"})
        rec = json.loads(base["p@x.fr"])
        assert rec["pose_le"] == "2026-11-20T10:05:00+00:00" and rec["le"] == "2026-12-07"
        assert rec["verifie_le"].startswith("2026-11-23")
    finally:
        kvstore._cmd, kvstore.kv_available = vrai, dispo


def test_une_relance_programmee_ne_part_pas_le_week_end():
    """Le doublon du 19/11 a envoye 54 relances un samedi et un dimanche. La
    fenetre d'envoi du bot (7 j/7) sert a REPONDRE vite, pas a relancer."""
    src = (ROOT / "scripts" / "run_bumps.py").read_text(encoding="utf-8")
    i = src.index("if not _jour_ouvre:")
    j = src.index("mr.send_reply(message_id=last_reply.message_id, body_html=body", i)
    assert "continue" in src[i:j]
    assert 'weekday() < 5' in src


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
