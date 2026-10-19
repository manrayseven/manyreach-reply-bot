"""Aucune réponse auto hors Webmarketing Conseil (Rudy 19/10).

Olivier Poizeau, prospect de l'espace LAERIS, a reçu le modèle de Webmarketing
Conseil : growpulser, growposter, refonte de site, et « Fondateur Webmarketing
Conseil » en signature. Le garde-fou des modèles FIXES fonctionnait bien (il
renvoie None pour un espace sans modèle) — mais le relais passait alors à l'IA,
qui a ressorti le modèle maison malgré la consigne de périmètre du prompt.

Rudy : « je ne veux pas promouvoir growpulser, growposter etc sur les autres
espaces ». La règle n'est donc plus confiée au jugement du modèle : hors
Webmarketing Conseil, le bot n'envoie RIEN. Les pistes remontent toujours en
alerte, les bounces gardent leur traitement, les négatifs deviennent silencieux.

Lance sans pytest :  python tests/test_space_no_autosend.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-test")

from src.fixed_replies import _template_key  # noqa: E402


def test_aucun_espace_ne_recoit_le_modele_webmarketing_conseil():
    """Le modèle « wc » n'est atteignable que hors espace et pour le compte
    par défaut. Un espace, quel qu'il soit, ne peut jamais y accéder."""
    for space in ("laeris", "cmaclim", "LAERIS", "un-nouveau-client"):
        assert _template_key(None, space) != "wc", space
        assert _template_key({"id": "rudy", "is_default": True}, space) != "wc", space
    # Un client non-défaut du compte principal non plus.
    assert _template_key({"id": "laeris"}, None) != "wc"
    # Seul le compte par défaut (ou l'absence de client) y a droit.
    assert _template_key(None, None) == "wc"
    assert _template_key({"id": "rudy", "is_default": True}, None) == "wc"


def test_un_espace_sans_modele_ne_retombe_pas_sur_un_autre():
    """Pas de modèle pour cet espace → None (et surtout pas celui d'un voisin)."""
    assert _template_key(None, "laeris") is None
    assert _template_key(None, "espace-inconnu") is None


def test_le_garde_fou_deterministe_existe_dans_la_boucle():
    """Le relais IA ne doit plus pouvoir contourner la règle : c'est du code,
    plus une consigne de prompt."""
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    i = src.index("RÉPONSES AUTO : WEBMARKETING CONSEIL UNIQUEMENT")
    bloc = src[i:i + 1800]
    assert "_is_wc" in bloc
    # Les pistes doivent continuer de remonter…
    assert "classification.intent not in ALERT_ONLY" in bloc
    # …et les bounces garder leur propre branche (sinon un journal noyé : 94 %
    # des entrants de Laeris sont des avis de non-remise).
    assert '("unsubscribe", "hostile", "bounce_or_auto")' in bloc
    # Le garde-fou doit précéder la rédaction (économise aussi les tokens).
    assert i < src.index("draft = fixed_draft(")


def test_dans_un_espace_toute_reponse_humaine_est_prioritaire():
    """Rudy 19/10, cas animation.vablc : « Vous pouvez nous adresser votre
    plaquette » est une demande limpide, mais aucun mot-clé ne la reconnaissait.
    Elle est restée 30 minutes derrière des centaines d'avis de non-remise.
    Dans un espace, la priorité ne dépend donc plus du vocabulaire.
    """
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    i = src.index("DANS UN ESPACE : toute réponse humaine est prioritaire")
    bloc = src[i:i + 900]
    assert "if space_id:" in bloc and "_lead_mids.add(_k)" in bloc
    # Les bounces restent exclus — sinon la priorité ne veut plus rien dire.
    assert "is_bounce_or_auto" in src[max(0, i - 400):i]

    # Le vocabulaire reste utile pour le compte principal : la demande de
    # documentation doit y être reconnue.
    from run_bot import _OPEN_DOOR_RE  # noqa: PLC0415
    for msg in (
        "Vous pouvez nous adresser votre plaquette et le cas échéant nous "
        "reprendrons contact avec vous.",
        "Merci de me faire parvenir votre documentation",
        "Envoyez-nous votre brochure",
    ):
        assert _OPEN_DOOR_RE.search(msg), msg


def test_toutes_les_touches_d_un_espace_remontent_en_alerte():
    """Rudy 19/10 : « je voudrais que pour la suite tu remontes bien toutes les
    touches ». Puisqu'un espace n'envoie plus rien, le silence ferait disparaître
    la réponse — personne ne la verrait. Tout humain qui écrit doit donc remonter,
    refus poli compris : c'est une information pour le client.
    """
    src = (ROOT / "scripts" / "run_bot.py").read_text(encoding="utf-8")
    i = src.index("TOUTES LES TOUCHES REMONTENT")
    bloc = src[i:i + 2600]
    # Une ALERTE, pas une ligne silencieuse.
    assert "🔔 ALERTE — réponse à traiter" in bloc
    # Avec la fiche complète, sinon « Mettre en relation » est inutilisable.
    assert "_alert_context(mr, prospect, thread, reply)" in bloc
    assert "set_alert_context" in bloc and "prospect_phone" in bloc
    # Le statut ManyReach reste mis à jour (sans envoi).
    assert "update_prospect" in bloc and "add_tag" in bloc
    # Et le verrou alerte/réponse auto est posé.
    assert "mark_alerted" in bloc


if __name__ == "__main__":
    from tests._runner import main as _main
    _main(globals())
