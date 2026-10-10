from abc import ABC, abstractmethod
from typing import Any

from app.core.risultati import ErroreTool, comanda_tool, leggi_tool

__all__ = ["Reflex", "Receptor", "ErroreTool"]


class Reflex(ABC):
    """
    Classe base astratta di ogni azione involontaria (riflesso/nervo): deterministica, senza modello e senza decisioni.
    Regole, redazione, pseudonimizzazione ed effettori semplici la estendono; ciò che usa un LLM è un organo (agente).
    `BaseTool` resta come alias.

    Nell'implementazione di `get_tool_value` e `set_tool_value` si può sollevare qualsiasi eccezione (meglio
    `ErroreTool`, che porta codice e dettagli) o restituire False per un comando rifiutato: chi usa il tool passa
    da `leggi_stato` / `esegui_comando`, che non sollevano mai e restituiscono un dizionario
    {device_name, success, response, ...} da inoltrare all'agente padre (vedi `app.core.risultati`).
    """
    def __init__(self, target_device: str, name: str | None = None):
        self.target_device = target_device
        self.name = name or f"tool_{target_device}"

    @abstractmethod
    async def get_tool_value(self) -> Any:
        """Legge il valore attuale dal sensore o attuatore."""
        pass

    @abstractmethod
    async def set_tool_value(self, value: Any) -> bool:
        """Invia un comando di azionamento all'attuatore."""
        pass

    async def leggi_stato(self) -> dict[str, Any]:
        """Lettura protetta: non solleva mai, restituisce l'esito come dizionario."""
        return await leggi_tool(self, self.target_device)

    async def esegui_comando(self, valore: Any) -> dict[str, Any]:
        """Comando protetto: non solleva mai, restituisce l'esito come dizionario."""
        return await comanda_tool(self, self.target_device, valore)


class Receptor(Reflex):
    """
    Recettore: trasforma uno stimolo esterno (un'immagine, un sensore, un documento) in un segnale. Può appoggiarsi a un
    modello (es. l'OCR Mistral) ma non decide nulla: per questo non è un organo. Il modello è sostituibile.
    """


# Alias del vecchio nome (retrocompatibilità)
BaseTool = Reflex
