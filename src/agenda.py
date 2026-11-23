# -*- coding: utf-8 -*-
"""Une relance programmée est-elle encore d'actualité ? (Rudy, 23/11)

    « OK pour noter ici les relances programmées, par contre tu dois regarder
      régulièrement si je n'ai pas répondu entre-temps à ces gens (et si du
      coup la relance doit toujours être celle-là, ou si carrément il doit y
      avoir relance). Par exemple contactnuadthai04 : je lui ai répondu tout à
      l'heure et on a bloqué un rdv. »

Avant ce module, l'agenda ne regardait qu'une chose : le PROSPECT a-t-il
réécrit ? Il ignorait complètement ce que Rudy avait fait depuis. Un
rendez-vous calé à la main n'empêchait donc pas la relance de partir à la date
prévue, en citant une phrase devenue caduque.

Trois issues, dans cet ordre :

  1. Le prospect a réécrit depuis        -> ANNULER. Sa nouvelle réponse suit le
     circuit normal du bot, qui reprogramme s'il redonne une date.
  2. Personne n'a rien écrit depuis      -> GARDER. C'est le cas pour lequel
     l'agenda existe.
  3. Rudy a écrit depuis                 -> on LIT ce qu'il a écrit. Rendez-vous
     calé, ou Rudy annonce qu'il reviendra lui-même (consigne du 21/11 : « tu ne
     le relances plus automatiquement ») -> ANNULER. Simple accusé de réception
     qui ne change rien au moment convenu -> GARDER.

Le point 3 demande de comprendre une phrase : c'est le modèle qui tranche, pas
une liste de tournures. S'il ne répond pas, on ne décide RIEN : la relance est
gardée mais retenue, jamais envoyée à l'aveugle.
"""
from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

GARDER = "garder"
ANNULER = "annuler"
INCONNU = "inconnu"   # on n'a pas pu trancher : ni envoi ni suppression


@dataclass(frozen=True)
class Verdict:
    decision: str
    raison: str
    juge_sur: str = ""   # identifiant du dernier message de Rudy examiné


def _texte(corps: str, n: int = 700) -> str:
    t = _html.unescape(re.sub(r"<[^>]+>", " ", corps or ""))
    # On coupe la citation de l'échange précédent.
    for marq in ("\n>", " a écrit :", " wrote:", "-----Original"):
        i = t.find(marq)
        if i > 30:
            t = t[:i]
    return " ".join(t.split())[:n]


def depuis_iso(iso) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


CONSIGNE = """Tu aides Rudy Viard (Webmarketing Conseil) à tenir son agenda de relances.

Un prospect avait indiqué QUAND le recontacter. Une relance automatique a été
programmée pour cette date. Depuis, Rudy a écrit lui-même au prospect. Tu dois
dire si la relance programmée doit être GARDÉE ou ANNULÉE.

ANNULER si, dans ce que Rudy a écrit depuis :
- un rendez-vous a été fixé, proposé à une date précise ou confirmé ;
- Rudy annonce qu'il reviendra LUI-MÊME vers le prospect à un moment donné
  (« je reviens vers vous en janvier », « je vous rappelle lundi ») : il s'en
  charge à la main, aucune relance automatique ne doit partir ;
- la conversation a avancé au point que la relance citerait une situation
  dépassée (proposition envoyée, sujet clos, refus acté).

GARDER si le message de Rudy ne change rien au moment convenu : un simple
accusé de réception (« bien noté », « très bien, bonnes fêtes »), ou un
document envoyé « en attendant ».

Dans le doute, ANNULER : une relance automatique qui contredit ce que Rudy
vient d'écrire est pire qu'une relance absente — Rudy suit ce fil lui-même.

Réponds UNIQUEMENT par un objet JSON :
{"decision": "garder" | "annuler", "raison": "une phrase courte, en français"}"""


def juger(thread, entree: dict, appeler_modele=None) -> Verdict:
    """Dit si la relance programmée `entree` tient toujours, au vu du fil.

    `appeler_modele(systeme, utilisateur) -> str` n'est sollicité que si Rudy a
    écrit depuis la programmation. Sans lui (ou s'il échoue) : INCONNU.
    """
    pose = depuis_iso(entree.get("pose_le"))
    if pose is None or not thread:
        return Verdict(GARDER, "rien à comparer")
    ordre = sorted(thread, key=lambda m: m.created_at)
    apres = [m for m in ordre if m.created_at > pose]

    if any(m.type == "Reply" for m in apres):
        return Verdict(ANNULER, "le prospect a réécrit depuis")

    # « SentManual » = écrit à la main ou envoyé par l'API ; un e-mail de
    # séquence de campagne n'a que le type « Sent ».
    de_rudy = [m for m in apres if m.type == "SentManual"]
    if not de_rudy:
        return Verdict(GARDER, "personne n'a écrit depuis")

    dernier = de_rudy[-1]
    # Déjà jugé sur ce même message : inutile de redemander chaque heure.
    if entree.get("juge_sur") == dernier.message_id and entree.get("verdict") in (GARDER, ANNULER):
        return Verdict(entree["verdict"], str(entree.get("raison_verdict") or "déjà jugé"),
                       dernier.message_id)

    if appeler_modele is None:
        return Verdict(INCONNU, "Rudy a écrit depuis — à relire", dernier.message_id)

    avant = [m for m in ordre if m.created_at <= pose][-3:]
    lignes = []
    for m in avant + apres:
        qui = "LE PROSPECT" if m.type == "Reply" else (
            "RUDY (à la main)" if m.type == "SentManual" else "E-MAIL DE CAMPAGNE")
        lignes.append(f"[{m.created_at.date().isoformat()} — {qui}]\n{_texte(m.body)}")
    utilisateur = (
        f"Relance programmée pour le {entree.get('le')}.\n"
        f"Ce que le prospect avait dit : « {entree.get('dit') or '(non noté)'} »\n"
        f"Programmée le {pose.date().isoformat()}.\n\n"
        "Le fil, du plus ancien au plus récent :\n\n" + "\n\n---\n\n".join(lignes)
    )
    try:
        brut = (appeler_modele(CONSIGNE, utilisateur) or "").strip()
        brut = re.sub(r"^```(?:json)?\s*|\s*```$", "", brut)
        i, j = brut.find("{"), brut.rfind("}")
        data = json.loads(brut[i:j + 1])
        decision = str(data.get("decision", "")).strip().lower()
        if decision not in (GARDER, ANNULER):
            raise ValueError(f"décision illisible : {decision!r}")
        return Verdict(decision, str(data.get("raison") or "")[:200], dernier.message_id)
    except Exception as e:  # noqa: BLE001
        return Verdict(INCONNU, f"jugement impossible ({str(e)[:80]})", dernier.message_id)
