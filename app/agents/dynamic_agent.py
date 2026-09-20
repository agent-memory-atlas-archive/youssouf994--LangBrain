"""
Modulo DynamicAgent — Agente di Dominio Parametrico Gerarchico.
Rappresenta un nodo in una gerarchia ad albero a N livelli:
  Cervello (Livello 0) -> Organi (Livello 1) -> Componenti (Livello 2) -> Sotto-Componenti (Livello N).
"""

import logging
from typing import Any
from langchain_core.messages import AIMessage

from app.agents.base_agent import BaseAgent
from app.core.constants import is_control_flag
from app.core.configurazione import get_configurazione
from app.core.risultati import APPLICATO, GIA_IMPOSTATO, e_comando_non_ammesso, e_guasto_tool
from app.graph.state import GraphState
from app.tools.sensor_tools import get_default_iot_tools, trova_tool

logger = logging.getLogger(__name__)


class DynamicAgent(BaseAgent):
    """
    Agente dinamico configurabile a runtime.
    Può fungere da Organo o Componente dell'Organo, delegando verso i propri sotto-agenti
    o escalando verso il proprio agente Padre (parent_agent_name).
    """

    def __init__(
        self,
        name: str,
        managed_targets: list[str],
        parent_agent_name: str | None = "Brain",
        sub_agent_names: list[str] | None = None,
        level: int = 1,
        system_prompt_template: str | None = None,
        user_prompt_template: str | None = None,
        conflict_window_minutes: int = 30,
        priority_weight: float = 1.0,
        tools: dict[str, Any] | None = None,
    ):
        super().__init__(
            name=name,
            managed_targets=managed_targets,
            conflict_window_minutes=conflict_window_minutes,
            priority_weight=priority_weight,
        )
        self.parent_agent_name = parent_agent_name
        self.sub_agent_names = sub_agent_names or []
        self.level = level
        self.tool_value_catalog = "\n".join(self._voce_catalogo(target) for target in (managed_targets or [])) or "- Nessun target gestito."
        if system_prompt_template and "DECISIONE:" not in system_prompt_template:
            # Il formato della risposta è un contratto del codice (lo legge `estrai_decisione`), non una scelta dell'utente:
            # un prompt personalizzato che non lo descrive viene completato, altrimenti il modello non saprebbe come rispondere.
            system_prompt_template = (
                f"{system_prompt_template.rstrip()}\n\n"
                f"Gestisci i target {managed_targets}. Valori validi per i device sotto il tuo dominio:\n{self.tool_value_catalog}\n"
                "Devi decidere se eseguire un'azione (ACTION), fare escalation al Padre (ESCALATE), o fare nulla (NONE).\n"
                "Rispondi ESATTAMENTE nel formato:\n"
                "DECISIONE: [ACTION|ESCALATE|NONE]\n"
                "MOTIVAZIONE: [spiegazione]"
            )
        self.system_prompt_template = system_prompt_template or (
            f"Sei l'agente gerarchico '{name}' (Livello {level}). "
            f"Gestisci i target {managed_targets}. "
            "Compila una checklist: target, stato attuale, valore valido, azione corretta.\n"
            "Devi decidere se eseguire un'azione (ACTION), fare escalation al Padre (ESCALATE), o fare nulla (NONE).\n"
            "Valori validi permessi per i device sotto il tuo dominio:\n"
            f"{self.tool_value_catalog}\n"
            "Rispondi ESATTAMENTE nel formato:\n"
            "DECISIONE: [ACTION|ESCALATE|NONE]\n"
            "MOTIVAZIONE: [spiegazione]"
        )
        self.user_prompt_template = user_prompt_template or (
            "Target primario: {target}\n"
            "Stato attuale letto dal tool: {current_status}\n"
            "Conflitto non riconciliato nel DB: {has_conflict}\n"
            "Riconciliazione recente: {recently_reconciled}\n"
            "Letture sensori rilevanti: {relevant_readings}\n"
            "Storico eventi recenti: {recent_events}\n"
            "Qual è la decisione corretta?"
        )
        self.tools = tools or get_default_iot_tools()

    @staticmethod
    def _voce_catalogo(target: str) -> str:
        """Valori validi del target: da configurazione.toml se elencato, altrimenti in base al nome."""
        descrizione = get_configurazione().descrizione_valori(target)
        if descrizione:
            return f"- {target}: {descrizione}"
        nome = str(target).lower()
        if any(token in nome for token in ("breaker", "light", "lamp")):
            return f"- {target}: ['ON', 'OFF']"
        if any(token in nome for token in ("valve", "air")):
            return f"- {target}: ['OPEN', 'CLOSED', '100%']"
        return f"- {target}: ['OFF', 'ON']"

    def _parent_route(self) -> str:
        """Restituisce il nome canonico del nodo padre noto al grafo."""
        parent = self.parent_agent_name or "END"
        return "brain" if parent.casefold() == "brain" else parent

    async def process(
        self,
        state: GraphState,
        recent_events: list[dict],
        relevant_readings: list[dict],
        agent_escalations: list[dict],
    ) -> dict[str, Any]:
        logger.info(f"[{self.name}] [Livello {self.level}] Esecuzione analisi dinamica...")

        runtime_config = dict(state.get("config", {}))
        visited_agents = list(runtime_config.get("_hierarchy_visited", []))
        if self.name not in visited_agents:
            visited_agents.append(self.name)
        runtime_config["_hierarchy_visited"] = visited_agents

        # Le escalation ricevute da un figlio risalgono di un livello. La coda è
        # già nello stato condiviso: non viene riaggiunta, evitando duplicazioni.
        if agent_escalations:
            aggiornamenti: list[dict[str, Any]] = []
            messaggi = [f"[{self.name}] Escalation del sotto-agente inoltrata a {self.parent_agent_name}."]
            for esc in agent_escalations:
                # Un guasto di un dispositivo viene prima diagnosticato qui, se questo agente ha priorità sufficiente.
                if not self.puo_fare_troubleshooting(esc):
                    continue
                esito = await self.risolvi_guasto_tool(esc, self.tools)
                dispositivo = esc.get("target_device")
                if esito["resolved"]:
                    aggiornamenti.append({**esc, "resolved": True})
                    messaggi = [f"[{self.name}] Guasto di '{dispositivo}' risolto con troubleshooting: {esito['diagnosis']}"]
                else:
                    aggiornamenti.append({**esc, "tool_result": esito["tool_result"]})
                    messaggi.append(
                        f"[{self.name}] Troubleshooting su '{dispositivo}' non risolutivo: {esito['diagnosis']}"
                    )
            update: dict[str, Any] = {
                "next_agent": self._parent_route(),
                "config": runtime_config,
                "messages": [AIMessage(content="\n".join(messaggi))],
            }
            if aggiornamenti:
                update["pending_escalations"] = aggiornamenti
            return update

        # Visita i sotto-agenti configurati una volta per ciclo, anche quando
        # l'organo possiede target diretti. Al ritorno, l'organo prosegue la sua analisi.
        next_child = next((child for child in self.sub_agent_names if child not in visited_agents), None)
        if next_child:
            return {
                "next_agent": next_child,
                "config": runtime_config,
                "messages": [AIMessage(content=f"[{self.name}] Delega analisi al sotto-agente {next_child}.")],
            }

        # Se non ha target diretti e i figli sono stati visitati, risale al padre.
        if not self.managed_targets:
            return {"next_agent": self._parent_route(), "config": runtime_config}

        primary_target = self.managed_targets[0]
        tool_obj = trova_tool(primary_target, self.tools)

        # 1. Readout stato reale dal tool
        current_status = "OFF"
        if tool_obj:
            try:
                current_status = str(await tool_obj.get_tool_value())
            except Exception as e:
                logger.error(f"[{self.name}] Errore lettura tool {primary_target}: {e}")

        # 2. Cortocircuito se il dispositivo è in uno stato di blocco
        if is_control_flag(current_status):
            logger.info(f"[{self.name}] Target '{primary_target}' in stato di blocco '{current_status}'. In attesa sblocco.")
            return {
                "next_agent": self._parent_route(),
                "config": runtime_config,
                "messages": [AIMessage(content=f"[{self.name}] {primary_target} in blocco ({current_status}). In attesa.")],
            }

        # 3. Trova l'evento più recente in ordine cronologico per questo target
        target_events = [e for e in recent_events if e.get("target") == primary_target]
        target_events.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)

        has_conflict = False
        recently_reconciled = False
        conflict_event = None

        if target_events:
            latest_ev = target_events[0]
            latest_act = str(latest_ev.get("action", ""))
            latest_actor = str(latest_ev.get("actor", ""))

            if latest_act.startswith("RECONCILED_") or latest_act.startswith("RESOLVED_") or latest_act.startswith("UNBLOCKED"):
                recently_reconciled = True
            elif (latest_act in ["FORCE_SHUTDOWN", "SECURITY_LOCK"] or latest_act.startswith("REJECTED_")) and latest_actor != self.name:
                has_conflict = True
                conflict_event = latest_ev

        # 4. Prompting MAO
        user_prompt = self.user_prompt_template.format(
            target=primary_target,
            current_status=current_status,
            has_conflict=has_conflict,
            recently_reconciled=recently_reconciled,
            relevant_readings=relevant_readings,
            recent_events=recent_events,
        )

        ai_response = await self.ask_brain(self.system_prompt_template, user_prompt, temperature=0.0, max_tokens=2048)
        ai_response_str = ai_response.strip().upper()
        logger.info(f"[{self.name}] Risposta AI: {ai_response_str}")
        decisione = self.estrai_decisione(ai_response, ("ACTION", "ESCALATE", "NONE"))
        if decisione is None and not (has_conflict and not recently_reconciled):
            raise self.decisione_non_riconosciuta(ai_response)

        update: dict[str, Any] = {}
        update["config"] = runtime_config

        # 5. Gestione decisione ed Escalation verso il Padre
        if (has_conflict and not recently_reconciled) or decisione == "ESCALATE":
            reason = f"Conflitto/anomalia rilevata da {self.name}: {ai_response}"
            logger.warning(f"[{self.name}] Escalation inviata a '{self.parent_agent_name}' per {primary_target}")

            proposta = get_configurazione().valore_attivo(primary_target)
            escalation = self.create_escalation(
                target_device=primary_target,
                proposed_action=proposta,
                reason=reason,
                conflict_detected=True,
                context_events=[conflict_event] if conflict_event else [],
            )

            await self.event_log.log_event(
                actor=self.name,
                action="ESCALATION_PROPOSED",
                target=primary_target,
                old_value=current_status,
                new_value=proposta,
                reasoning=reason,
                escalated=True,
            )

            update["pending_escalations"] = [escalation]
            update["next_agent"] = self._parent_route()
            update["messages"] = [AIMessage(content=f"[{self.name}] Conflitto rilevato. Escalation inviata a {self.parent_agent_name} per {primary_target}.")]

        elif decisione == "ACTION" and not has_conflict:
            reasoning = f"Azione consigliata da AI in {self.name}: {ai_response_str}"
            valore_attivo = get_configurazione().valore_attivo(primary_target)
            risultato = await self.applica_stato(
                target=primary_target,
                action="DYNAMIC_ACTION",
                new_value=valore_attivo,
                reasoning=reasoning,
                escalated=False,
                tools_map=self.tools,
            )
            update["next_agent"] = self._parent_route()
            if risultato["status"] == APPLICATO:
                update["messages"] = [AIMessage(content=f"[{self.name}] Azione eseguita su {primary_target} (applied=True).")]
            elif risultato["status"] == GIA_IMPOSTATO:
                update["messages"] = [AIMessage(content=f"[{self.name}] {primary_target} già impostato: nessuna azione necessaria.")]
            else:
                # Priorità insufficiente o guasto del dispositivo: il motivo sale al Padre nell'escalation
                escalation = await self.escala_da_risultato(risultato, proposed_action=valore_attivo)
                update["pending_escalations"] = [escalation]
                if e_guasto_tool(risultato):
                    testo = f"[{self.name}] Guasto di {primary_target}: {risultato['response']}. Escalation inviata a {self.parent_agent_name}."
                elif e_comando_non_ammesso(risultato):
                    testo = f"[{self.name}] Comando non ammesso su {primary_target}: {risultato['response']} Escalation inviata a {self.parent_agent_name}."
                else:
                    testo = f"[{self.name}] Azione su {primary_target} bloccata per priorità. Escalation inviata a {self.parent_agent_name}."
                update["messages"] = [AIMessage(content=testo)]
        else:
            logger.info(f"[{self.name}] Nessuna azione richiesta.")
            update["next_agent"] = self._parent_route()
            update["messages"] = [AIMessage(content=f"[{self.name}] Nessuna azione necessaria.")]

        return update
