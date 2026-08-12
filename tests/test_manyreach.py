"""Tests du pré-filtre bounce/auto-reply (src/manyreach.py).

Capture les régressions du 16-17/06 :
- autoreply de voyage individuel ("currently travelling, back in the office") →
  doit être silencieux, pas une alerte objection_timing ;
- congé maternité avec CORPS VIDE (info uniquement dans le sujet) → silencieux ;
- un vrai refus / une fermeture saisonnière d'entreprise → PAS un bounce (doit
  atteindre le classifier).

Lance sans pytest :  python tests/test_manyreach.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.manyreach import (  # noqa: E402
    Message,
    detect_antispam_challenge,
    extract_challenge_url,
    is_bounce_or_auto,
    is_mailinblack,
)


def _msg(body="", subject="Re: Question", from_email="x@y.fr"):
    return Message(
        message_id="m1", created_at=datetime.now(timezone.utc), type="Reply",
        campaign_id="1", followup_id=None, from_email=from_email,
        to_email="r@x.fr", subject=subject, body=body,
    )


def test_ooo_travel_is_bounce():
    body = ("Dear, I am currently travelling, with uneven access to my email. "
            "I will be back in the office fully on Thursday June 18th.")
    assert is_bounce_or_auto(_msg(body=body)) is True


def test_maternity_subject_empty_body_is_bounce():
    # Corps vide, info uniquement dans le sujet (cas a.mocquet).
    assert is_bounce_or_auto(_msg(body="", subject="Congé maternite")) is True


def test_team_vacation_ooo_is_bounce():
    # Auto-reply "vacances d'équipe" (cas joliebibi) : ne doit PAS finir en alerte.
    body = ("Le team Jolie Bibi & son Mini sera absente du 24 juillet au 24 août "
            "inclus. Nous traiterons vos jolies commandes et répondrons à vos mails "
            "à notre retour. Passez un très bel été :)")
    assert is_bounce_or_auto(_msg(body=body)) is True
    # Négatif : un vrai lead intéressé ne matche pas.
    assert is_bounce_or_auto(_msg(
        body="Bonjour, ça m'intéresse, pouvez-vous me rappeler ? Merci")) is False


def test_repos_return_ooo_is_bounce():
    # Auto-reply "repos / on revient le X" (cas mollo.traiteur) : remontait en
    # alerte objection_timing ("nous revenons le lundi 24 aout" lu comme "plus
    # tard"). Doit être silencieux.
    body = ("Bonjour, Notre équipe prend quelques jours de repos, nous revenons "
            "le lundi 24 aout ! A très bientôt Mollo.")
    assert is_bounce_or_auto(_msg(body=body)) is True
    # Négatif : un vrai "recontactez-nous à la rentrée" reste un objection_timing
    # (ne doit PAS être avalé par le pré-filtre).
    assert is_bounce_or_auto(_msg(
        body="Pas le moment, recontactez-nous à la rentrée svp.")) is False


def test_email_address_change_is_bounce():
    # Auto-reply de changement d'adresse (cas harenovationconstruction) :
    # remontait en "Demande d'infos". Redirection d'adresse → silencieux.
    body = ("Bonjour, Merci pour votre message. Veuillez noter que notre adresse "
            "e-mail a changé. Nouvelle adresse : gestion@harenovation.fr. Merci "
            "de mettre à jour vos contacts et d'utiliser uniquement cette nouvelle "
            "adresse pour toute correspondance à venir.")
    assert is_bounce_or_auto(_msg(body=body)) is True


def test_parental_and_maternity_variants():
    assert is_bounce_or_auto(_msg(subject="Maternity leave - back in Sept")) is True
    assert is_bounce_or_auto(_msg(subject="Absence congé parental")) is True


def test_real_refusal_is_not_bounce():
    body = "Bonjour, non je ne serais pas intéressé. Nous sommes actuellement fermés. Merci."
    assert is_bounce_or_auto(_msg(body=body)) is False


def test_dated_absence_autoreply_is_bounce():
    # "en séminaire/formation/déplacement/absent JUSQU'AU X" = autoreply daté
    # (cas benjamin.blanchard) → silencieux, surtout pas une alerte "plus tard".
    for body in (
        "Bonjour, Je suis en séminaire jusqu'au 17 Juillet inclus. Je reviens vers vous.",
        "Je suis absent jusqu'au 20 août.",
        "En formation jusqu'au 5 septembre.",
    ):
        assert is_bounce_or_auto(_msg(body=body)) is True, body


def test_real_timing_request_is_not_bounce():
    # Un vrai "recontactez-moi en septembre" doit atteindre le classifier.
    assert is_bounce_or_auto(_msg(body="Rappelez-moi en septembre, le sujet m'intéresse.")) is False


def test_closure_autoreply_dated_is_bounce():
    # Autoreply de fermeture datée + "nous répondrons à partir du..." (cas enault).
    body = ("Le bureau sera exceptionnellement fermé du 07/07 au 09/07 inclus. "
            "Nous répondrons à vos demandes à partir du 10/07.")
    assert is_bounce_or_auto(_msg(body=body)) is True


def test_seasonal_closure_is_not_bounce():
    # Fermeture saisonnière d'entreprise avec réouverture → doit atteindre le
    # classifier (objection_timing), donc PAS attrapée par le pré-filtre bounce.
    body = "Nous sommes fermés pour la saison, réouverture le 15 mars. Recontactez-nous."
    assert is_bounce_or_auto(_msg(body=body)) is False


def test_mailer_daemon_from_is_bounce():
    assert is_bounce_or_auto(_msg(from_email="mailer-daemon@x.com")) is True


def test_mailinblack_detection():
    assert is_mailinblack(_msg(from_email="noreply@invitations.mailinblack.com")) is True
    assert is_mailinblack(_msg(body="un clic pour délivrer votre email")) is True
    assert is_mailinblack(_msg(body="message normal")) is False


def test_challenge_filter_names():
    # MailInBlack : par expéditeur (invitations + passerelles revendeur mib.*).
    assert detect_antispam_challenge(
        _msg(from_email="gegos@invitations.mailinblack.com")) == "MailInBlack"
    assert detect_antispam_challenge(
        _msg(from_email="ardrom@mib.ipgarde.com")) == "MailInBlack"
    # MailInBlack : par corps (challenge relayé depuis une autre adresse).
    assert detect_antispam_challenge(
        _msg(body="Un clic pour délivrer votre email !")) == "MailInBlack"
    # Autres filtres challenge-réponse connus.
    assert detect_antispam_challenge(
        _msg(from_email="verify@spamenmoins.com")) == "SpamEnMoins"
    assert detect_antispam_challenge(
        _msg(from_email="noreply@boxbe.com")) == "Boxbe"
    # Générique (filtre inconnu, phrase type challenge).
    assert detect_antispam_challenge(
        _msg(body="Votre message est en attente de validation.")) == "Challenge"
    assert detect_antispam_challenge(
        _msg(body="Please verify that you are a real person to deliver your email.")) == "Challenge"
    # Sujet seul (corps vide/strippé).
    assert detect_antispam_challenge(
        _msg(body="", subject="Votre email : validation requise")) == "Challenge"
    # Vrai challenge captcha "authentification" (poitiers.cci.fr / security-mail).
    assert detect_antispam_challenge(_msg(
        subject="Votre email a été bloqué pour authentification",
        body="Votre email n'a pas été délivré car le destinataire a souhaité "
             "mettre en place un Captcha pour valider l'existance de l'expéditeur. "
             "Pour libérer l'email, merci de remplir le formulaire accessible ici :",
        from_email="humail@poitiers.cci.fr")) == "Challenge"
    # Négatifs : vrai reply humain et bounce classique.
    assert detect_antispam_challenge(_msg(body="Bonjour, pas intéressé merci.")) is None
    # ⚠️ BOUNCE relayé via un serveur MailInBlack : contient "mailinblack" mais
    # c'est un rejet de remise (adresse inexistante) → PAS un challenge.
    assert detect_antispam_challenge(_msg(
        subject="Undelivered Mail Returned to Sender",
        body="The mail system: host mx-mibc-fr-04.mailinblack.com said: "
             "550 5.1.1: Recipient address rejected: User unknown in relay recipient table")) is None


def test_securelink_email_is_not_a_challenge():
    # Email NORMAL dont MailInBlack a réécrit les liens (securelink) : "mailinblack"
    # apparaît dans les URLs mais ce n'est PAS un challenge (cas autocars-groussin).
    assert detect_antispam_challenge(_msg(
        from_email="contact@autocars-groussin.com", subject="RE: Fiche Google",
        body="<html>bonjour <a href='https://mibc-fr-11.mailinblack.com/securelink/"
             "?url=https://www.webmarketing-conseil.fr&key=abc'>lien</a></html>")) is None


def test_boxtrapper_comquoi_is_challenge_with_link():
    # Challenge BoxTrapper/cPanel (cas comquoi) : détecté + lien bxd.cgi extrait.
    body = ("The message you sent requires that you verify that you are a real live "
            "human being and not a spam source. Or, you can click the following link: "
            "http://mail.comquoi.fr/cgi-sys/bxd.cgi?a=contact@comquoi.fr&id=abc123")
    m = _msg(from_email="contact@comquoi.fr",
             subject="[SPAM] Your email requires verification verify#abc123", body=body)
    assert detect_antispam_challenge(m) == "Challenge"
    assert extract_challenge_url(m) == "http://mail.comquoi.fr/cgi-sys/bxd.cgi?a=contact@comquoi.fr&id=abc123"


def test_extract_challenge_url_skips_scanner_and_pixel():
    # /protect/securelink = scanner de liens MailInBlack (pas une validation) → ignoré.
    assert extract_challenge_url(_msg(
        body="clique https://mibc-fr-11.mailinblack.com/protect/securelink?url="
             "https%3A%2F%2Fwww.webmarketing-conseil.fr&key=abc")) is None
    # Namespaces XML Outlook (xmlns:...) → jamais retournés (cas autocars-groussin :
    # renvoyait schemas.microsoft.com → page morte).
    assert extract_challenge_url(_msg(
        body='<html xmlns:m="http://schemas.microsoft.com/office/2004/12/omml">'
             'contenu sans vrai lien http://schemas.openxmlformats.org/x</html>')) is None
    # Vrai lien de validation en texte brut → extrait.
    assert extract_challenge_url(_msg(
        body="Pour valider : https://web-production-5a23a.up.railway.app/verify/XZHMT")
    ) == "https://web-production-5a23a.up.railway.app/verify/XZHMT"
    assert detect_antispam_challenge(
        _msg(body="This is the mail system. Your message could not be delivered.")) is None


def test_challenge_url_extraction():
    # URL de validation en texte brut → extraite (priorité aux domaines connus).
    m = _msg(body="Cliquez ici pour valider : https://app.mailinblack.com/v/abc123 merci")
    assert extract_challenge_url(m) == "https://app.mailinblack.com/v/abc123"
    # Repli : première URL non-image/désinscription.
    m2 = _msg(body="Validez sur https://portail-filtre.fr/confirm?id=42.")
    assert extract_challenge_url(m2) == "https://portail-filtre.fr/confirm?id=42"
    # Corps strippé sans URL (cas MailInBlack via API ManyReach) → None.
    assert extract_challenge_url(_msg(body="Un clic pour délivrer votre email !")) is None


def test_revalidate_challenge_purges_false_positive():
    # Re-contrôle d'un challenge STOCKÉ : on refetch le vrai corps et on relance
    # la détection. Cas autocars-groussin : vrai refus poli dont la signature
    # contenait des liens 'mailinblack.com/securelink' → loggé à tort en
    # MailInBlack. revalidate doit renvoyer ('', None) = purge.
    from src.manyreach import ManyReachClient

    mr = ManyReachClient.__new__(ManyReachClient)  # pas d'I/O réseau au __init__
    real_reply_body = (
        "<html xmlns:m=\"http://schemas.microsoft.com/office/2004/12/omml\">"
        "<body><p>Bonjour, merci pour votre proposition, mais nous ne sommes pas "
        "intéressés.</p><p>Cordialement</p>"
        "<a href=\"https://mibc-fr-11.mailinblack.com/securelink/?url="
        "https://www.webmarketing-conseil.fr&key=abc\">site</a></body></html>"
    )
    mr._request = lambda method, path, params=None, **kw: {"items": [{
        "messageId": "m1", "createdAt": "2026-08-05T12:40:21+00:00", "type": "Reply",
        "campaignId": 1, "followupId": 1, "fromEmail": "contact@autocars-groussin.com",
        "toEmail": "rudy@k-webmarketing.fr", "subject": "RE: Fiche Google",
        "body": real_reply_body,
    }]}
    assert mr.revalidate_challenge("contact@autocars-groussin.com", "RE: Fiche Google") == ("", None)

    # Vrai challenge MailInBlack (par corps) → reste un challenge.
    mr._request = lambda method, path, params=None, **kw: {"items": [{
        "messageId": "m2", "createdAt": "2026-08-05T12:40:21+00:00", "type": "Reply",
        "campaignId": 1, "followupId": 1, "fromEmail": "gw@mib.ipgarde.com",
        "toEmail": "rudy@x.fr", "subject": "Un clic pour délivrer votre email",
        "body": "Un clic pour délivrer votre email !",
    }]}
    filt, _url = mr.revalidate_challenge("gw@mib.ipgarde.com", "Un clic pour délivrer votre email")
    assert filt == "MailInBlack"

    # Message introuvable (aléa API) → (None, None) = ne rien purger.
    mr._request = lambda method, path, params=None, **kw: {"items": []}
    assert mr.revalidate_challenge("x@y.fr", "sujet") == (None, None)


def test_challenge_is_not_bounce_shadowed():
    # Un challenge doit être détecté AVANT le filtre bounce (l'ordre est géré
    # dans run_bot) mais ne doit pas non plus être avalé par is_bounce_or_auto
    # à cause d'un pattern trop large.
    m = _msg(from_email="prospect@invitations.mailinblack.com",
             body="Un clic pour délivrer votre email !")
    assert detect_antispam_challenge(m) == "MailInBlack"


if __name__ == "__main__":
    from tests._runner import main
    main(dict(globals()))
