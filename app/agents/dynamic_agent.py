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
from app.core.lingua import scegli
from app.core.risultati import APPLICATO, GIA_IMPOSTATO, e_comando_non_ammesso, e_guasto_tool
from app.graph.state import GraphState
from app.tools.effectors import default_effectors, trova_tool

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
        self.tool_value_catalog = "\n".join(self._voce_catalogo(target) for target in (managed_targets or [])) or "-"
        # Il formato della risposta è un contratto del codice (lo legge `estrai_decisione`), non una scelta dell'utente:
        # un prompt personalizzato che non lo descrive viene completato. I default seguono la lingua in vigore.
        self._custom_system_prompt = system_prompt_template
        self._custom_user_prompt = user_prompt_template
        self.tools = tools or default_effectors()

    @property
    def system_prompt_template(self) -> str:
        targets, catalogo = self.managed_targets, self.tool_value_catalog
        formato = scegli(
            "Devi decidere se eseguire un'azione (ACTION), fare escalation al Padre (ESCALATE), o fare nulla (NONE).\n"
            "Rispondi ESATTAMENTE nel formato:\nDECISIONE: [ACTION|ESCALATE|NONE]\nMOTIVAZIONE: [spiegazione]",
            "Decide whether to take an action (ACTION), escalate to your parent (ESCALATE) or do nothing (NONE).\n"
            "Answer EXACTLY in the format:\nDECISION: [ACTION|ESCALATE|NONE]\nREASON: [explanation]")
        if len(targets) > 1:
            formato = self._formato_multi_target()
        custom = self._custom_system_prompt
        if custom:
            if "DECISION" in custom:
                # Un prompt che descrive solo il formato a target singolo viene completato con quello a blocchi.
                return custom if len(targets) == 1 or "TARGET:" in custom else f"{custom.rstrip()}\n\n{formato}"
            return (f"{custom.rstrip()}\n\n"
                    + scegli(f"Gestisci i target {targets}. Valori validi per i device sotto il tuo dominio:\n{catalogo}\n",
                             f"You manage the targets {targets}. Valid values for the devices of your domain:\n{catalogo}\n")
                    + formato)
        return scegli(
            f"Sei l'agente gerarchico '{self.name}' (Livello {self.level}). Gestisci i target {targets}. "
            "Compila una checklist: target, stato attuale, valore valido, azione corretta.\n"
            f"Valori validi permessi per i device sotto il tuo dominio:\n{catalogo}\n",
            f"You are the hierarchical agent '{self.name}' (level {self.level}). You manage the targets {targets}. "
            "Go through a checklist: target, current state, valid value, right action.\n"
            f"Valid values for the devices of your domain:\n{catalogo}\n") + formato

    @staticmethod
    def _formato_multi_target() -> str:
        """Il formato a blocchi, uno per target: una sola chiamata al modello decide tutti i target dell'agente."""
        return scegli(
            "Per OGNI target decidi se eseguire un'azione (ACTION), fare escalation al Padre (ESCALATE) o fare nulla (NONE).\n"
            "Rispondi ESATTAMENTE con un blocco per target, nel formato:\n"
            "TARGET: [nome del target]\nDECISIONE: [ACTION|ESCALATE|NONE]\nMOTIVAZIONE: [spiegazione]",
            "For EACH target decide whether to take an action (ACTION), escalate to your parent (ESCALATE) or do nothing (NONE).\n"
            "Answer EXACTLY with one block per target, in the format:\n"
            "TARGET: [target name]\nDECISION: [ACTION|ESCALATE|NONE]\nREASON: [explanation]")

    @property
    def user_prompt_template(self) -> str:
        multi = len(self.managed_targets) > 1
        return self._custom_user_prompt or scegli(
            ("Target: {target}\n" if multi else "Target primario: {target}\n") +
            "Stato attuale letto dal tool: {current_status}\n"
            "Conflitto non riconciliato nel DB: {has_conflict}\n"
            "Riconciliazione recente: {recently_reconciled}\n"
            "Letture sensori rilevanti: {relevant_readings}\n"
            "Storico eventi recenti: {recent_events}\n"
            "Qual è la decisione corretta?",
            ("Targets: {target}\n" if multi else "Main target: {target}\n") +
            "Current state read from the tool: {current_status}\n"
            "Unreconciled conflict in the DB: {has_conflict}\n"
            "Recent reconciliation: {recently_reconciled}\n"
            "Relevant sensor readings: {relevant_readings}\n"
            "Recent events: {recent_events}\n"
            "What is the right decision?"
        )

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

        # 1-3. Stato di ogni target: lettura reale dal tool, blocchi, conflitti e riconciliazioni recenti.
        stati = [await self._stato_target(target, recent_events) for target in self.managed_targets]
        messaggi: list[str] = []
        escalation: list[dict[str, Any]] = []
        for s in stati:
            if s["bloccato"]:
                logger.info(f"[{self.name}] Target '{s['target']}' in stato di blocco '{s['stato']}'. In attesa sblocco.")
                messaggi.append(f"[{self.name}] {s['target']} in blocco ({s['stato']}). In attesa.")

        # 4. Una sola chiamata al modello per ciclo, per i target che chiedono davvero una decisione: un target bloccato
        #    attende lo sblocco, un conflitto non riconciliato sale comunque al padre. Senza target da decidere nessuna chiamata.
        da_decidere = [s for s in stati if not s["bloccato"] and not s["conflitto_aperto"]]
        decisioni: dict[str, tuple[str, str]] = {}
        if da_decidere:
            ai_response = await self.ask_brain(self.system_prompt_template, self._prompt_utente(da_decidere, relevant_readings,
                                                                                                  recent_events),
                                               temperature=0.0, max_tokens=2048)
            logger.info(f"[{self.name}] Risposta AI: {ai_response.strip().upper()}")
            blocchi = self.dividi_in_blocchi(ai_response, "TARGET", [s["target"] for s in da_decidere])
            # Tutte le decisioni vengono lette prima di agire: una risposta incompleta non comanda nessun dispositivo.
            for s in da_decidere:
                testo = blocchi.get(s["target"], "")
                decisione = self.estrai_decisione(testo, ("ACTION", "ESCALATE", "NONE"))
                if decisione is None:
                    raise self.decisione_non_riconosciuta(testo or ai_response)
                decisioni[s["target"]] = (decisione, testo.strip())

        # 5. Esito di ogni target: escalation verso il padre, azione, oppure nulla.
        for s in stati:
            if s["bloccato"]:
                continue
            if s["conflitto_aperto"]:
                esc, testo = await self._escala_target(s, scegli("conflitto non riconciliato nel registro eventi",
                                                                  "unreconciled conflict in the event log"))
            else:
                decisione, risposta = decisioni[s["target"]]
                if decisione == "ESCALATE":
                    esc, testo = await self._escala_target(s, risposta)
                elif decisione == "ACTION":
                    esc, testo = await self._agisci_su_target(s, risposta)
                else:
                    logger.info(f"[{self.name}] Nessuna azione richiesta su {s['target']}.")
                    esc, testo = None, (f"[{self.name}] Nessuna azione necessaria." if len(stati) == 1
                                        else f"[{self.name}] {s['target']}: nessuna azione necessaria.")
            if esc:
                escalation.append(esc)
            messaggi.append(testo)

        update: dict[str, Any] = {
            "next_agent": self._parent_route(),
            "config": runtime_config,
            "messages": [AIMessage(content="\n".join(messaggi))],
        }
        if escalation:
            update["pending_escalations"] = escalation
        return update

    async def _stato_target(self, target: str, recent_events: list[dict]) -> dict[str, Any]:
        """Stato reale del target (dal tool) e, dall'evento più recente che lo riguarda, conflitti e riconciliazioni."""
        tool_obj = trova_tool(target, self.tools)
        stato = "OFF"
        if tool_obj:
            try:
                stato = str(await tool_obj.get_tool_value())
            except Exception as e:
                logger.error(f"[{self.name}] Errore lettura tool {target}: {e}")
        eventi = sorted((e for e in recent_events if e.get("target") == target),
                        key=lambda x: str(x.get("timestamp", "")), reverse=True)
        conflitto, riconciliato, evento_conflitto = False, False, None
        if eventi:
            azione, attore = str(eventi[0].get("action", "")), str(eventi[0].get("actor", ""))
            if azione.startswith("RECONCILED_") or azione.startswith("RESOLVED_") or azione.startswith("UNBLOCKED"):
                riconciliato = True
            elif (azione in ["FORCE_SHUTDOWN", "SECURITY_LOCK"] or azione.startswith("REJECTED_")) and attore != self.name:
                conflitto, evento_conflitto = True, eventi[0]
        return {"target": target, "stato": stato, "bloccato": is_control_flag(stato), "conflitto": conflitto,
                "riconciliato": riconciliato, "conflitto_aperto": conflitto and not riconciliato,
                "evento_conflitto": evento_conflitto}

    def _prompt_utente(self, stati: list[dict[str, Any]], relevant_readings: list[dict], recent_events: list[dict]) -> str:
        """Il template utente in vigore: con più target ogni campo elenca il valore di ciascuno (`target: valore`)."""
        if len(stati) == 1:
            s = stati[0]
            valori = {"target": s["target"], "current_status": s["stato"], "has_conflict": s["conflitto"],
                      "recently_reconciled": s["riconciliato"]}
        else:
            per_target = lambda chiave: "; ".join(f"{s['target']}: {s[chiave]}" for s in stati)  # noqa: E731
            valori = {"target": ", ".join(s["target"] for s in stati), "current_status": per_target("stato"),
                      "has_conflict": per_target("conflitto"), "recently_reconciled": per_target("riconciliato")}
        return self.user_prompt_template.format(relevant_readings=relevant_readings, recent_events=recent_events, **valori)

    async def _escala_target(self, s: dict[str, Any], motivo: str) -> tuple[dict[str, Any], str]:
        target = s["target"]
        reason = f"Conflitto/anomalia rilevata da {self.name}: {motivo}"
        logger.warning(f"[{self.name}] Escalation inviata a '{self.parent_agent_name}' per {target}")
        proposta = get_configurazione().valore_attivo(target)
        escalation = self.create_escalation(
            target_device=target,
            proposed_action=proposta,
            reason=reason,
            conflict_detected=True,
            context_events=[s["evento_conflitto"]] if s["evento_conflitto"] else [],
        )
        await self.event_log.log_event(
            actor=self.name,
            action="ESCALATION_PROPOSED",
            target=target,
            old_value=s["stato"],
            new_value=proposta,
            reasoning=reason,
            escalated=True,
        )
        return escalation, f"[{self.name}] Conflitto rilevato. Escalation inviata a {self.parent_agent_name} per {target}."

    async def _agisci_su_target(self, s: dict[str, Any], risposta: str) -> tuple[dict[str, Any] | None, str]:
        target = s["target"]
        valore_attivo = get_configurazione().valore_attivo(target)
        risultato = await self.applica_stato(
            target=target,
            action="DYNAMIC_ACTION",
            new_value=valore_attivo,
            reasoning=f"Azione consigliata da AI in {self.name}: {risposta.upper()}",
            escalated=False,
            tools_map=self.tools,
        )
        if risultato["status"] == APPLICATO:
            return None, f"[{self.name}] Azione eseguita su {target} (applied=True)."
        if risultato["status"] == GIA_IMPOSTATO:
            return None, f"[{self.name}] {target} già impostato: nessuna azione necessaria."
        # Priorità insufficiente o guasto del dispositivo: il motivo sale al Padre nell'escalation
        escalation = await self.escala_da_risultato(risultato, proposed_action=valore_attivo)
        if e_guasto_tool(risultato):
            testo = f"[{self.name}] Guasto di {target}: {risultato['response']}. Escalation inviata a {self.parent_agent_name}."
        elif e_comando_non_ammesso(risultato):
            testo = f"[{self.name}] Comando non ammesso su {target}: {risultato['response']} Escalation inviata a {self.parent_agent_name}."
        else:
            testo = f"[{self.name}] Azione su {target} bloccata per priorità. Escalation inviata a {self.parent_agent_name}."
        return escalation, testo
