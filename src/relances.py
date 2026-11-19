# -*- coding: utf-8 -*-
"""Quelle relance pour qui — règles de Rudy (11/11, reprécisées le 19/11).

    « Il faut des réponses contextuelles pour chaque personne.

      Si la personne est Interested              -> je traite à la main.
      Si elle est Interested et s'est vue proposer un rendez-vous, mais n'a
         pas répondu OU n'est pas passée en Meeting Booked
                                                 -> relancer, SANS jour ni
                                                    horaire précis (le bot n'a
                                                    pas accès à mon agenda).
      Si elle est Meeting Booked ou Meeting Completed ET a reçu un email avec
         une proposition restée sans réponse     -> relancer sur la proposition.

      Ils doivent tous être traités automatiquement, y compris les anciens. »

Deux conséquences que le code doit respecter à la lettre :

1. Un « Interested » à qui l'on n'a RIEN proposé n'est jamais relancé par le
   bot. C'est la règle 1, et elle prime sur l'envie de remplir la file.
2. Un prospect « Won » n'est relancé nulle part (consigne du 11/11), et un
   « Meeting Completed » seulement si une proposition lui a été envoyée.

POURQUOI DES MOTS-CLÉS ICI, ALORS QU'ILS SONT PROSCRITS AILLEURS : les listes
ci-dessous ne lisent pas les prospects, elles lisent NOS PROPRES ENVOIS. Nous
écrivons ces messages, depuis nos propres modèles — le vocabulaire est connu et
fini. Courir après la formulation d'un inconnu est perdu d'avance ; reconnaître
sa propre écriture ne l'est pas.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from datetime import datetime

# ── Ce que NOUS avons envoyé ────────────────────────────────────────────────

# Une proposition de rendez-vous : on demande à se parler, ou on propose un
# créneau. Vocabulaire de nos modèles (training_examples.md) et des messages
# que Rudy écrit lui-même.
_RDV_PROPOSE = re.compile(
    r"(15\s*min|quinze minutes|en visio|un appel|vous appeler|se parler"
    r"|[êe]tes-vous disponible|seriez-vous disponible|vos disponibilit"
    r"|un cr[ée]neau|pouvons-nous (appeler|[ée]changer)|on se cale"
    r"|caler un (rendez|rdv|point)|prendre rendez-vous|je vous propose"
    r"|sur quel num[ée]ro|vous rappeler)", re.I)

# Une proposition commerciale : plaquettes, offre chiffrée, devis, formules.
_PROPOSITION = re.compile(
    r"(plaquette|proposition|devis|mon offre|notre offre|les deux approches"
    r"|voici ce que je propose|formule|prospection-externalisee"
    r"|systeme-prospection|syst[èe]me install[ée]|/prospection/|/cold-emailing/"
    r"|ci-joint|en pi[èe]ce jointe)", re.I)

# Une maquette de site : un lien vers un fichier HTML déposé sur WordPress.
_MAQUETTE = re.compile(r"wp-content/uploads/[^\s\"'<>]+\.html", re.I)

# Nos propres relances, pour les compter sans rien stocker. Le moteur est
# volontairement sans mémoire d'envoi : le fil ManyReach fait foi.
_DEJA_RELANCE = re.compile(
    r"(revenir vers vous rapidement|Dernier message de ma part"
    r"|Vous m'aviez dit de revenir vers vous|nous n'avons pas r[ée]ussi [àa] nous"
    r"|est-ce toujours d'actualit[ée]|je me permets de revenir)", re.I)


def _texte(corps: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", " ", corps or ""))


@dataclass(frozen=True)
class Contexte:
    """Ce que le fil dit, une fois lu une seule fois."""
    dernier_est_de_nous: bool
    jours_depuis_dernier: float
    rdv_propose: bool
    proposition_envoyee: bool
    maquette_envoyee: bool
    relances_deja: int
    envois_consecutifs: int
    date_proposition: datetime | None
    date_rdv_propose: datetime | None
    dernier_message_id: str | None
    dernier_reply_id: str | None


def lire_contexte(thread, maintenant: datetime) -> Contexte | None:
    """Lit le fil UNE fois et en tire tout ce dont les règles ont besoin."""
    if not thread:
        return None
    ordre = sorted(thread, key=lambda m: m.created_at)
    dernier = ordre[-1]
    nos_envois = [m for m in ordre if m.type in ("Sent", "SentManual")]
    reponses = [m for m in ordre if m.type == "Reply"]

    rdv = prop = maq = None
    relances = 0
    for m in nos_envois:
        t = _texte(m.body)
        if _DEJA_RELANCE.search(t):
            relances += 1
            # Une relance n'est pas une proposition : sinon la relance
            # s'auto-alimenterait, chaque envoi justifiant le suivant.
            continue
        if _RDV_PROPOSE.search(t):
            rdv = m.created_at
        if _PROPOSITION.search(t):
            prop = m.created_at
        if _MAQUETTE.search(m.body or ""):
            maq = m.created_at

    consecutifs = 0
    for m in reversed(ordre):
        if m.type in ("Sent", "SentManual"):
            consecutifs += 1
        else:
            break

    return Contexte(
        dernier_est_de_nous=dernier.type in ("Sent", "SentManual"),
        jours_depuis_dernier=(maintenant - dernier.created_at).total_seconds() / 86400.0,
        rdv_propose=rdv is not None,
        proposition_envoyee=prop is not None,
        maquette_envoyee=maq is not None,
        relances_deja=relances,
        envois_consecutifs=consecutifs,
        date_proposition=prop,
        date_rdv_propose=rdv,
        dernier_message_id=dernier.message_id,
        dernier_reply_id=(reponses[-1].message_id if reponses else None),
    )


# ── La décision ─────────────────────────────────────────────────────────────

# Cadences. Deux relances au maximum par motif : au-delà, c'est du harcèlement,
# et le relevé du 19/11 montrait déjà 51 fils avec trois envois ou plus d'affilée.
CADENCE_RDV = (3.0, 10.0)
CADENCE_PROPOSITION = (4.0, 12.0)
CADENCE_MAQUETTE = (5.0, 14.0)
MAX_RELANCES = 2
# Au-delà, on se tait : voir le commentaire dans decider().
MAX_ENVOIS_CONSECUTIFS = 4

# Statuts balayés. « Won » est absent volontairement (consigne du 11/11 : un
# client gagné n'est relancé nulle part). « NotInterested » aussi.
STATUTS_RELANCABLES = (
    "Interested", "MaybeLater", "MeetingBooked", "MeetingCompleted",
)


@dataclass(frozen=True)
class Decision:
    relancer: bool
    motif: str          # "rdv" | "proposition" | "maquette" | ""
    numero: int         # 1 ou 2
    raison: str
    revoir_dans: float  # jours avant le prochain examen (économie d'API)


def _pas_maintenant(raison: str, jours: float) -> Decision:
    return Decision(False, "", 0, raison, max(0.5, jours))


def decider(statut: str, ctx: Contexte | None, now: datetime,
            maquettes_actives: bool = False) -> Decision:
    """Applique les règles de Rudy, dans son ordre."""
    if ctx is None:
        return _pas_maintenant("fil vide", 30)

    # Le prospect a parlé en dernier : ce n'est pas une relance qu'il attend,
    # c'est une réponse. Le dashboard s'en charge ailleurs.
    if not ctx.dernier_est_de_nous:
        return _pas_maintenant("le prospect a parlé en dernier", 7)

    if statut not in STATUTS_RELANCABLES:
        return _pas_maintenant(f"statut {statut} — jamais relancé", 90)

    if ctx.relances_deja >= MAX_RELANCES:
        return _pas_maintenant(f"{ctx.relances_deja} relances déjà envoyées", 90)

    # PLAFOND DUR D'ENVOIS CONSÉCUTIFS. L'ancien moteur s'arrêtait à trois
    # messages sans réponse ; en réécrivant les règles j'avais perdu ce
    # garde-fou, et le relevé du 19/11 compte 51 fils à trois envois d'affilée
    # ou plus — dont douze à cinq. Leur en envoyer deux de plus serait du
    # harcèlement, quelle que soit la règle qui le justifie.
    if ctx.envois_consecutifs >= MAX_ENVOIS_CONSECUTIFS:
        return _pas_maintenant(
            f"{ctx.envois_consecutifs} messages d'affilée sans réponse — on arrête", 90)

    jours = ctx.jours_depuis_dernier

    # RÈGLE 3 — RDV calé ou passé, proposition envoyée, silence.
    if statut in ("MeetingBooked", "MeetingCompleted"):
        if not ctx.proposition_envoyee:
            # Rudy pilote ses RDV lui-même. Sans proposition envoyée, le bot
            # n'a rien à dire (consigne du 11/11).
            return _pas_maintenant("RDV calé, aucune proposition envoyée", 14)
        seuil = CADENCE_PROPOSITION[min(ctx.relances_deja, 1)]
        if jours >= seuil:
            return Decision(True, "proposition", ctx.relances_deja + 1,
                            f"proposition sans réponse depuis {jours:.0f} j", 0)
        return _pas_maintenant(f"proposition envoyée il y a {jours:.0f} j", seuil - jours)

    # RÈGLE 2 — Interested, un rendez-vous proposé, pas de suite.
    if ctx.rdv_propose:
        seuil = CADENCE_RDV[min(ctx.relances_deja, 1)]
        if jours >= seuil:
            return Decision(True, "rdv", ctx.relances_deja + 1,
                            f"rendez-vous proposé il y a {jours:.0f} j, sans suite", 0)
        return _pas_maintenant(f"rendez-vous proposé il y a {jours:.0f} j", seuil - jours)

    # Une maquette n'est ni un rendez-vous ni une proposition chiffrée : elle
    # ne figure pas dans les trois règles. Elle n'est relancée que si Rudy
    # l'active explicitement (MAQUETTES_AUTO=1).
    if ctx.maquette_envoyee and maquettes_actives:
        seuil = CADENCE_MAQUETTE[min(ctx.relances_deja, 1)]
        if jours >= seuil:
            return Decision(True, "maquette", ctx.relances_deja + 1,
                            f"maquette envoyée il y a {jours:.0f} j, sans retour", 0)
        return _pas_maintenant(f"maquette envoyée il y a {jours:.0f} j", seuil - jours)

    # RÈGLE 1 — Interested sans rien de proposé : Rudy traite à la main.
    return _pas_maintenant("rien ne lui a été proposé — Rudy traite à la main", 14)


# ── Le texte ────────────────────────────────────────────────────────────────

_MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
         "août", "septembre", "octobre", "novembre", "décembre")

_SIGNATURE = ("<p>Bien à vous,<br>Rudy Viard<br>"
              "Fondateur Webmarketing Conseil</p>")


def _le(d: datetime | None) -> str:
    if not d:
        return ""
    return f" du {d.day} {_MOIS[d.month - 1]}"


def _prenom(prospect) -> str:
    p = getattr(prospect, "first_name", None)
    if p and not str(p)[:4].isdigit() and len(str(p)) > 1:
        return " " + str(p).strip()
    return ""


def corps(motif: str, numero: int, prospect, ctx: Contexte) -> str:
    """Le message. Court, sans lien, sans tarif — et SANS jour ni horaire
    précis sur une relance de rendez-vous : le bot n'a pas accès à l'agenda de
    Rudy, et proposer un créneau qu'il ne pourra pas tenir est pire que rien.
    """
    nom = _prenom(prospect)

    if motif == "rdv":
        if numero == 1:
            coeur = (
                "<p>Nous devions nous parler, et sauf erreur de ma part nous "
                "n'avons pas réussi à nous avoir.</p>"
                "<p>Quels jours vous arrangent cette semaine ou la suivante ? "
                "Je m'adapte. Quinze minutes suffisent.</p>")
        else:
            coeur = (
                "<p>Je reviens une dernière fois sur notre appel, qui n'a jamais "
                "eu lieu.</p>"
                "<p>Si le moment n'est plus le bon, dites-le-moi simplement et "
                "je n'insiste pas. Sinon, donnez-moi deux ou trois moments qui "
                "vous arrangent.</p>")
    elif motif == "proposition":
        quand = _le(ctx.date_proposition)
        if numero == 1:
            coeur = (
                f"<p>Je reviens vers vous au sujet de ma proposition{quand}.</p>"
                "<p>Qu'en avez-vous pensé ? Si un point vous retient — le "
                "périmètre, le rythme, le budget —, dites-le-moi franchement, "
                "c'est fait pour être discuté.</p>")
        else:
            coeur = (
                f"<p>Dernier message de ma part au sujet de ma proposition{quand}.</p>"
                "<p>Un simple « non, pas pour nous » me va très bien : je "
                "préfère savoir plutôt que vous relancer dans le vide.</p>")
    elif motif == "maquette":
        quand = _le(ctx.date_proposition or ctx.date_rdv_propose)
        if numero == 1:
            coeur = (
                "<p>Je vous avais préparé une première version de votre site. "
                "Avez-vous pu la regarder ?</p>"
                "<p>Si quelque chose vous a arrêté — le style, une rubrique, le "
                "principe même —, dites-le-moi franchement : ça se change.</p>")
        else:
            coeur = (
                "<p>Dernier message au sujet de la maquette de votre site.</p>"
                "<p>Si ce n'est pas le moment, je la retire et je ne vous "
                "dérange plus. Un mot de votre part suffit.</p>")
        _ = quand
    else:
        raise ValueError(f"motif inconnu : {motif!r}")

    return f"<p>Bonjour{nom},</p>{coeur}{_SIGNATURE}"
