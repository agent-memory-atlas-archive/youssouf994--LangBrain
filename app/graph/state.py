import operator
from typing import Annotated, TypedDict, Any
from datetime import datetime, timezone
from uuid import uuid4
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

# --- Modelli Pydantic per i Dati ---

class SensorReading(BaseModel):
    sensor_id: str
    agent_owner: str  # es: "climate", "security", "lighting"
    value: float
    unit: str         # es: "°C", "%", "lux"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class ActionEvent(BaseModel):
    event_id: int | None = None
    actor: str        # es: "agent_climate", "brain"
    action: str       # es: "SET_TEMPERATURE", "LOCK_DOOR"
    target: str       # es: "ac_living_room", "front_door"
    value: str        # es: "22°C", "LOCKED"
    reasoning: str    # motivazione della decisione
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    escalated: bool = False

class EscalationItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    """Identificatore stabile: permette di aggiornare o rimuovere una singola escalation nello stato."""
    source_agent: str
    target_device: str
    proposed_action: str
    reason: str
    conflict_detected: bool = False
    context_events: list[dict] = []
    tool_result: dict | None = None
    """Se l'escalation nasce da un guasto di un dispositivo: esito strutturato (vedi app.core.risultati)."""

# --- Reducer Functions per LangGraph ---

def reduce_escalations(current: list[dict] | None, update: list[dict] | None) -> list[dict]:
    """
    Se update è una lista vuota [], svuota la coda delle escalation pendenti.
    Altrimenti accumula le nuove escalation. Un elemento con un `id` già presente sostituisce quello esistente
    (es. per aggiungere la diagnosi di un guasto); se ha `resolved: True` lo rimuove dalla coda.
    """
    if update == []:
        return []
    unite = list(current or [])
    for item in update or []:
        identificativo = item.get("id")
        posizione = next((i for i, e in enumerate(unite) if identificativo and e.get("id") == identificativo), None)
        if posizione is None:
            if not item.get("resolved"):
                unite.append(item)
        elif item.get("resolved"):
            unite.pop(posizione)
        else:
            unite[posizione] = item
    return unite

def reduce_readings(current: list[dict] | None, update: list[dict] | None) -> list[dict]:
    """
    Se update è una lista vuota [], azzera la lista.
    Altrimenti accumula lo storico delle letture nel ciclo di esecuzione.
    """
    if update == []:
        return []
    return (current or []) + (update or [])

# --- Stato Condiviso del Grafo LangGraph ---

class GraphState(TypedDict):
    # Standard LangGraph conversation messages reducer
    messages: Annotated[list[BaseMessage], add_messages]
    
    # Storico letture sensori accumulato (finestra temporale di N ore configurabile via config)
    readings: Annotated[list[dict], reduce_readings]
    
    # Storico eventi recenti dal DB (es. ultimi 5-10 min per audit/conflitti)
    recent_events: list[dict]
    
    # Coda delle escalation pendenti in attesa che l'orchestratore le evada
    pending_escalations: Annotated[list[dict], reduce_escalations]
    
    # Routing condizionale e controlli di flusso
    next_agent: str
    hitl_required: bool
    
    # Configurazione dinamica (es. {"readings_window_hours": 4, "hitl_nodes": ["brain", "security"]})
    config: dict[str, Any]
