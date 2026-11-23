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

from src import agenda, relances  # noqa: E402
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

    # Une seule attente de quota par passage : deux couteraient le budget.
    _quota_attendu = [False]
    vus_ce_passage: dict = {}
    n_examines = 0
    seen: set[str] = set()
    n_bumped = 0
    # Le modele n'est sollicite que si Rudy a ecrit depuis la programmation
    # d'une relance : quelques appels par jour au plus. Cree a la demande.
    _client_ia: list = []

    def _modele_agenda(systeme: str, utilisateur: str) -> str:
        import anthropic  # noqa: PLC0415
        from src.llm_compat import HAIKU, options_for, text_of  # noqa: PLC0415
        if not _client_ia:
            _client_ia.append(anthropic.Anthropic(max_retries=2))
        msg = _client_ia[0].messages.create(
            model=HAIKU, max_tokens=300, **options_for(HAIKU),
            system=systeme, messages=[{"role": "user", "content": utilisateur}])
        return text_of(msg)

    with ManyReachClient() as mr:
        # ── RENDEZ-VOUS DONNÉS PAR LES PROSPECTS EUX-MÊMES (Rudy 16/11) ──
        # « Je pense, 1er trimestre 2027. » Ces fils finissent par une réponse
        # du prospect : `decide_bump` refuse de les relancer, par construction.
        # On les traite donc AVANT, sur leur date à eux. Un rendez-vous est
        # consommé dans tous les cas — relancé, ou retiré parce qu'il n'a plus
        # lieu d'être. Sinon l'agenda se remplit de dettes qu'on ne solde pas.
        n_promesses = 0
        n_annulees = 0
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415
            _jour_ouvre = now.astimezone(ZoneInfo("Europe/Paris")).weekday() < 5
        except Exception:  # noqa: BLE001
            _jour_ouvre = now.weekday() < 5
        aujourdhui = now.date().isoformat()
        try:
            agenda_complet = kvstore.relances()
        except Exception as _e:  # noqa: BLE001
            agenda_complet = []
            print(f"  !! agenda de relance illisible : {_e}")

        # REVERIFIER, PAS SEULEMENT ENVOYER (Rudy 23/11) : « tu dois regarder
        # regulierement si je n'ai pas repondu entre-temps a ces gens ». Avant,
        # on ne lisait le fil qu'a l'echeance, et seulement pour voir si le
        # PROSPECT avait reecrit. Un rendez-vous cale a la main par Rudy
        # n'empechait donc pas la relance de partir. Desormais chaque entree est
        # relue : toujours juste avant un envoi, et toutes les six heures sinon.
        def _a_relire(r: dict) -> bool:
            if str(r.get("le", "9999")) <= aujourdhui:
                return True
            v = agenda.depuis_iso(r.get("verifie_le"))
            return v is None or (now - v).total_seconds() > 6 * 3600

        a_voir = sorted((r for r in agenda_complet if _a_relire(r)),
                        key=lambda r: (str(r.get("le", "9999")) > aujourdhui,
                                       str(r.get("verifie_le") or "")))
        for rdv in a_voir:
            if (_time.time() - start_ts) > budget_s:
                print("  >> Budget atteint — le reste de l'agenda au prochain passage")
                break
            email = str(rdv.get("email") or "")
            if not email:
                continue
            seen.add(email)
            due = str(rdv.get("le", "9999")) <= aujourdhui
            # L'identifiant de fiche est note a la programmation : un appel de
            # moins. Les entrees ajoutees a la main ne l'ont pas.
            prospect = None
            try:
                _pid = rdv.get("prospect_id")
                prospect = mr.get_prospect(int(_pid)) if _pid else mr.find_prospect_by_email(email)
            except ManyReachError as _e:
                if getattr(_e, "status", None) == 429:
                    print("  >> Quota ManyReach atteint — agenda reporte")
                    break
                prospect = None
            except Exception:  # noqa: BLE001
                prospect = None
            if not prospect:
                if due:
                    print(f"- {email} : fiche introuvable — relance programmee retiree")
                    if not dry_run:
                        kvstore.drop_relance(email)
                continue
            # Statut terminal : meme lecture qu'ailleurs dans le bot.
            statut = str(prospect.sending_status or "")
            confirme = str((prospect.raw or {}).get("confirmedStatus") or "")
            if statut in ("Unsub", "Hostile", "Bounced") or confirme in ("NotInterested", "Won"):
                print(f"- {email} : statut {statut or confirme} — relance programmee retiree")
                if not dry_run:
                    kvstore.drop_relance(email)
                n_annulees += 1
                continue
            try:
                thread = mr.get_prospect_thread(prospect.prospect_id)
            except ManyReachError as _e:
                if getattr(_e, "status", None) == 429:
                    print("  >> Quota ManyReach atteint — agenda reporte")
                    break
                continue

            verdict = agenda.juger(thread, rdv, appeler_modele=_modele_agenda)
            if verdict.decision == agenda.ANNULER:
                print(f"- {email} : relance du {rdv.get('le')} ANNULEE — {verdict.raison}")
                if not dry_run:
                    kvstore.drop_relance(email)
                    try:
                        kvstore.log_action({
                            "at": now.isoformat(), "from": email,
                            "subject": "Relance programmee annulee",
                            "intent": "manual_reply",
                            "status": f"Relance du {rdv.get('le')} annulee : {verdict.raison}",
                            "reply": str(rdv.get("dit") or ""), "response": "",
                        })
                    except Exception:  # noqa: BLE001
                        pass
                n_annulees += 1
                continue

            # On garde : on note quand et sur quoi, pour ne pas rejuger chaque heure.
            if not dry_run:
                try:
                    kvstore.maj_relance(email, {
                        "verifie_le": now.isoformat(),
                        "juge_sur": verdict.juge_sur,
                        "verdict": verdict.decision,
                        "raison_verdict": verdict.raison,
                    })
                except Exception:  # noqa: BLE001
                    pass

            if verdict.decision == agenda.INCONNU:
                # Rudy a ecrit depuis et on n'a pas su lire : on ne supprime
                # rien, et surtout on n'ENVOIE rien a l'aveugle.
                print(f"- {email} : {verdict.raison} — relance RETENUE")
                continue
            if not due:
                continue

            # JOURS OUVRES SEULEMENT, comme les relances de Lead Finder. Le
            # doublon du 19/11 a envoye 54 relances un samedi et un dimanche :
            # la fenetre d'envoi du bot (7 j/7) est faite pour REPONDRE vite,
            # pas pour relancer. Une echeance qui tombe un week-end attend lundi.
            if not _jour_ouvre:
                continue
            # L'echeance est la, et rien ne s'y oppose : on tient parole.
            if (deja_aujourdhui + n_bumped) >= plafond and not dry_run:
                print(f"  >> Plafond du jour atteint ({plafond}) — relances programmees reportees")
                break
            last_reply = next(
                (m for m in sorted(thread or [], key=lambda x: x.created_at, reverse=True)
                 if m.type == "Reply"), None)
            if not last_reply:
                if not dry_run:
                    kvstore.drop_relance(email)
                continue
            body = build_promise_body(prospect, str(rdv.get("le") or ""), str(rdv.get("dit") or ""))
            print(f"- {email} -> relance promise ({rdv.get('le')} : « {str(rdv.get('dit'))[:60]} »)")
            if dry_run:
                print(f"    [DRY-RUN] enverrait : {body[:140]}...")
            else:
                try:
                    mr.send_reply(message_id=last_reply.message_id, body_html=body, send_as_reply=True)
                except ManyReachError as _e:
                    print(f"  !! {email} : envoi refuse ({str(_e)[:90]})")
                    if getattr(_e, "status", None) == 429:
                        break
                    continue
                kvstore.drop_relance(email)
                print("    [ENVOYE] relance promise")
            n_promesses += 1
            n_bumped += 1
        if n_annulees:
            print(f"  ({n_annulees} relance(s) programmee(s) annulee(s) apres relecture du fil)")
        if n_promesses:
            print(f"  ({n_promesses} rendez-vous donnes par les prospects eux-memes)")

        # ── CE MOTEUR FAIT DOUBLON, ET J'AI MIS QUATRE JOURS A LE VOIR ──
        #
        # Lead Finder relance DEJA les prospects par stade, depuis le serveur
        # Oracle, tous les jours ouvres a 9 h 30 (serveur/crontab.txt), avec les
        # memes statuts, les memes cadences (Interested 3 j puis 7, MeetingBooked
        # 4 j, MeetingCompleted 7 j), le meme plafond de deux relances et la meme
        # regle du dernier message. Repare le 20/11 : plafond porte a 250, 453
        # relances en retard resorbees.
        #
        # Le 19/11 j'ai reconstruit tout cela ici, sans verifier. Les deux
        # moteurs visent le MEME compte ManyReach : s'ils tournaient ensemble,
        # chaque prospect recevrait deux relances. Rudy l'a dit en une phrase le
        # 23/11 — « mais normalement c'etait deja comme ca ».
        #
        # La cadence par stade appartient donc a Lead Finder. Ce passage ne garde
        # que ce que Lead Finder ne fait PAS : les rendez-vous que le prospect
        # donne lui-meme (« je pense, 1er trimestre 2027 »), traites plus haut
        # depuis l'agenda. Mettre RELANCES_CADENCE=1 reactive la cadence ici, et
        # il faudrait alors couper celle du serveur.
        _cadence_ici = _os.environ.get("RELANCES_CADENCE", "0") == "1"
        if not _cadence_ici:
            print("  (cadence par stade : assuree par Lead Finder sur le serveur)")

        # UN FLUX PAR STATUT (Rudy 19/11). Le statut n'est PAS sur la fiche
        # prospect — verifie le meme jour : `confirmedStatus` est absent du
        # payload, mon releve les renvoyait tous a None. On ne le connait que
        # par le FEED d'ou la reponse provient. On interroge donc chaque statut
        # separement, au lieu de tout fusionner comme avant.
        _flux: list = []
        for _st in (relances.STATUTS_RELANCABLES if _cadence_ici else ()):
            try:
                for _m in mr.list_replies(confirmed_statuses=(_st,),
                                          max_per_status=args.limit):
                    _flux.append((_st, _m))
            except ManyReachError as _e:
                print(f"  !! statut {_st} illisible ({str(_e)[:70]})")
                if getattr(_e, "status", None) == 429:
                    break
        # LE PLUS ANCIEN D'ABORD (Rudy 19/11, apres un premier passage a vide).
        # `list_replies` trie du plus RECENT au plus ancien : c'est ce que veut
        # le traitement des reponses entrantes, l'inverse de ce que veut une
        # relance. Les prospects dus sont les VIEUX ; en commencant par les
        # frais, le moteur depensait tout son quota sur des gens qu'il est trop
        # tot pour relancer et n'atteignait les autres qu'apres une quinzaine
        # de passages. Une ligne de tri, et le premier passage sert a quelque
        # chose.
        _flux.sort(key=lambda sm: sm[1].created_at)
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
                    # S'ARRETER AU PREMIER 429 COUTAIT TOUTE L'HEURE (constate
                    # le 19/11 : trois prospects cibles n'avaient toujours rien
                    # recu). Le quota se libere par fenetre glissante d'une
                    # minute : si le budget le permet, on attend et on reprend.
                    _reste = budget_s - (_time.time() - start_ts)
                    if _reste > 25 and not _quota_attendu[0]:
                        _quota_attendu[0] = True
                        print("  >> Quota atteint — on patiente 20 s et on reprend")
                        _time.sleep(20)
                        try:
                            thread = mr.get_prospect_thread(prospect.prospect_id)
                        except ManyReachError:
                            print("  >> Quota toujours sature — arret propre")
                            break
                    else:
                        print("  >> Quota ManyReach atteint (compte partage avec "
                              "/api/cron) — arret propre, suite au prochain passage")
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

            # LA LISTE « ILS ATTENDENT TA REPONSE » EST SUPPRIMEE (Rudy
            # 23/11) : « je ne vois pas a quoi ca sert ». Il avait raison — sa
            # liste de 33 contenait un rebond OVH, deux repondeurs d'absence,
            # un « merci pour la mise en contact » et deux confirmations de
            # rendez-vous. Sa regle tient en une phrase : soit le contact doit
            # etre relance et on le fait, soit c'est termine et on n'y revient
            # pas. Plus de troisieme pile a trier a la main. C'est desormais
            # Lead Finder qui tranche (scripts/relancer-engagements.ts).
            if ctx is not None and not dry_run:
                try:
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
