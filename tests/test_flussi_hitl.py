import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
from langgraph.types import Command

from app.agents.base_agent import BaseAgent
from app.core.configurazione import ErroreConfigurazione, costruisci_configurazione, imposta_configurazione
from app.core.errori_llm import ErroreLLM
from app.graph.builder import build_graph
from app.graph.hitl_config import DECISIONE_SISTEMA, e_decisione_sistema, hitl_manager
from app.graph.orchestrator import BrainAgent
from app.graph.timer_hitl import GestoreTimerHitl
from app.tools.sensor_tools import IoTDeviceTool


def configura(test, **hitl):
    imposta_configurazione(costruisci_configurazione({"hitl": hitl}))
    test.addCleanup(imposta_configurazione, None)


class AgenteRegistratore(BaseAgent):
    """Agente di prova: conta le esecuzioni e può proporre un'escalation."""

    def __init__(self, nome="agente_x", escalation=None):
        super().__init__(nome, ["lampada"], 30, 1.0)
        self.esecuzioni = 0
        self.escalation = escalation

    async def process(self, state, recent_events, relevant_readings, agent_escalations):
        self.esecuzioni += 1
        risultato = {"next_agent": "END", "messages": [AIMessage(content=f"[{self.name}] eseguito")]}
        if self.escalation:
            risultato["pending_escalations"] = [self.escalation]
        return risultato


class AgenteConLlmInguasto(BaseAgent):
    async def process(self, state, recent_events, relevant_readings, agent_escalations):
        raise ErroreLLM("CREDITI_ESAURITI", "crediti insufficienti")


class BaseGrafoTest(unittest.IsolatedAsyncioTestCase):
    thread = "thread-flussi"

    def setUp(self):
        hitl_manager.update_config(hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
        self.addCleanup(hitl_manager.update_config, hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
        self.enterContext(patch("app.tools.event_log.EventLog.get_recent_events", AsyncMock(return_value=[])))
        self.config = {"configurable": {"thread_id": self.thread}}

    def costruisci(self, agente, prossimo=None):
        self.agente = agente
        self.grafo, _ = build_graph(custom_agent_instances={agente.name: agente})
        self.stato = {
            "messages": [], "readings": [], "recent_events": [], "pending_escalations": [],
            "next_agent": prossimo or agente.name, "hitl_required": False, "config": {},
        }

    async def avvia(self):
        return await self.grafo.ainvoke(self.stato, config=self.config)

    async def in_pausa(self):
        return any(t.interrupts for t in (await self.grafo.aget_state(self.config)).tasks)

    async def tipo_pausa(self):
        for t in (await self.grafo.aget_state(self.config)).tasks:
            for i in t.interrupts:
                return i.value["type"]

    async def riprendi(self, decisione):
        return await self.grafo.ainvoke(Command(resume={"decision": decisione, "reasoning": "prova"}), config=self.config)


class ConfigurazioneFlussiTest(unittest.TestCase):
    def test_valori_predefiniti(self):
        cfg = costruisci_configurazione({})
        self.assertEqual((cfg.hitl_livello, cfg.hitl_azione_alla_scadenza), ("entrambi", "umano"))
        self.assertEqual(cfg.hitl_target_critici_brain, ["alarm_system", "front_door_lock"])

    def test_i_tre_livelli_e_le_tre_azioni_sono_ammessi(self):
        for livello in ("nodi", "brain", "entrambi"):
            self.assertEqual(costruisci_configurazione({"hitl": {"livello": livello}}).hitl_livello, livello)
        for azione in ("sistema", "respingi", "umano"):
            self.assertEqual(costruisci_configurazione({"hitl": {"azione_alla_scadenza": azione}}).hitl_azione_alla_scadenza, azione)

    def test_il_vecchio_valore_nessuna_equivale_a_umano(self):
        self.assertEqual(costruisci_configurazione({"hitl": {"azione_alla_scadenza": "nessuna"}}).hitl_azione_alla_scadenza, "umano")

    def test_valori_non_validi_sono_rifiutati(self):
        for hitl in ({"livello": "tutti"}, {"azione_alla_scadenza": "approva"}, {"target_critici_brain": "front_door_lock"},
                     {"target_critici_brain": [""]}):
            with self.subTest(hitl=hitl), self.assertRaises(ErroreConfigurazione):
                costruisci_configurazione({"hitl": hitl})

    def test_il_file_fornito_e_valido_e_conserva_il_comportamento_attuale(self):
        from pathlib import Path
        from app.core.configurazione import carica_configurazione

        cfg = carica_configurazione(Path(__file__).resolve().parents[1] / "configurazione.toml")
        self.assertEqual((cfg.hitl_livello, cfg.hitl_timer_attivo, cfg.hitl_azione_alla_scadenza), ("entrambi", False, "umano"))
        self.assertEqual(cfg.hitl_target_critici_brain, ["alarm_system", "front_door_lock"])

    def test_riconoscimento_della_decisione_sistema(self):
        self.assertTrue(e_decisione_sistema("sistema"))
        self.assertTrue(e_decisione_sistema(DECISIONE_SISTEMA))
        self.assertFalse(e_decisione_sistema("APPROVA"))


class FlussoNodiTest(BaseGrafoTest):
    def setUp(self):
        super().setUp()
        hitl_manager.update_config(hitl_nodes=["agente_x"])

    async def test_livello_nodi_e_entrambi_fermano_il_nodo(self):
        for livello in ("nodi", "entrambi"):
            with self.subTest(livello=livello):
                configura(self, livello=livello)
                self.thread = f"nodi-{livello}"
                self.config = {"configurable": {"thread_id": self.thread}}
                self.costruisci(AgenteRegistratore())
                await self.avvia()
                self.assertTrue(await self.in_pausa())
                self.assertEqual(await self.tipo_pausa(), "hitl_node_entry_interrupt")
                self.assertEqual(self.agente.esecuzioni, 0)

    async def test_livello_brain_non_ferma_mai_i_nodi(self):
        configura(self, livello="brain")
        self.costruisci(AgenteRegistratore())

        risultato = await self.avvia()

        self.assertFalse(await self.in_pausa())
        self.assertEqual(self.agente.esecuzioni, 1)
        self.assertIn("eseguito", risultato["messages"][-1].content)

    async def test_livello_brain_non_ferma_nemmeno_dopo_una_proposta_d_azione(self):
        configura(self, livello="brain")
        hitl_manager.update_config(hitl_nodes=[], hitl_targets=["front_door_lock"])
        escalation = {"source_agent": "agente_x", "target_device": "front_door_lock", "proposed_action": "LOCKED", "reason": "x"}
        self.costruisci(AgenteRegistratore(escalation=escalation))
        self.stato["config"] = {}

        await self.avvia()

        self.assertFalse(await self.tipo_pausa() == "hitl_action_proposal_interrupt")


class FlussoBrainTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        hitl_manager.update_config(hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
        self.addCleanup(hitl_manager.update_config, hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
        self.tool = IoTDeviceTool("front_door_lock", "UNLOCKED")
        self.brain = BrainAgent(tools=[self.tool])
        self.brain.event_log.log_event = AsyncMock()
        self.brain.event_log.mark_resolved = AsyncMock()
        self.brain.ask_brain = AsyncMock(return_value="DECISIONE: APPROVA\nMOTIVAZIONE: deciso dal Brain")

    def escalation(self, target="front_door_lock"):
        return {"id": "e1", "source_agent": "organ_x", "target_device": target, "proposed_action": "LOCKED", "reason": "conflitto"}

    async def processa(self, esc, contesto=None):
        stato = {"messages": [], "readings": [], "recent_events": [], "next_agent": "brain", "hitl_required": False,
                 "config": contesto or {}, "pending_escalations": [esc]}
        return await self.brain.process(stato, [], [], [esc])

    async def test_livelli_brain_ed_entrambi_chiedono_all_operatore_per_i_target_critici(self):
        for livello in ("brain", "entrambi"):
            with self.subTest(livello=livello):
                configura(self, livello=livello)
                with patch("langgraph.types.interrupt", return_value={"decision": "APPROVA", "reasoning": "ok"}) as pausa:
                    await self.processa(self.escalation())
                self.assertEqual(pausa.call_args.args[0]["type"], "escalation_approval_request")

    async def test_livello_nodi_lascia_decidere_il_brain_senza_chiedere(self):
        configura(self, livello="nodi")
        with patch("langgraph.types.interrupt") as pausa:
            risultato = await self.processa(self.escalation())

        pausa.assert_not_called()
        self.brain.ask_brain.assert_awaited()
        self.assertIn("APPROVATA", risultato["messages"][0].content)

    async def test_un_target_non_critico_non_viene_chiesto(self):
        configura(self, livello="brain")
        with patch("langgraph.types.interrupt") as pausa:
            await self.processa(self.escalation("lampada"))
        pausa.assert_not_called()

    async def test_i_target_critici_si_scelgono_in_configurazione(self):
        configura(self, livello="brain", target_critici_brain=["lampada"])
        with patch("langgraph.types.interrupt", return_value={"decision": "APPROVA"}) as pausa:
            await self.processa(self.escalation("lampada"))
            await self.processa(self.escalation("front_door_lock"))
        self.assertEqual(pausa.call_count, 1)

    async def test_una_lista_vuota_di_target_critici_non_protegge_nulla(self):
        configura(self, livello="brain", target_critici_brain=[])
        with patch("langgraph.types.interrupt") as pausa:
            await self.processa(self.escalation())
        pausa.assert_not_called()

    async def test_hitl_targets_dell_api_e_hitl_all_valgono_anche_per_il_brain(self):
        configura(self, livello="brain", target_critici_brain=[])
        hitl_manager.update_config(hitl_targets=["lampada"])
        with patch("langgraph.types.interrupt", return_value={"decision": "APPROVA"}) as pausa:
            await self.processa(self.escalation("lampada"))
        self.assertEqual(pausa.call_count, 1)

        hitl_manager.update_config(hitl_targets=[], hitl_all=True)
        with patch("langgraph.types.interrupt", return_value={"decision": "APPROVA"}) as pausa:
            await self.processa(self.escalation("qualsiasi"))
        self.assertEqual(pausa.call_count, 1)

    async def test_il_contesto_del_grafo_puo_aggiungere_target_protetti(self):
        configura(self, livello="brain", target_critici_brain=[])
        with patch("langgraph.types.interrupt", return_value={"decision": "APPROVA"}) as pausa:
            await self.processa(self.escalation("lampada"), {"hitl_targets": ["lampada"]})
        self.assertEqual(pausa.call_count, 1)


class PauseDiEmergenzaTest(BaseGrafoTest):
    async def test_l_llm_non_utilizzabile_ferma_il_grafo_con_qualsiasi_livello(self):
        for livello in ("nodi", "brain", "entrambi"):
            with self.subTest(livello=livello):
                configura(self, livello=livello)
                self.thread = f"emergenza-{livello}"
                self.config = {"configurable": {"thread_id": self.thread}}
                self.costruisci(AgenteConLlmInguasto("agente_llm", ["lampada"], 30, 1.0))
                await self.avvia()
                self.assertEqual(await self.tipo_pausa(), "llm_failure_human_intervention")

    async def test_il_guasto_non_risolto_chiede_l_operatore_anche_con_livello_nodi(self):
        from app.core.risultati import TOOL_ERRORE, nuovo_risultato
        from app.graph.state import EscalationItem

        configura(self, livello="nodi")
        tool = IoTDeviceTool("lampada", "OFF")
        brain = BrainAgent(tools=[tool])
        brain.event_log.log_event = AsyncMock()
        brain.event_log.mark_resolved = AsyncMock()
        esc = EscalationItem(
            source_agent="figlio", target_device="lampada", proposed_action="ON", reason="guasto", conflict_detected=True,
            tool_result=nuovo_risultato("lampada", TOOL_ERRORE, "guasto hw", actor="figlio", requested_value="ON"),
        ).model_dump()
        esc["tool_result"]["attempts"].append({"agent": "Brain", "phase": "troubleshooting"})

        with patch("langgraph.types.interrupt", return_value={"decision": "APPROVA"}) as pausa:
            await brain.process({"messages": [], "readings": [], "recent_events": [], "next_agent": "brain",
                                 "hitl_required": False, "config": {}, "pending_escalations": [esc]}, [], [], [esc])

        self.assertEqual(pausa.call_args.args[0]["type"], "tool_failure_human_intervention")


class DecisioneSistemaTest(BaseGrafoTest):
    async def test_ingresso_del_nodo_il_nodo_viene_eseguito_come_senza_hitl(self):
        configura(self, livello="nodi")
        hitl_manager.update_config(hitl_nodes=["agente_x"])
        self.costruisci(AgenteRegistratore())
        await self.avvia()
        self.assertEqual(self.agente.esecuzioni, 0)

        risultato = await self.riprendi(DECISIONE_SISTEMA)

        self.assertFalse(await self.in_pausa())
        self.assertEqual(self.agente.esecuzioni, 1)
        self.assertIn("eseguito", risultato["messages"][-1].content)

    async def test_per_confronto_respingi_annulla_e_approva_del_brain_non_esegue_il_nodo(self):
        configura(self, livello="nodi")
        hitl_manager.update_config(hitl_nodes=["agente_x"])
        self.costruisci(AgenteRegistratore())
        await self.avvia()

        risultato = await self.riprendi("RESPINGI")

        self.assertEqual(self.agente.esecuzioni, 0)
        self.assertIn("annullato", risultato["messages"][-1].content)

    async def test_proposta_d_azione_con_sistema_l_escalation_resta_al_brain(self):
        configura(self, livello="nodi")
        hitl_manager.update_config(hitl_targets=["front_door_lock"])
        escalation = {"id": "e1", "source_agent": "agente_x", "target_device": "front_door_lock", "proposed_action": "LOCKED", "reason": "x"}
        self.costruisci(AgenteRegistratore(escalation=escalation))
        await self.avvia()
        self.assertEqual(await self.tipo_pausa(), "hitl_action_proposal_interrupt")

        istantanea = await self.riprendi(DECISIONE_SISTEMA)

        self.assertFalse(await self.in_pausa())
        self.assertEqual([e["id"] for e in istantanea["pending_escalations"]], ["e1"])

    async def test_proposta_d_azione_respinta_svuota_le_escalation(self):
        configura(self, livello="nodi")
        hitl_manager.update_config(hitl_targets=["front_door_lock"])
        escalation = {"id": "e1", "source_agent": "agente_x", "target_device": "front_door_lock", "proposed_action": "LOCKED", "reason": "x"}
        self.costruisci(AgenteRegistratore(escalation=escalation))
        await self.avvia()

        istantanea = await self.riprendi("RESPINGI")

        self.assertEqual(istantanea["pending_escalations"], [])

    async def test_llm_non_utilizzabile_con_sistema_annulla_il_ciclo(self):
        configura(self, livello="entrambi")
        self.costruisci(AgenteConLlmInguasto("agente_llm", ["lampada"], 30, 1.0))
        await self.avvia()

        risultato = await self.riprendi(DECISIONE_SISTEMA)

        self.assertFalse(await self.in_pausa())
        self.assertIn("annullato", risultato["messages"][-1].content)


class DecisioneSistemaNelBrainTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        configura(self, livello="brain")
        self.tool = IoTDeviceTool("front_door_lock", "UNLOCKED")
        self.brain = BrainAgent(tools=[self.tool])
        self.brain.event_log.log_event = AsyncMock()
        self.brain.event_log.mark_resolved = AsyncMock()

    async def test_l_approvazione_scaduta_la_decide_il_brain_con_il_suo_modello(self):
        self.brain.ask_brain = AsyncMock(return_value="DECISIONE: RESPINGI\nMOTIVAZIONE: conflitto con la sicurezza")
        esc = {"id": "e1", "source_agent": "organ_x", "target_device": "front_door_lock", "proposed_action": "LOCKED", "reason": "c"}

        with patch("langgraph.types.interrupt", return_value={"decision": DECISIONE_SISTEMA, "reasoning": "timer scaduto"}):
            risultato = await self.brain.process(
                {"messages": [], "readings": [], "recent_events": [], "next_agent": "brain", "hitl_required": False,
                 "config": {}, "pending_escalations": [esc]}, [], [], [esc],
            )

        self.brain.ask_brain.assert_awaited_once()
        self.assertIn("RESPINTA", risultato["messages"][0].content)

    async def test_se_il_modello_del_brain_non_e_disponibile_scatta_la_pausa_di_emergenza(self):
        self.brain.ask_brain = AsyncMock(side_effect=ErroreLLM("LIMITE_RICHIESTE", "troppe richieste"))
        esc = {"id": "e1", "source_agent": "organ_x", "target_device": "front_door_lock", "proposed_action": "LOCKED", "reason": "c"}

        with patch("langgraph.types.interrupt", return_value={"decision": DECISIONE_SISTEMA}):
            with self.assertRaises(ErroreLLM):
                await self.brain.process(
                    {"messages": [], "readings": [], "recent_events": [], "next_agent": "brain", "hitl_required": False,
                     "config": {}, "pending_escalations": [esc]}, [], [], [esc],
                )

    async def test_guasto_non_risolto_con_sistema_viene_registrato_senza_altre_azioni(self):
        from app.core.risultati import TOOL_ERRORE, nuovo_risultato

        self.brain.tools["lampada"] = IoTDeviceTool("lampada", "OFF")
        esc = {"id": "e2", "source_agent": "figlio", "target_device": "lampada", "proposed_action": "ON", "reason": "guasto",
               "tool_result": nuovo_risultato("lampada", TOOL_ERRORE, "guasto hw", actor="figlio", requested_value="ON")}
        esc["tool_result"]["attempts"].append({"agent": "Brain", "phase": "troubleshooting"})
        self.brain._execute_semantic_override = AsyncMock()

        with patch("langgraph.types.interrupt", return_value={"decision": DECISIONE_SISTEMA, "reasoning": "timer scaduto"}):
            messaggi = await self.brain._richiedi_intervento_umano(esc)

        self.brain._execute_semantic_override.assert_not_awaited()
        self.assertIn("non risolto", messaggi[0])
        self.assertEqual(self.brain.event_log.log_event.await_args.kwargs["action"], "TOOL_FAILURE_HANDLED")
        self.assertEqual(self.brain.event_log.log_event.await_args.kwargs["new_value"], DECISIONE_SISTEMA)


class TimerConAzioneSistemaTest(BaseGrafoTest):
    class Orologio:
        def __init__(self):
            self.ora = 1_000_000.0

        def __call__(self):
            return self.ora

    async def asyncSetUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.orologio = self.Orologio()
        self.gestore = GestoreTimerHitl(lambda: self.grafo, os.path.join(self._cartella.name, "t.db"), self.orologio)
        await self.gestore.assicura_tabella()

    async def test_alla_scadenza_il_sistema_riprende_il_grafo_e_il_nodo_viene_eseguito(self):
        configura(self, livello="nodi", timer_attivo=1, timer_predefinito_secondi=30, azione_alla_scadenza="sistema")
        hitl_manager.update_config(hitl_nodes=["agente_x"])
        self.costruisci(AgenteRegistratore())
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.ora += 31

        ripresi = await self.gestore.processa_scadute()

        self.assertEqual(ripresi, [self.thread])
        self.assertFalse(await self.in_pausa())
        self.assertEqual(self.agente.esecuzioni, 1)

    async def test_con_azione_umano_il_grafo_resta_in_pausa(self):
        configura(self, livello="nodi", timer_attivo=1, timer_predefinito_secondi=30, azione_alla_scadenza="umano")
        hitl_manager.update_config(hitl_nodes=["agente_x"])
        self.costruisci(AgenteRegistratore())
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.ora += 31

        self.assertEqual(await self.gestore.processa_scadute(), [])
        self.assertTrue(await self.in_pausa())
        self.assertEqual(self.agente.esecuzioni, 0)

    async def test_il_vecchio_valore_nessuna_si_comporta_come_umano(self):
        configura(self, livello="nodi", timer_attivo=1, timer_predefinito_secondi=30, azione_alla_scadenza="nessuna")
        hitl_manager.update_config(hitl_nodes=["agente_x"])
        self.costruisci(AgenteRegistratore())
        await self.avvia()
        await self.gestore.stato(self.thread)
        self.orologio.ora += 31

        self.assertEqual(await self.gestore.processa_scadute(), [])
        self.assertTrue(await self.in_pausa())


class EndpointConfigurazioneHitlTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_hitl_config_mostra_le_scelte_del_file(self):
        from app.api.main import get_hitl_config

        configura(self, livello="brain", timer_attivo=1, azione_alla_scadenza="sistema", target_critici_brain=["alarm_system"])

        risposta = await get_hitl_config()

        self.assertIn("hitl_nodes", risposta)
        self.assertEqual(risposta["configurazione_file"], {
            "livello": "brain", "target_critici_brain": ["alarm_system"], "timer_attivo": True,
            "timer_predefinito_secondi": 300, "azione_alla_scadenza": "sistema",
        })


if __name__ == "__main__":
    unittest.main()
