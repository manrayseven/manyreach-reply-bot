"""Relances (bumps) des prospects intéressés sans réponse.

Usage :
    py scripts/run_bumps.py                # dry-run (montre qui serait relancé)
    py scripts/run_bumps.py --no-dry-run   # envoie réellement les relances

Stateless : déduit l'état de relance du thread ManyReach (voir src/bumps.py).
Respecte la fenêtre d'envoi (heures ouvrées) et les créneaux Calendar.
"""
from __future__ import annotations

import argparse
import html as _html
from os import environ as _osenv
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src import relances  # noqa: E402
from src.bumps import build_promise_body  # noqa: E402
from src import kvstore  # noqa: E402
from src.manyreach import ManyReachClient, ManyReachError  # noqa: E402

BUMP_STATUSES = ("Interested", "MaybeLater")

_CITATION = re.compile(
    r"(?:\n\s*>|\nLe .{0,40}a écrit|\nOn .{0,40}wrote"
    r"|\nDe\s*:|\nFrom\s*:|\nEnvoyé de mon)", re.I)


def _extrait_reponse(corps: str) -> str:
    """Le texte utile du message, sans la citation de l'echange."""
    txt = re.sub(r"<[^>]+>", " ", corps or "")
    txt = _html.unescape(txt)
    coupe = _CITATION.split(txt, 1)[0]
    return " ".join((coupe if len(coupe.strip()) >= 8 else txt).split())[:300]



def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Relance les prospects intéressés sans réponse.")
    ap.add_argument("--no-dry-run", action="store_true", help="Envoie réellement les relances")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    dry_run = not args.no_dry_run

    settings = yaml.safe_load((PROJECT_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8-sig")) or {}
    send_cfg = settings.get("sending", {}) or {}

    # PLUS DE CRENEAUX CONCRETS DANS UNE RELANCE (Rudy 19/11) : « relancer mais
    # pas avec un horaire et jour precis, car tu n'as pas acces a mon
    # calendrier ». Le client Google Agenda ne sert donc plus ici — proposer
    # une heure que Rudy ne pourra pas tenir est pire que ne rien proposer.
    # On demande SES disponibilites a lui.
    now = datetime.now(timezone.utc)

    def send_window_open():
        hours = send_cfg.get("allowed_hours", [9, 19])
        days = send_cfg.get("allowed_weekdays", [0, 1, 2, 3, 4])
        # IMPORTANT : heure Paris, pas heure serveur (UTC sur Vercel).
        tz_name = (settings.get("calendar", {}) or {}).get("timezone", "Europe/Paris")
        try:
            from zoneinfo import ZoneInfo
            local = now.astimezone(ZoneInfo(tz_name))
        except Exception:
            local = now.astimezone()
        return local.weekday() in days and hours[0] <= local.hour < hours[1]

    def _trace(motif: str, examines: int = 0, envoyees: int = 0) -> None:
        """Toute sortie laisse une trace. Sans elle, /api/cron croirait qu'un
        passage reste du et sacrifierait un tour toutes les 5 minutes pour
        rien — la nuit, hors fenetre d'envoi, ca affamerait le traitement des
        reponses entrantes."""
        if dry_run:
            return
        try:
            _j = now.date().isoformat()
            kvstore.set_bump_run({
                "at": now.isoformat(), "motif": motif,
                "examines": examines, "envoyees": envoyees, "jour": _j,
                "du_jour": kvstore.bump_sent_today(_j),
                "plafond": int(_osenv.get("BUMPS_MAX_PER_DAY", "25")),
            })
        except Exception:  # noqa: BLE001
            pass

    print(f"=== Relances — {'DRY-RUN' if dry_run else 'LIVE'} — regles Rudy : rdv / proposition ===")
    if not dry_run and not send_window_open():
        print("Hors fenêtre d'envoi — on ne relance pas maintenant. Réessaie en heures ouvrées.")
        _trace("hors fenetre d'envoi")
        return 0

    import os as _os, time as _time
    # 50 s : /api/bumps a un timeout de 60 s (vercel.json).
    budget_s = float(_os.environ.get("BUMPS_BUDGET_SECONDS", "50"))
    start_ts = _time.time()

    # PLAFOND JOURNALIER (Rudy 19/11). Journalier et non par passage : c'est ce
    # qui rend sur d'appeler /api/bumps toutes les heures. Sans lui, resorber un
    # retard de 149 relances enverrait 149 emails d'un coup.
    jour = now.date().isoformat()
    plafond = int(_os.environ.get("BUMPS_MAX_PER_DAY", "25"))
    try:
        deja_aujourdhui = kvstore.bump_sent_today(jour)
    except Exception:  # noqa: BLE001
        deja_aujourdhui = 0
    if deja_aujourdhui >= plafond and not dry_run:
        print(f"Plafond du jour atteint ({deja_aujourdhui}/{plafond}) — stop.")
        _trace("plafond du jour atteint")
        return 0

    # MEMOIRE DE PASSAGE : qui a deja ete EXAMINE recemment. Sauter une adresse
    # ici ne coute AUCUN appel API ; c'est ce qui fait avancer le balayage au
    # lieu de rabacher les vingt plus recents (voir src/kvstore.py).
    rescan_j = float(_os.environ.get("BUMP_RESCAN_DAYS", "3"))
    # Une maquette n'est ni un rendez-vous ni une proposition chiffree :
    # elle ne figure pas dans les trois regles de Rudy. Relancee seulement
    # s'il l'active (MAQUETTES_AUTO=1).
    _maquettes_auto = _os.environ.get("MAQUETTES_AUTO", "0") == "1"
    try:
        deja_vus = kvstore.bump_seen()
    except Exception:  # noqa: BLE001
        deja_vus = {}

    def _a_revoir_plus_tard(em: str) -> bool:
        """Vrai si on sait deja qu'il est trop tot pour ce prospect.

        La memoire ne stocke pas « vu le X » mais « a revoir le Y » : un fil
        ou le prospect a parle en dernier, ou deja relance trois fois, ne
        merite pas un appel API par jour. C'est ce qui fait converger le
        balayage — apres un premier tour, le quota ne sert plus qu'aux
        prospects reellement dus.
        """
        iso = deja_vus.get((em or "").lower().strip())
        if not iso:
            return False
        try:
            quand = datetime.fromisoformat(iso)
        except ValueError:
            return False
        if quand.tzinfo is None:
            quand = quand.replace(tzinfo=timezone.utc)
        return quand > now

    def _revoir_dans(jours: float) -> str:
        return (now + timedelta(days=jours)).isoformat()

    vus_ce_passage: dict = {}
    n_examines = 0
    seen: set[str] = set()
    n_bumped = 0
    with ManyReachClient() as mr:
        # ── RENDEZ-VOUS DONNÉS PAR LES PROSPECTS EUX-MÊMES (Rudy 16/11) ──
        # « Je pense, 1er trimestre 2027. » Ces fils finissent par une réponse
        # du prospect : `decide_bump` refuse de les relancer, par construction.
        # On les traite donc AVANT, sur leur date à eux. Un rendez-vous est
        # consommé dans tous les cas — relancé, ou retiré parce qu'il n'a plus
        # lieu d'être. Sinon l'agenda se remplit de dettes qu'on ne solde pas.
        n_promesses = 0
        try:
            dues = kvstore.relances_dues(now.date().isoformat())
        except Exception as _e:  # noqa: BLE001
            dues = []
            print(f"  !! agenda de relance illisible : {_e}")
        for rdv in dues:
            if (_time.time() - start_ts) > budget_s:
                print("  >> Budget atteint — le reste des rendez-vous au prochain run")
                break
            # Le plafond du jour vaut aussi pour les rendez-vous promis : un
            # prospect ne doit pas recevoir d'email parce qu'un compteur a ete
            # oublie sur une branche.
            if (deja_aujourdhui + n_bumped) >= plafond and not dry_run:
                print(f"  >> Plafond du jour atteint ({plafond}) — rendez-vous reportes")
                break
            email = str(rdv.get("email") or "")
            if not email:
                continue
            seen.add(email)
            prospect = mr.find_prospect_by_email(email)
            if not prospect:
                print(f"- {email} : fiche introuvable — rendez-vous retire")
                if not dry_run:
                    kvstore.drop_relance(email)
                continue
            # Statut terminal : meme lecture qu'ailleurs dans le bot (sendingStatus
            # pour Unsub/Hostile, confirmedStatus pour Won/NotInterested).
            statut = str(prospect.sending_status or "")
            confirme = str((prospect.raw or {}).get("confirmedStatus") or "")
            if statut in ("Unsub", "Hostile", "Bounced") or confirme in ("NotInterested", "Won"):
                statut = statut or confirme
                print(f"- {email} : statut {statut} — rendez-vous retire, aucune relance")
                if not dry_run:
                    kvstore.drop_relance(email)
                continue
            try:
                thread = mr.get_prospect_thread(prospect.prospect_id)
            except ManyReachError as _e:
                if getattr(_e, "status", None) == 429:
                    print("  >> Quota ManyReach atteint — rendez-vous reportes")
                    break
                continue
            # A-t-il repris contact depuis ? Alors le rendez-vous est caduc :
            # c'est une conversation en cours, pas une promesse a tenir.
            pose = str(rdv.get("pose_le") or "")
            repris = False
            for m in thread or []:
                if m.type == "Reply" and pose and m.created_at.isoformat() > pose:
                    repris = True
                    break
            if repris:
                print(f"- {email} : a repondu depuis — rendez-vous retire")
                if not dry_run:
                    kvstore.drop_relance(email)
                continue
            last_reply = next(
                (m for m in sorted(thread or [], key=lambda x: x.created_at, reverse=True)
                 if m.type == "Reply"), None)
            if not last_reply:
                if not dry_run:
                    kvstore.drop_relance(email)
                continue
            body = build_promise_body(prospect, str(rdv.get("le") or ""), str(rdv.get("dit") or ""))
            print(f"- {email} → relance promise ({rdv.get('le')} : « {str(rdv.get('dit'))[:60]} »)")
            if dry_run:
                print(f"    [DRY-RUN] enverrait : {body[:140]}...")
            else:
                mr.send_reply(message_id=last_reply.message_id, body_html=body, send_as_reply=True)
                kvstore.drop_relance(email)
                print("    [ENVOYÉ ✓] relance promise")
            n_promesses += 1
            n_bumped += 1
        if n_promesses:
            print(f"  ({n_promesses} rendez-vous donnes par les prospects eux-memes)")

        # UN FLUX PAR STATUT (Rudy 19/11). Le statut n'est PAS sur la fiche
        # prospect — verifie le meme jour : `confirmedStatus` est absent du
        # payload, mon releve les renvoyait tous a None. On ne le connait que
        # par le FEED d'ou la reponse provient. On interroge donc chaque statut
        # separement, au lieu de tout fusionner comme avant.
        _flux: list = []
        for _st in relances.STATUTS_RELANCABLES:
            try:
                for _m in mr.list_replies(confirmed_statuses=(_st,),
                                          max_per_status=args.limit):
                    _flux.append((_st, _m))
            except ManyReachError as _e:
                print(f"  !! statut {_st} illisible ({str(_e)[:70]})")
                if getattr(_e, "status", None) == 429:
                    break
        for _statut, reply in _flux:
            # Budget de temps : on s'arrête avant le timeout Vercel (60s). Le reste
            # sera relancé au prochain run quotidien (la cadence J+2/J+5 tolère ça).
            if (_time.time() - start_ts) > budget_s:
                print(f"  >> Budget {budget_s}s atteint — stop, suite au prochain run quotidien")
                break
            if (deja_aujourdhui + n_bumped) >= plafond and not dry_run:
                print(f"  >> Plafond du jour atteint ({plafond}) — suite au prochain passage")
                break
            email = reply.from_email
            if email in seen:
                continue
            seen.add(email)
            # Saute SANS appel API : c'est tout l'interet de la memoire.
            if _a_revoir_plus_tard(email):
                continue
            _cle = (email or "").lower().strip()
            # Par defaut on redemande demain ; chaque issue ci-dessous ajuste.
            vus_ce_passage[_cle] = _revoir_dans(rescan_j)
            n_examines += 1
            # Un identifiant de fiche en cache = une requete economisee, donc
            # deux fois plus de prospects examines dans le meme budget.
            prospect = None
            try:
                _pid = kvstore.cached_prospect_id(email)
            except Exception:  # noqa: BLE001
                _pid = None
            if _pid:
                try:
                    prospect = mr.get_prospect(_pid)
                except Exception:  # noqa: BLE001
                    prospect = None
            if not prospect:
                try:
                    prospect = mr.find_prospect_by_email(email)
                except ManyReachError as _e:
                    if getattr(_e, "status", None) == 429:
                        print("  >> Quota ManyReach atteint — arret propre")
                        break
                    continue
                if prospect:
                    try:
                        kvstore.cache_prospect_id(email, prospect.prospect_id)
                    except Exception:  # noqa: BLE001
                        pass
            if not prospect:
                vus_ce_passage[_cle] = _revoir_dans(30)
                continue
            # LE QUOTA EST CELUI DU COMPTE, PAS DU PROCESSUS (constate le 19/11
            # en simulation). /api/cron tourne toutes les 5 minutes et puise au
            # meme seau de 60 requetes/minute : le regulateur interne ne peut
            # pas l'anticiper. Avant, un 429 remontait jusqu'en haut et TUAIT
            # le passage — on perdait tout le travail deja fait, trace comprise.
            # Desormais on s'arrete proprement : la memoire de passage garde ce
            # qui a ete examine, et le passage suivant reprend plus loin.
            try:
                thread = mr.get_prospect_thread(prospect.prospect_id)
            except ManyReachError as _e:
                if getattr(_e, "status", None) == 429:
                    print("  >> Quota ManyReach atteint (le compte est partage "
                          "avec /api/cron) — arret propre, suite au prochain passage")
                    break
                print(f"  !! {email} : fil illisible ({str(_e)[:90]})")
                continue
            # LES REGLES DE RUDY (src/relances.py), et non plus une cadence
            # aveugle J+2/J+5 pour tout le monde. Un « Interested » a qui l'on
            # n'a rien propose n'est PAS relance : il le traite a la main.
            ctx = relances.lire_contexte(thread, now)
            decision = relances.decider(_statut, ctx, now,
                                        maquettes_actives=_maquettes_auto)
            vus_ce_passage[_cle] = _revoir_dans(decision.revoir_dans or rescan_j)

            # LE PROSPECT A PARLE EN DERNIER : ce n'est pas une relance qu'il
            # attend, c'est une REPONSE. Le moteur est le seul a passer par la,
            # alors il le note pour le tableau de bord.
            if ctx is not None and not dry_run:
                try:
                    if not ctx.dernier_est_de_nous:
                        _age_r = ctx.jours_depuis_dernier
                        if _age_r >= float(_os.environ.get("ATTENTE_MIN_JOURS", "3")):
                            _der = sorted(thread, key=lambda m: m.created_at)[-1]
                            kvstore.set_attente(email, {
                                "depuis": _der.created_at.isoformat(),
                                "jours": round(_age_r),
                                "extrait": _extrait_reponse(_der.body),
                                "pid": prospect.prospect_id,
                                "campaign_id": _der.campaign_id,
                                "message_id": _der.message_id,
                            })
                        else:
                            kvstore.drop_attente(email)
                    else:
                        # On a reparle depuis : l'attente est soldee.
                        kvstore.drop_attente(email)
                except Exception:  # noqa: BLE001
                    pass

            if not decision.relancer:
                # En simulation on dit POURQUOI : sans ca, un essai a blanc qui
                # renvoie zero ne distingue pas « rien a faire » d'une regle
                # trop stricte qui ecarte tout le monde en silence.
                if dry_run:
                    print(f"  . {email} [{_statut}] : {decision.raison}")
                continue
            body = relances.corps(decision.motif, decision.numero, prospect, ctx)
            # On repond au dernier message du prospect dans le fil.
            last_reply = next(
                (m for m in sorted(thread, key=lambda x: x.created_at, reverse=True)
                 if m.type == "Reply"), None)
            if not last_reply:
                continue
            print(f"- {email} [{_statut}] → relance {decision.motif} "
                  f"#{decision.numero} ({decision.raison})")
            if dry_run:
                print(f"    [DRY-RUN] enverrait : {body[:130]}...")
            else:
                try:
                    mr.send_reply(message_id=last_reply.message_id,
                                  body_html=body, send_as_reply=True)
                except ManyReachError as _e:
                    print(f"  !! {email} : envoi refuse ({str(_e)[:90]})")
                    if getattr(_e, "status", None) == 429:
                        break
                    continue
                try:
                    kvstore.add_bump_sent(jour)
                except Exception:  # noqa: BLE001
                    pass
                print(f"    [ENVOYÉ ✓] relance {decision.motif} #{decision.numero}")
            # Deuxieme relance = la derniere : on n'y revient plus.
            vus_ce_passage[_cle] = _revoir_dans(
                8.0 if decision.numero == 1 else 90.0)
            n_bumped += 1

    # TRACE (Rudy 19/11) : sans elle, impossible de savoir si les relances
    # tournent. C'est ce silence qui a laisse le moteur affame des semaines.
    if not dry_run:
        try:
            kvstore.mark_bump_seen_many(vus_ce_passage)
            kvstore.set_bump_run({
                "at": now.isoformat(),
                "examines": n_examines,
                "envoyees": n_bumped,
                "promesses": n_promesses,
                "jour": jour,
                "du_jour": kvstore.bump_sent_today(jour),
                "plafond": plafond,
            })
        except Exception as _e:  # noqa: BLE001
            print(f"  !! trace non ecrite : {_e}")
    print(f"{n_examines} prospect(s) examine(s), "
          f"{n_bumped} relance(s) {'simulee(s)' if dry_run else 'envoyee(s)'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
