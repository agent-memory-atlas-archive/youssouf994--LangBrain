import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.agents.base_agent import BaseAgent
from app.agents.dynamic_agent import DynamicAgent
from app.core.priorita import priorita_attore, registra_priorita, trova_blocco_prevalente
from app.tools.tool_wrapper import execute_tool_safely


def adesso(minuti_fa=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minuti_fa)).strftime("%Y-%m-%d %H:%M:%S")


def evento(attore, azione="DEVICE_OFF", nuovo="OFF", target="lampada", minuti_fa=0):
    return {"actor": attore, "action": azione, "target": target, "new_value": nuovo, "timestamp": adesso(minuti_fa)}


class ToolFinto:
    def __init__(self, valore="OFF"):
        self.valore = valore

    async def get_tool_value(self):
        return self.valore

    async def set_tool_value(self, valore):
        self.valore = valore


class AgenteDiProva(BaseAgent):
    async def process(self, state, recent_events, relevant_readings, agent_escalations):
        return {}


def crea_agente(nome, peso, eventi):
    agente = AgenteDiProva(nome, ["lampada"], 30, peso)
    agente.event_log.get_recent_events = AsyncMock(return_value=eventi)
    agente.event_log.log_event = AsyncMock()
    return agente


class TrovaBloccoPrevalenteTest(unittest.TestCase):
    def setUp(self):
        registra_priorita("prova_sicurezza", 500.0)
        registra_priorita("prova_luci", 1.0)
        registra_priorita("prova_pari_a", 10.0)
        registra_priorita("prova_pari_b", 10.0)

    def cerca(self, eventi, richiedente="prova_luci", peso=1.0, ttl=30):
        return trova_blocco_prevalente(eventi, "lampada", richiedente, peso, ttl)

    def test_blocco_di_priorita_maggiore_prevale(self):
        self.assertIn("500.0 > 1.0", self.cerca([evento("prova_sicurezza")]))

    def test_blocco_di_priorita_minore_non_prevale(self):
        self.assertIsNone(self.cerca([evento("prova_luci")], richiedente="prova_sicurezza", peso=500.0))

    def test_a_parita_di_priorita_il_blocco_non_prevale(self):
        self.assertIsNone(self.cerca([evento("prova_pari_a")], richiedente="prova_pari_b", peso=10.0))

    def test_il_brain_prevale_su_qualsiasi_agente(self):
        self.assertIsNotNone(self.cerca([evento("Brain")], richiedente="prova_sicurezza", peso=500.0))

    def test_attore_sconosciuto_prevale_per_prudenza(self):
        motivo = self.cerca([evento("operatore_umano", azione="SECURITY_LOCK", nuovo="LOCKED")])
        self.assertIn("sconosciuta", motivo)

    def test_azioni_di_blocco_prevalgono_anche_senza_valore_off(self):
        self.assertIsNotNone(self.cerca([evento("prova_sicurezza", azione="SECURITY_LOCK", nuovo="LOCKED")]))

    def test_valore_ordinario_non_e_un_blocco(self):
        self.assertIsNone(self.cerca([evento("prova_sicurezza", azione="SET_LEVEL", nuovo="ON")]))

    def test_eventi_propri_o_di_altri_target_vengono_ignorati(self):
        eventi = [evento("prova_luci"), evento("prova_sicurezza", target="altro_dispositivo")]
        self.assertIsNone(self.cerca(eventi))

    def test_eventi_chiusi_o_scaduti_non_bloccano(self):
        eventi = [
            evento("prova_sicurezza", azione="RESOLVED_SECURITY_LOCK"),
            evento("prova_sicurezza", azione="UNBLOCKED"),
            evento("prova_sicurezza", minuti_fa=120),
        ]
        self.assertIsNone(self.cerca(eventi))

    def test_priorita_note_dei_soli_agenti_istanziati(self):
        AgenteDiProva("prova_nativo", ["lampada"], 30, 700.0)
        self.assertEqual(priorita_attore("prova_nativo"), 700.0)
        self.assertEqual(priorita_attore("Brain"), float("inf"))
        self.assertIsNone(priorita_attore("prova_mai_visto"))


class ApplyStatusPrioritaTest(unittest.IsolatedAsyncioTestCase):
    async def test_agente_a_bassa_priorita_viene_respinto_e_il_rifiuto_e_registrato(self):
        AgenteDiProva("prova_sicurezza_2", ["lampada"], 30, 500.0)
        agente = crea_agente("prova_luci_2", 1.0, [evento("prova_sicurezza_2")])
        tool = ToolFinto("OFF")

        applicato = await agente.apply_status("lampada", "TURN_ON", "ON", "test", tools_map={"lampada": tool})

        self.assertFalse(applicato)
        self.assertEqual(tool.valore, "OFF")
        registrato = agente.event_log.log_event.await_args.kwargs
        self.assertEqual(registrato["action"], "REJECTED_TURN_ON")
        self.assertIn("500.0 > 1.0", registrato["reasoning"])

    async def test_agente_ad_alta_priorita_non_e_bloccato_da_un_blocco_minore(self):
        AgenteDiProva("prova_luci_3", ["lampada"], 30, 1.0)
        agente = crea_agente("prova_sicurezza_3", 500.0, [evento("prova_luci_3")])
        tool = ToolFinto("OFF")

        applicato = await agente.apply_status("lampada", "TURN_ON", "ON", "test", tools_map={"lampada": tool})

        self.assertTrue(applicato)
        self.assertEqual(tool.valore, "ON")

    async def test_execute_tool_safely_rispetta_actor_priority(self):
        registra_priorita("prova_intermedio", 50.0)
        log = AgenteDiProva("prova_supporto", ["lampada"], 30, 1.0).event_log
        log.get_recent_events = AsyncMock(return_value=[evento("prova_intermedio")])

        tool_basso = ToolFinto("OFF")
        ok_basso, motivo = await execute_tool_safely("prova_basso", 1.0, "lampada", tool_basso, "ON", event_log=log)
        tool_alto = ToolFinto("OFF")
        ok_alto, _ = await execute_tool_safely("prova_alto", 900.0, "lampada", tool_alto, "ON", event_log=log)

        self.assertFalse(ok_basso)
        self.assertIn("50.0 > 1.0", motivo)
        self.assertEqual(tool_basso.valore, "OFF")
        self.assertTrue(ok_alto)
        self.assertEqual(tool_alto.valore, "ON")


class DynamicAgentPrioritaTest(unittest.IsolatedAsyncioTestCase):
    def crea(self, nome, peso, eventi, tool):
        agente = DynamicAgent(nome, ["lampada"], parent_agent_name="Brain", priority_weight=peso, tools={"lampada": tool})
        agente.event_log.get_recent_events = AsyncMock(return_value=eventi)
        agente.event_log.log_event = AsyncMock()
        agente.ask_brain = AsyncMock(return_value="DECISIONE: ACTION\nMOTIVAZIONE: accendere")
        return agente

    async def test_azione_bloccata_per_priorita_diventa_escalation_verso_il_padre(self):
        AgenteDiProva("prova_sicurezza_4", ["lampada"], 30, 500.0)
        tool = ToolFinto("OFF")
        agente = self.crea("prova_luci_4", 1.0, [evento("prova_sicurezza_4")], tool)

        risultato = await agente.process({"config": {}}, [evento("prova_sicurezza_4")], [], [])

        self.assertEqual(tool.valore, "OFF")
        self.assertEqual(risultato["next_agent"], "brain")
        self.assertEqual(len(risultato["pending_escalations"]), 1)
        self.assertEqual(risultato["pending_escalations"][0]["source_agent"], "prova_luci_4")

    async def test_azione_consentita_viene_eseguita_senza_escalation(self):
        AgenteDiProva("prova_luci_5", ["lampada"], 30, 1.0)
        tool = ToolFinto("OFF")
        agente = self.crea("prova_sicurezza_5", 500.0, [evento("prova_luci_5")], tool)

        risultato = await agente.process({"config": {}}, [evento("prova_luci_5")], [], [])

        self.assertEqual(tool.valore, "ON")
        self.assertNotIn("pending_escalations", risultato)


if __name__ == "__main__":
    unittest.main()
