"""Compatibilité entre générations de modèles Claude (montée Haiku 4.5 -> 5.5, nov. 2026).

Haiku 5.5 coûte 10 fois moins cher que Haiku 4.5 (0,10 / 0,50 $ par million de tokens
contre 1 / 5 $), mais trois choses changent :
- la réflexion est active par défaut : la réponse peut commencer par un bloc "thinking",
  donc on ne lit jamais msg.content[0] mais le premier bloc de type "text" ;
- temperature / top_p / top_k non par défaut renvoient une erreur 400 ;
- le même texte compte ~30 % de tokens en plus (prévoir de la marge sur max_tokens).
"""

HAIKU = "claude-haiku-5-5"


def text_of(msg) -> str:
    """Texte de la réponse, quel que soit le nombre de blocs de réflexion avant."""
    for block in getattr(msg, "content", None) or []:
        kind = getattr(block, "type", None)
        # Sans "type" : ancien SDK ou faux client de test, le bloc unique est le texte.
        if kind == "text" or (kind is None and hasattr(block, "text")):
            return block.text or ""
    return ""


def options_for(model: str) -> dict:
    """Réglages à ajouter à messages.create selon le modèle.

    Haiku 5.5 : réflexion coupée (accepté jusqu'à l'effort high) pour garder le
    comportement, le coût et la latence de Haiku 4.5 sur des tâches de classement.
    """
    if "haiku-5" in (model or ""):
        return {"thinking": {"type": "disabled"}}
    return {}


def accepts_temperature(model: str) -> bool:
    """Les modèles de la génération 5.5 refusent une temperature non par défaut."""
    return not any(t in (model or "") for t in ("haiku-5", "sonnet-5-5", "opus-5-5", "fable-5"))
