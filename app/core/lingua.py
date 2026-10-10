"""
Language of LangBrain (`[generale] lingua` / `[general] language` in configurazione.toml): "it" or "en".

It chooses the default prompts of the Brain and the agents, the messages of the API and the pages. The answers of the
model are read in both languages whatever the setting ("DECISIONE: APPROVA" and "DECISION: APPROVE").
"""

from app.core.configurazione import get_configurazione


def lingua() -> str:
    try:
        return get_configurazione().lingua
    except Exception:  # an unreadable configuration must not stop a message from being written
        return "it"


def scegli(it: str, en: str) -> str:
    """The text in the language in force."""
    return en if lingua() == "en" else it
