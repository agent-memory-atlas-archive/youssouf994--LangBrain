"""
Gestione Centrale e Dinamica della Configurazione Human-in-the-Loop (HITL).
Permette di inserire punti di interrupt ovunque nel flusso del grafo LangGraph (su nodi, target sensori o azioni specifiche)
e definire il tempo massimo di attesa prima del fallback.
"""

import logging
from typing import Any
from pydantic import BaseModel, Field

from app.core.configurazione import (
    HITL_LIVELLO_BRAIN, HITL_LIVELLO_ENTRAMBI, HITL_LIVELLO_NODI, get_configurazione,
)

logger = logging.getLogger(__name__)

# Decisione con cui il timer scaduto restituisce la richiesta al sistema: il Brain valuta con il suo modello, come senza HITL.
DECISIONE_SISTEMA = "SISTEMA"


def flusso_nodi_attivo() -> bool:
    """True se il livello HITL configurato include il flusso sui nodi del grafo (wrapper)."""
    return get_configurazione().hitl_livello in (HITL_LIVELLO_NODI, HITL_LIVELLO_ENTRAMBI)


def flusso_brain_attivo() -> bool:
    """True se il livello HITL configurato include il flusso sul Brain (approvazione delle escalation)."""
    return get_configurazione().hitl_livello in (HITL_LIVELLO_BRAIN, HITL_LIVELLO_ENTRAMBI)


def e_decisione_sistema(decisione: object) -> bool:
    return DECISIONE_SISTEMA in str(decisione).upper()


# Distingue "parametro non indicato" (nessuna modifica) da None (azzera il valore).
NON_IMPOSTATO: Any = object()


class HitlConfigSchema(BaseModel):
    """Schema di configurazione dei punti di interrupt HITL e dei tempi di attesa."""
    hitl_all: bool = False
    """Se True, applica l'interrupt HITL prima dell'esecuzione di qualsiasi nodo nel grafo."""
    hitl_nodes: list[str] = Field(default_factory=list)
    """Lista dei nodi agenti su cui attivare l'interrupt HITL (es. ['brain', 'organ_security'])."""
    hitl_targets: list[str] = Field(default_factory=list)
    """Lista dei target sensore su cui attivare l'interrupt HITL (es. ['front_door_lock', 'cardiac_pacemaker'])."""
    hitl_actions: list[str] = Field(default_factory=list)
    """Lista delle azioni specifiche che richiedono approvazione umana (es. ['FORCE_SHUTDOWN', 'UNLOCK'])."""
    max_wait_seconds: int | None = Field(default=None)
    """
    Durata in secondi del timer HITL. Vale solo se il timer è attivo in configurazione.toml ([hitl] timer_attivo = 1) e
    sostituisce `timer_predefinito_secondi`; con null si torna al valore predefinito. Se il timer è spento è solo un metadato.
    """
    allow_override: bool = True
    """Se False, le direttive OVERRIDE inviate dall'operatore vengono ignorate: utile per disabilitare 'God Mode' in produzione."""


class HitlConfigManager:
    """Manager singleton per la gestione dinamica delle policy HITL a runtime."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.config = HitlConfigSchema()
        return cls._instance

    def get_config(self) -> HitlConfigSchema:
        return self.config

    def update_config(
        self,
        hitl_all: bool | None = None,
        hitl_nodes: list[str] | None = None,
        hitl_targets: list[str] | None = None,
        hitl_actions: list[str] | None = None,
        max_wait_seconds: int | None = NON_IMPOSTATO,
        allow_override: bool | None = None,
    ) -> HitlConfigSchema:
        """Aggiorna solo i parametri indicati. `max_wait_seconds=None` azzera l'attesa massima."""
        if hitl_all is not None:
            self.config.hitl_all = hitl_all
        if hitl_nodes is not None:
            self.config.hitl_nodes = hitl_nodes
        if hitl_targets is not None:
            self.config.hitl_targets = hitl_targets
        if hitl_actions is not None:
            self.config.hitl_actions = hitl_actions
        if max_wait_seconds is not NON_IMPOSTATO:
            self.config.max_wait_seconds = max_wait_seconds
        if allow_override is not None:
            self.config.allow_override = allow_override

        logger.info(
            "[HitlConfigManager] Configurazione HITL aggiornata: all=%s, nodes=%s, targets=%s, actions=%s, max_wait=%ss",
            self.config.hitl_all,
            self.config.hitl_nodes,
            self.config.hitl_targets,
            self.config.hitl_actions,
            self.config.max_wait_seconds,
        )
        return self.config

    def should_interrupt(
        self,
        node_name: str,
        state: dict[str, Any],
        proposed_target: str | None = None,
        proposed_action: str | None = None,
    ) -> bool:
        """Determina se un'esecuzione richiede l'invocazione di un interrupt HITL."""
        cfg = self.config

        # 1. Se hitl_all è attivo a livello globale
        if cfg.hitl_all:
            return True

        # 2. Se il nome del nodo rientra tra i nodi monitorati
        if node_name in cfg.hitl_nodes:
            return True

        # 3. Se il target rientra tra i dispositivi protetti
        if proposed_target and proposed_target in cfg.hitl_targets:
            return True

        # 4. Se l'azione rientra tra le azioni critiche
        if proposed_action and proposed_action in cfg.hitl_actions:
            return True

        # 5. Verifica nelle escalation pendenti nello stato del grafo
        pending_escalations = state.get("pending_escalations", [])
        for esc in pending_escalations:
            source = esc.get("source_agent")
            target = esc.get("target_device") or esc.get("target")
            action = esc.get("proposed_action") or esc.get("action")

            if source and source in cfg.hitl_nodes:
                return True
            if target and target in cfg.hitl_targets:
                return True
            if action and action in cfg.hitl_actions:
                return True

        # 6. Fallback allo stato dinamico del contesto del grafo (per retrocompatibilità)
        state_config = state.get("config", {})
        if state_config.get("hitl_all", False):
            return True
        if node_name in state_config.get("hitl_nodes", []):
            return True
        if proposed_target and proposed_target in state_config.get("hitl_targets", []):
            return True

        return False


# Singleton esportato
hitl_manager = HitlConfigManager()
