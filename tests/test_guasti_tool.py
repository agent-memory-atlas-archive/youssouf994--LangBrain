import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.agents.base_agent import BaseAgent
from app.agents.dynamic_agent import DynamicAgent
from app.core.risultati import (
    ErroreTool, TOOL_ERRORE, comanda_tool, e_guasto_tool, leggi_tool, nuovo_risultato,
)
from app.graph.orchestrator import BrainAgent
from app.graph.state import EscalationItem, reduce_escalations
from app.tools.baseTool import BaseTool
from app.tools.sensor_tools import IoTDeviceTool


class AgenteDiProva(BaseAgent):
    async def process(self, state, recent_events, relevant_readings, agent_escalations):
        return {}


def crea_agente(nome="agente_prova", peso=1.0):
    agente = AgenteDiProva(nome, ["lampada"], 30, peso)
    agente.event_log.get_recent_events = AsyncMock(return_value=[])
    agente.event_log.log_event = AsyncMock()
    agente.event_log.mark_resolved = AsyncMock()
    return agente


def azioni_registrate(agente):
    return [c.kwargs["action"] for c in agente.event_log.log_event.await_args_list]


def guasto_di(segnalante, dispositivo="lampada", valore="ON", messaggio="controller Zigbee non risponde"):
    return nuovo_risultato(
        dispositivo, TOOL_ERRORE, messaggio, actor=segnalante, action="DYNAMIC_ACTION",
        old_value="OFF", requested_value=valore, error_type="SIMULATED_FAULT",
    )


def escalation_da_guasto(segnalante, **kwargs):
    return EscalationItem(
        source_agent=segnalante, target_device="lampada", proposed_action="TURN_ON", reason="guasto",
        conflict_detected=True, tool_result=guasto_di(segnalante, **kwargs),
    ).model_dump()


class RisultatiToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_comando_riuscito_restituisce_dispositivo_ed_esito(self):
        risultato = await comanda_tool(IoTDeviceTool("lampada"), "lampada", "ON")
        self.assertEqual(
            (risultato["device_name"], risultato["success"], risultato["value"]), ("lampada", True, "ON")
        )

    async def test_errore_tool_porta_messaggio_codice_e_dettagli(self):
        tool = IoTDeviceTool("lampada")

        async def guasto(_):
            raise ErroreTool("bus CAN scollegato", codice="BUS_DOWN", dettagli={"retry_dopo": 30})

        tool.set_tool_value = guasto
        risultato = await comanda_tool(tool, "lampada", "ON")

        self.assertFalse(risultato["success"])
        self.assertEqual(risultato["response"], "bus CAN scollegato")
        self.assertEqual(risultato["error_type"], "BUS_DOWN")
        self.assertEqual(risultato["details"], {"retry_dopo": 30})

    async def test_eccezione_generica_diventa_un_risultato_e_non_si_propaga(self):
        tool = IoTDeviceTool("lampada")
        tool.set_tool_value = AsyncMock(side_effect=ValueError("valore fuori scala"))
        risultato = await comanda_tool(tool, "lampada", "999")
        self.assertEqual((risultato["success"], risultato["error_type"]), (False, "ValueError"))
        self.assertIn("fuori scala", risultato["response"])

    async def test_comando_rifiutato_dal_dispositivo(self):
        tool = IoTDeviceTool("lampada")
        tool.set_tool_value = AsyncMock(return_value=False)
        risultato = await comanda_tool(tool, "lampada", "ON")
        self.assertEqual((risultato["success"], risultato["error_type"]), (False, "COMMAND_REJECTED"))

    async def test_dispositivo_che_non_risponde_va_in_timeout(self):
        tool = IoTDeviceTool("lampada")

        async def lento(_):
            await asyncio.sleep(5)

        tool.set_tool_value = lento
        risultato = await comanda_tool(tool, "lampada", "ON", timeout=0.05)
        self.assertEqual((risultato["success"], risultato["error_type"]), (False, "TIMEOUT"))

    async def test_lettura_di_un_dispositivo_guasto(self):
        tool = IoTDeviceTool("lampada")
        tool.imposta_guasto("sensore offline")
        risultato = await leggi_tool(tool, "lampada")
        self.assertEqual((risultato["success"], risultato["response"]), (False, "sensore offline"))

    async def test_basetool_espone_metodi_protetti(self):
        tool = IoTDeviceTool("lampada")
        self.assertIsInstance(tool, BaseTool)
        tool.imposta_guasto("guasto")
        self.assertFalse((await tool.esegui_comando("ON"))["success"])
        self.assertFalse((await tool.leggi_stato())["success"])

    async def test_guasto_transitorio_rientra_dopo_il_numero_di_operazioni_indicato(self):
        tool = IoTDeviceTool("lampada")
        tool.imposta_guasto("disturbo", operazioni=2)
        esiti = [(await tool.esegui_comando("ON"))["success"] for _ in range(3)]
        self.assertEqual(esiti, [False, False, True])

    async def test_guasto_solo_comandi_lascia_funzionare_le_letture(self):
        tool = IoTDeviceTool("lampada", "OFF")
        tool.imposta_guasto("attuatore bloccato", operazioni=1, solo_comandi=True)
        self.assertTrue((await tool.leggi_stato())["success"])
        self.assertFalse((await tool.esegui_comando("ON"))["success"])
        self.assertTrue((await tool.esegui_comando("ON"))["success"])

    def test_e_guasto_tool_distingue_guasto_da_altri_esiti(self):
        self.assertTrue(e_guasto_tool(guasto_di("a")))
        self.assertFalse(e_guasto_tool(nuovo_risultato("lampada", "REJECTED_PRIORITY", "bloccato")))
        self.assertFalse(e_guasto_tool(None))


class ApplicaStatoTest(unittest.IsolatedAsyncioTestCase):
    async def test_guasto_del_tool_restituisce_il_motivo_e_non_registra_l_azione_come_eseguita(self):
        agente = crea_agente()
        tool = IoTDeviceTool("lampada", "OFF")
        tool.imposta_guasto("controller non risponde")

        risultato = await agente.applica_stato("lampada", "TURN_ON", "ON", "test", tools_map={"lampada": tool})

        self.assertEqual(risultato["status"], "TOOL_ERROR")
        self.assertFalse(risultato["success"])
        self.assertEqual(risultato["device_name"], "lampada")
        self.assertEqual(risultato["response"], "controller non risponde")
        self.assertEqual(risultato["error_type"], "SIMULATED_FAULT")
        self.assertEqual(azioni_registrate(agente), ["TOOL_ERROR_TURN_ON"])
        self.assertEqual(risultato["attempts"][0]["agent"], "agente_prova")

    async def test_tool_assente_e_un_guasto(self):
        agente = crea_agente()
        risultato = await agente.applica_stato("lampada", "TURN_ON", "ON", "test", tools_map={})
        self.assertEqual(risultato["status"], "TOOL_MISSING")
        self.assertTrue(e_guasto_tool(risultato))

    async def test_azione_riuscita(self):
        agente = crea_agente()
        tool = IoTDeviceTool("lampada", "OFF")
        risultato = await agente.applica_stato("lampada", "TURN_ON", "ON", "test", tools_map={"lampada": tool})
        self.assertEqual((risultato["status"], risultato["success"]), ("APPLIED", True))
        self.assertEqual(await tool.get_tool_value(), "ON")

    async def test_valore_gia_impostato_non_e_un_errore(self):
        agente = crea_agente()
        risultato = await agente.applica_stato(
            "lampada", "TURN_ON", "ON", "test", tools_map={"lampada": IoTDeviceTool("lampada", "ON")}
        )
        self.assertEqual((risultato["status"], risultato["success"]), ("ALREADY_SET", True))

    async def test_fallimento_dell_audit_non_annulla_l_attuazione(self):
        agente = crea_agente()
        agente.event_log.log_event = AsyncMock(side_effect=RuntimeError("db non raggiungibile"))
        tool = IoTDeviceTool("lampada", "OFF")

        risultato = await agente.applica_stato("lampada", "TURN_ON", "ON", "test", tools_map={"lampada": tool})

        self.assertTrue(risultato["success"])
        self.assertFalse(risultato["audit_logged"])
        self.assertEqual(await tool.get_tool_value(), "ON")

    async def test_tool_creato_on_demand_dopo_la_costruzione_dell_agente_viene_trovato(self):
        from app.tools.sensor_tools import get_tool

        agente = crea_agente()
        tool = get_tool("dispositivo_on_demand_prova", initial_value="OFF")

        risultato = await agente.applica_stato("dispositivo_on_demand_prova", "TURN_ON", "ON", "test", tools_map={})

        self.assertEqual(risultato["status"], "APPLIED")
        self.assertEqual(await tool.get_tool_value(), "ON")

    async def test_lettura_iniziale_fallita_non_impedisce_il_comando(self):
        agente = crea_agente()
        tool = IoTDeviceTool("lampada", "OFF")
        tool.imposta_guasto("sensore offline", operazioni=1)

        risultato = await agente.applica_stato("lampada", "TURN_ON", "ON", "test", tools_map={"lampada": tool})

        self.assertTrue(risultato["success"])
        self.assertEqual(risultato["details"]["lettura"], "sensore offline")

    async def test_apply_status_resta_compatibile_con_il_risultato_booleano(self):
        agente = crea_agente()
        guasto = IoTDeviceTool("lampada", "OFF")
        guasto.imposta_guasto("errore")
        self.assertTrue(await agente.apply_status("lampada", "A", "ON", "t", tools_map={"lampada": IoTDeviceTool("lampada", "OFF")}))
        self.assertFalse(await agente.apply_status("lampada", "A", "ON", "t", tools_map={"lampada": guasto}))
        self.assertFalse(await agente.apply_status("lampada", "A", "ON", "t", tools_map={"lampada": IoTDeviceTool("lampada", "ON")}))


class ValoreFisicoOverrideTest(unittest.TestCase):
    def test_i_nomi_degli_eventi_di_errore_non_diventano_mai_valori_fisici(self):
        from app.graph.orchestrator import _normalize_action_value

        for nome in ("TOOL_ERROR_RECONCILED_FORCE_SHUTDOWN", "TROUBLESHOOT_RETRY_DYNAMIC_ACTION", "TOOL_FAILURE_HANDLED"):
            with self.subTest(nome=nome):
                self.assertEqual(_normalize_action_value("living_room_lights", "UNBLOCK_AND_SET", None, nome), "ON")

    def test_un_valore_esplicito_valido_viene_rispettato(self):
        from app.graph.orchestrator import _normalize_action_value

        self.assertEqual(_normalize_action_value("front_door_lock", "UNBLOCK_AND_SET", "LOCKED", "x"), "LOCKED")


class ReducerEscalationTest(unittest.TestCase):
    def test_elemento_con_id_esistente_sostituisce_quello_precedente(self):
        corrente = [{"id": "a", "reason": "vecchio"}, {"id": "b"}]
        risultato = reduce_escalations(corrente, [{"id": "a", "reason": "nuovo"}])
        self.assertEqual(risultato, [{"id": "a", "reason": "nuovo"}, {"id": "b"}])

    def test_elemento_risolto_viene_rimosso(self):
        self.assertEqual(reduce_escalations([{"id": "a"}, {"id": "b"}], [{"id": "a", "resolved": True}]), [{"id": "b"}])

    def test_elementi_senza_id_o_nuovi_vengono_accumulati_e_la_lista_vuota_svuota(self):
        self.assertEqual(reduce_escalations([{"x": 1}], [{"x": 2}, {"id": "n"}]), [{"x": 1}, {"x": 2}, {"id": "n"}])
        self.assertEqual(reduce_escalations([{"id": "a"}], []), [])

    def test_elemento_risolto_mai_visto_non_viene_aggiunto(self):
        self.assertEqual(reduce_escalations([], [{"id": "z", "resolved": True}]), [])


class TroubleshootingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        eventi = patch("app.tools.event_log.EventLog.get_recent_events", AsyncMock(return_value=[]))
        eventi.start()
        self.addCleanup(eventi.stop)

    def crea_padre(self, peso=50.0, risposta="DIAGNOSI: disturbo transitorio\nDECISIONE: RETRY"):
        padre = crea_agente("padre_prova", peso)
        padre.ask_brain = AsyncMock(return_value=risposta)
        return padre

    def test_priorita_sufficiente_e_un_solo_tentativo_per_agente(self):
        esc = escalation_da_guasto("figlio_prova")
        AgenteDiProva("figlio_prova", ["lampada"], 30, 10.0)

        self.assertTrue(crea_agente("padre_forte", 50.0).puo_fare_troubleshooting(esc))
        self.assertFalse(crea_agente("padre_debole", 5.0).puo_fare_troubleshooting(esc))

        forte = crea_agente("padre_forte", 50.0)
        esc["tool_result"]["attempts"].append({"agent": "padre_forte", "phase": "troubleshooting"})
        self.assertFalse(forte.puo_fare_troubleshooting(esc))

    def test_solo_i_guasti_di_dispositivo_sono_oggetto_di_troubleshooting(self):
        senza_guasto = AgenteDiProva("f", ["lampada"], 30, 1.0).create_escalation("lampada", "ON", "conflitto")
        self.assertFalse(crea_agente("padre_forte2", 50.0).puo_fare_troubleshooting(senza_guasto))

    def test_il_segnalante_non_diagnostica_il_proprio_guasto(self):
        self.assertFalse(crea_agente("figlio_x", 99.0).puo_fare_troubleshooting(escalation_da_guasto("figlio_x")))

    async def test_ritento_riuscito_risolve_il_guasto(self):
        padre = self.crea_padre()
        tool = IoTDeviceTool("lampada", "OFF")

        esito = await padre.risolvi_guasto_tool(escalation_da_guasto("figlio_p1"), {"lampada": tool})

        self.assertTrue(esito["resolved"])
        self.assertEqual(await tool.get_tool_value(), "ON")
        self.assertEqual(esito["diagnosis"], "disturbo transitorio")
        ultimo = esito["tool_result"]["attempts"][-1]
        self.assertEqual((ultimo["agent"], ultimo["decision"], ultimo["status"]), ("padre_prova", "RETRY", "APPLIED"))
        padre.event_log.mark_resolved.assert_awaited_once_with("lampada")

    async def test_ritento_fallito_lascia_il_guasto_aperto_con_il_motivo(self):
        padre = self.crea_padre()
        tool = IoTDeviceTool("lampada", "OFF")
        tool.imposta_guasto("hardware guasto")

        esito = await padre.risolvi_guasto_tool(escalation_da_guasto("figlio_p2"), {"lampada": tool})

        self.assertFalse(esito["resolved"])
        self.assertEqual(esito["tool_result"]["attempts"][-1]["status"], "TOOL_ERROR")
        self.assertEqual(esito["tool_result"]["attempts"][-1]["response"], "hardware guasto")
        padre.event_log.mark_resolved.assert_not_awaited()

    async def test_diagnosi_che_consiglia_escalation_non_ritenta(self):
        padre = self.crea_padre(risposta="DIAGNOSI: guasto hardware permanente\nDECISIONE: ESCALATE")
        tool = IoTDeviceTool("lampada", "OFF")

        esito = await padre.risolvi_guasto_tool(escalation_da_guasto("figlio_p3"), {"lampada": tool})

        self.assertEqual((esito["resolved"], esito["decision"]), (False, "ESCALATE"))
        self.assertEqual(await tool.get_tool_value(), "OFF")
        self.assertEqual(esito["tool_result"]["attempts"][-1]["status"], "NOT_ATTEMPTED")

    async def test_modello_non_disponibile_fa_risalire_l_errore_senza_ritentare(self):
        from app.core.errori_llm import ErroreLLM

        padre = self.crea_padre()
        padre.ask_brain = AsyncMock(side_effect=ErroreLLM("CREDITI_ESAURITI", "crediti insufficienti"))
        tool = IoTDeviceTool("lampada", "OFF")

        with self.assertRaises(ErroreLLM):
            await padre.risolvi_guasto_tool(escalation_da_guasto("figlio_p4"), {"lampada": tool})

        self.assertEqual(await tool.get_tool_value(), "OFF")

    async def test_il_ritento_rispetta_il_controllo_di_priorita_del_padre(self):
        AgenteDiProva("attore_forte", ["lampada"], 30, 900.0)
        padre = self.crea_padre(peso=50.0)
        padre.event_log.get_recent_events = AsyncMock(return_value=[{
            "actor": "attore_forte", "action": "SECURITY_LOCK", "target": "lampada", "new_value": "LOCKED",
            "timestamp": "2999-01-01 00:00:00",
        }])
        tool = IoTDeviceTool("lampada", "OFF")

        esito = await padre.risolvi_guasto_tool(escalation_da_guasto("figlio_p5"), {"lampada": tool})

        self.assertFalse(esito["resolved"])
        self.assertEqual(esito["tool_result"]["attempts"][-1]["status"], "REJECTED_PRIORITY")
        self.assertEqual(await tool.get_tool_value(), "OFF")


class DynamicAgentGuastoTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        eventi = patch("app.tools.event_log.EventLog.get_recent_events", AsyncMock(return_value=[]))
        eventi.start()
        self.addCleanup(eventi.stop)

    def crea(self, nome, peso, tool, risposta="DECISIONE: ACTION\nMOTIVAZIONE: accendere"):
        agente = DynamicAgent(nome, ["lampada"], parent_agent_name="Brain", priority_weight=peso, tools={"lampada": tool})
        agente.event_log.get_recent_events = AsyncMock(return_value=[])
        agente.event_log.log_event = AsyncMock()
        agente.event_log.mark_resolved = AsyncMock()
        agente.ask_brain = AsyncMock(return_value=risposta)
        return agente

    async def test_guasto_del_tool_sale_al_padre_con_il_motivo(self):
        tool = IoTDeviceTool("lampada", "OFF")
        tool.imposta_guasto("controller Zigbee non risponde")
        agente = self.crea("figlio_d1", 10.0, tool)

        risultato = await agente.process({"config": {}}, [], [], [])

        escalation = risultato["pending_escalations"][0]
        self.assertEqual(risultato["next_agent"], "brain")
        self.assertEqual(escalation["tool_result"]["status"], "TOOL_ERROR")
        self.assertEqual(escalation["tool_result"]["response"], "controller Zigbee non risponde")
        self.assertIn("controller Zigbee non risponde", escalation["reason"])
        self.assertIn("ESCALATION_PROPOSED", azioni_registrate(agente))

    async def test_l_escalation_per_conflitto_propone_il_valore_attivo_del_dispositivo_non_un_comando_generico(self):
        from app.core.configurazione import costruisci_configurazione, imposta_configurazione

        imposta_configurazione(costruisci_configurazione({
            "politica": {"dispositivi_non_elencati": "consenti"},
            "dispositivi": {"porta_x": {"valori": ["LOCKED", "UNLOCKED"], "valore_attivo": "LOCKED"}},
        }))
        self.addCleanup(imposta_configurazione, None)
        agente = DynamicAgent("figlio_d3", ["porta_x"], parent_agent_name="Brain", priority_weight=10.0,
                              tools={"porta_x": IoTDeviceTool("porta_x", "UNLOCKED")})
        agente.event_log.log_event = AsyncMock()
        agente.ask_brain = AsyncMock(return_value="DECISIONE: NONE\nMOTIVAZIONE: nulla da fare")
        conflitto = {"actor": "operatore", "action": "SECURITY_LOCK", "target": "porta_x", "timestamp": "2999-01-01 00:00:00"}

        risultato = await agente.process({"config": {}}, [conflitto], [], [])

        self.assertEqual(risultato["pending_escalations"][0]["proposed_action"], "LOCKED")
        self.assertEqual(agente.event_log.log_event.await_args.kwargs["new_value"], "LOCKED")

    async def test_valore_gia_impostato_non_genera_escalation(self):
        agente = self.crea("figlio_d2", 10.0, IoTDeviceTool("lampada", "ON"))
        risultato = await agente.process({"config": {}}, [], [], [])
        self.assertNotIn("pending_escalations", risultato)
        self.assertIn("già impostato", risultato["messages"][0].content)

    async def test_padre_con_priorita_sufficiente_risolve_e_rimuove_l_escalation(self):
        AgenteDiProva("figlio_d3", ["lampada"], 30, 10.0)
        tool = IoTDeviceTool("lampada", "OFF")
        padre = self.crea("padre_d3", 50.0, tool, risposta="DIAGNOSI: transitorio\nDECISIONE: RETRY")
        esc = escalation_da_guasto("figlio_d3")

        risultato = await padre.process({"config": {}}, [], [], [esc])

        self.assertEqual(await tool.get_tool_value(), "ON")
        self.assertEqual(risultato["pending_escalations"], [{**esc, "resolved": True}])
        self.assertEqual(reduce_escalations([esc], risultato["pending_escalations"]), [])
        self.assertIn("risolto", risultato["messages"][0].content)

    async def test_padre_che_non_risolve_inoltra_l_escalation_con_la_diagnosi(self):
        AgenteDiProva("figlio_d4", ["lampada"], 30, 10.0)
        tool = IoTDeviceTool("lampada", "OFF")
        tool.imposta_guasto("hardware guasto")
        padre = self.crea("padre_d4", 50.0, tool, risposta="DIAGNOSI: sembra hardware\nDECISIONE: RETRY")
        esc = escalation_da_guasto("figlio_d4")

        risultato = await padre.process({"config": {}}, [], [], [esc])

        aggiornata = risultato["pending_escalations"][0]
        self.assertEqual(aggiornata["id"], esc["id"])
        self.assertEqual(aggiornata["tool_result"]["diagnosis"], "sembra hardware")
        self.assertEqual(risultato["next_agent"], "brain")

    async def test_padre_con_priorita_insufficiente_inoltra_senza_diagnosticare(self):
        AgenteDiProva("figlio_d5", ["lampada"], 30, 100.0)
        padre = self.crea("padre_d5", 5.0, IoTDeviceTool("lampada", "OFF"))

        risultato = await padre.process({"config": {}}, [], [], [escalation_da_guasto("figlio_d5")])

        padre.ask_brain.assert_not_awaited()
        self.assertNotIn("pending_escalations", risultato)
        self.assertEqual(risultato["next_agent"], "brain")


class BrainGuastoTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        eventi = patch("app.tools.event_log.EventLog.get_recent_events", AsyncMock(return_value=[]))
        eventi.start()
        self.addCleanup(eventi.stop)
        self.tool = IoTDeviceTool("lampada", "OFF")
        self.brain = BrainAgent(tools=[self.tool], sub_agent_names=[])
        self.brain.event_log.log_event = AsyncMock()
        self.brain.event_log.mark_resolved = AsyncMock()
        self.stato = {
            "messages": [], "readings": [], "recent_events": [], "next_agent": "brain",
            "hitl_required": False, "config": {}, "pending_escalations": [],
        }

    async def processa(self, escalation):
        return await self.brain.process({**self.stato, "pending_escalations": [escalation]}, [], [], [escalation])

    async def test_troubleshooting_riuscito_chiude_il_ciclo(self):
        self.brain.ask_brain = AsyncMock(return_value="DIAGNOSI: transitorio\nDECISIONE: RETRY")

        risultato = await self.processa(escalation_da_guasto("figlio_b1"))

        self.assertEqual((risultato["next_agent"], risultato["pending_escalations"]), ("END", []))
        self.assertEqual(await self.tool.get_tool_value(), "ON")
        self.assertIn("risolto con troubleshooting", risultato["messages"][0].content)

    async def test_troubleshooting_fallito_ripassa_dal_brain_prima_di_chiamare_l_operatore(self):
        self.tool.imposta_guasto("hardware guasto")
        self.brain.ask_brain = AsyncMock(return_value="DIAGNOSI: guasto permanente\nDECISIONE: ESCALATE")
        esc = escalation_da_guasto("figlio_b2")

        with patch("langgraph.types.interrupt") as interrupt:
            risultato = await self.processa(esc)

        interrupt.assert_not_called()
        self.assertEqual(risultato["next_agent"], "brain")
        aggiornata = risultato["pending_escalations"][0]
        self.assertEqual(aggiornata["id"], esc["id"])
        self.assertEqual(aggiornata["tool_result"]["diagnosis"], "guasto permanente")
        self.assertIn("Brain", {t["agent"] for t in aggiornata["tool_result"]["attempts"]})

    async def test_secondo_passaggio_chiede_l_intervento_umano_senza_rifare_la_diagnosi(self):
        self.tool.imposta_guasto("hardware guasto")
        self.brain.ask_brain = AsyncMock(return_value="DIAGNOSI: guasto permanente\nDECISIONE: ESCALATE")
        primo = await self.processa(escalation_da_guasto("figlio_b3"))
        self.brain.ask_brain.reset_mock()

        with patch("langgraph.types.interrupt", return_value={"decision": "APPROVA", "reasoning": "preso in carico"}) as interrupt:
            risultato = await self.processa(primo["pending_escalations"][0])

        self.brain.ask_brain.assert_not_awaited()
        payload = interrupt.call_args.args[0]
        self.assertEqual(payload["type"], "tool_failure_human_intervention")
        self.assertEqual(payload["device_name"], "lampada")
        self.assertEqual(payload["diagnosis"], "guasto permanente")
        self.assertEqual((risultato["next_agent"], risultato["pending_escalations"]), ("END", []))
        self.assertIn("TOOL_FAILURE_HANDLED", azioni_registrate(self.brain))
        self.brain.event_log.mark_resolved.assert_awaited_with("lampada")

    async def test_override_dell_operatore_esegue_la_direttiva_sul_dispositivo(self):
        esc = escalation_da_guasto("figlio_b4")
        esc["tool_result"]["attempts"].append({"agent": "Brain", "phase": "troubleshooting"})
        self.brain._execute_semantic_override = AsyncMock(return_value=["[Brain_Override] eseguito"])

        with patch("langgraph.types.interrupt", return_value={"decision": "OVERRIDE", "reasoning": "accendi la lampada"}):
            risultato = await self.processa(esc)

        self.brain._execute_semantic_override.assert_awaited_once()
        self.assertEqual(self.brain._execute_semantic_override.await_args.kwargs["fallback_target"], "lampada")
        self.assertIn("eseguito", risultato["messages"][0].content)

    async def test_approvazione_con_dispositivo_guasto_diventa_escalation_di_guasto(self):
        self.tool.imposta_guasto("attuatore bloccato")
        self.brain.ask_brain = AsyncMock(return_value="DECISIONE: APPROVA\nMOTIVAZIONE: ok")
        origine = AgenteDiProva("figlio_b5", ["lampada"], 30, 1.0).create_escalation("lampada", "ON", "richiesta")

        risultato = await self.processa(origine)

        self.assertEqual(risultato["next_agent"], "brain")
        nuovo = [e for e in risultato["pending_escalations"] if not e.get("resolved")][0]
        self.assertEqual(nuovo["tool_result"]["response"], "attuatore bloccato")
        self.assertIn({**origine, "resolved": True}, risultato["pending_escalations"])
        self.brain.event_log.mark_resolved.assert_not_awaited()


class EndpointToolGuastoTest(unittest.TestCase):
    """Nessun lifespan: gli endpoint lavorano sulla mappa dei tool patchata, senza toccare il DB reale."""

    def setUp(self):
        from app.api.main import app

        self.tool = IoTDeviceTool("lampada", "OFF")
        for bersaglio in (
            patch("app.api.main._shared_tools", {"lampada": self.tool}),
            patch.dict(os.environ, {"API_KEY": ""}),
        ):
            bersaglio.start()
            self.addCleanup(bersaglio.stop)
        self.client = TestClient(app)

    def test_scrittura_su_dispositivo_guasto_risponde_502_con_il_motivo(self):
        self.tool.imposta_guasto("controller non risponde")
        risposta = self.client.post("/tools", json={"target": "lampada", "value": "ON"})
        self.assertEqual(risposta.status_code, 502)
        corpo = risposta.json()
        self.assertEqual((corpo["device_name"], corpo["success"], corpo["response"]), ("lampada", False, "controller non risponde"))

    def test_lettura_su_dispositivo_guasto_risponde_502(self):
        self.tool.imposta_guasto("sensore offline")
        self.assertEqual(self.client.get("/tools/lampada").status_code, 502)

    def test_elenco_dei_tool_segnala_l_errore_senza_fallire(self):
        self.tool.imposta_guasto("sensore offline")
        risposta = self.client.get("/tools")
        self.assertEqual(risposta.status_code, 200)
        self.assertEqual(risposta.json()["lampada"]["error"], "sensore offline")

    def test_endpoint_di_simulazione_imposta_e_rimuove_il_guasto(self):
        impostato = self.client.post("/tools/lampada/fault", json={"fault": "disturbo", "operations": 2}).json()
        self.assertEqual((impostato["fault"], impostato["operations"]), ("disturbo", 2))
        self.assertEqual(self.client.post("/tools", json={"target": "lampada", "value": "ON"}).status_code, 502)

        self.client.post("/tools/lampada/fault", json={"fault": None})
        self.assertEqual(self.client.post("/tools", json={"target": "lampada", "value": "ON"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
