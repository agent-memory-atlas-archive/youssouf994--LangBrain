"""
Agenti "nativi" di esempio: il core non dipende dal dominio, li carica per nome solo se il pacchetto `examples` è disponibile.
Per aggiungerne uno basta una voce in `AGENTI_NATIVI` (nome agente → "modulo:Classe").
"""

import importlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

AGENTI_NATIVI: dict[str, str] = {
    "agent_climate": "examples.smart_home.climate_agent:ClimateAgent",
}


def crea_agente_nativo(name: str, tools: dict[str, Any] | None = None):
    """Istanzia l'agente nativo `name`; None se non è nativo o se l'esempio non è installato."""
    percorso = AGENTI_NATIVI.get(name)
    if not percorso:
        return None
    modulo, _, classe = percorso.partition(":")
    try:
        return getattr(importlib.import_module(modulo), classe)(tools=tools)
    except ImportError:
        logger.warning("Agente nativo %s non disponibile (%s): uso un agente dinamico", name, modulo)
        return None
