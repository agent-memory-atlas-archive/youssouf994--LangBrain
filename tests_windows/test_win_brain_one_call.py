"""
Il Brain decide tutte le escalation che riceve con una sola chiamata al modello per ciclo, e chiede all'operatore
(interrupt) prima di qualsiasi effetto: alla ripresa nessuna chiamata o comando viene ripetuto.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.core.errori_llm import ErroreLLM
from app.graph.orchestrator import BrainAgent
from app.graph.state import EscalationItem
from app.tools.sensor_tools import IoTDeviceTool


class Pausa(Exception):
    """Simula l'interrupt di LangGraph: il nodo si ferma lì e verrà rieseguito alla ripresa."""


def escalation(target: str, source: str = "organo") -> dict:
    return EscalationItem(source_agent=source, target_device=target, proposed_action="ON", reason=f"motivo {target}",
                          conflict_detected=True).model_dump()


def brain(risposta: str = ""):
    tools = [IoTDeviceTool("lampada", "OFF"), IoTDeviceTool("ventola", "OFF")]
    b = BrainAgent(tools=tools, sub_agent_names=[])
    b.event_log.log_event = AsyncMock()
    b.event_log.get_recent_events = AsyncMock(return_value=[])
    b.event_log.mark_resolved = AsyncMock()
    b.ask_brain = AsyncMock(return_value=risposta)
    return b, {t.target_device: t for t in tools}


async def esegui(b, escalations, eventi=None):
    stato = {"messages": [], "readings": [], "recent_events": [], "next_agent": "brain", "hitl_required": False,
             "config": {}, "pending_escalations": escalations}
    return await b.process(stato, eventi or [], [], escalations)


class BrainOneCallTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        senza_hitl = patch("app.graph.orchestrator.flusso_brain_attivo", return_value=False)
        senza_hitl.start()
        self.addCleanup(senza_hitl.stop)

    async def test_several_escalations_one_call(self):
        b, tools = brain("ESCALATION: 1\nDECISIONE: APPROVA\nMOTIVAZIONE: ok\n\nESCALATION: 2\nDECISIONE: RESPINGI\nMOTIVAZIONE: no")
        risultato = await esegui(b, [escalation("lampada"), escalation("ventola")])
        b.ask_brain.assert_awaited_once()
        self.assertEqual(await tools["lampada"].get_tool_value(), "ON")
        self.assertEqual(await tools["ventola"].get_tool_value(), "REJECTED")
        self.assertEqual(risultato["next_agent"], "END")
        sistema, utente = b.ask_brain.await_args.args[:2]
        self.assertIn("ESCALATION: <", sistema)
        self.assertIn("ESCALATION: 1", utente)
        self.assertIn("ESCALATION: 2", utente)

    async def test_the_history_is_sent_once(self):
        eventi = [{"target": "x", "action": "EVENTO_UNICO_123", "actor": "a"}]
        b, _ = brain("ESCALATION: 1\nDECISIONE: RESPINGI\nMOTIVAZIONE: -\nESCALATION: 2\nDECISIONE: RESPINGI\nMOTIVAZIONE: -")
        await esegui(b, [escalation("lampada"), escalation("ventola")], eventi)
        self.assertEqual(b.ask_brain.await_args.args[1].count("EVENTO_UNICO_123"), 1)

    async def test_one_escalation_keeps_the_single_format(self):
        b, tools = brain("DECISIONE: APPROVA\nMOTIVAZIONE: ok")
        await esegui(b, [escalation("lampada")])
        b.ask_brain.assert_awaited_once()
        self.assertNotIn("ESCALATION:", b.ask_brain.await_args.args[0])
        self.assertEqual(await tools["lampada"].get_tool_value(), "ON")

    async def test_an_incomplete_answer_commands_no_device(self):
        b, tools = brain("ESCALATION: 1\nDECISIONE: APPROVA\nMOTIVAZIONE: ok")
        with self.assertRaises(ErroreLLM) as ctx:
            await esegui(b, [escalation("lampada"), escalation("ventola")])
        self.assertEqual(ctx.exception.codice, "RISPOSTA_NON_UTILIZZABILE")
        self.assertEqual((await tools["lampada"].get_tool_value(), await tools["ventola"].get_tool_value()), ("OFF", "OFF"))
        b.event_log.log_event.assert_not_awaited()


class BrainHumanFirstTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        con_hitl = patch("app.graph.orchestrator.flusso_brain_attivo", return_value=True)
        con_hitl.start()
        self.addCleanup(con_hitl.stop)

    async def test_the_operator_is_asked_before_any_call_or_command(self):
        b, tools = brain("DECISIONE: APPROVA\nMOTIVAZIONE: ok")
        # ventola decided by the model (first in the queue), lampada needs the operator (second).
        with patch.object(b, "_richiede_approvazione_umana", side_effect=lambda stato, t: t == "lampada"), \
                patch("langgraph.types.interrupt", side_effect=Pausa):
            with self.assertRaises(Pausa):
                await esegui(b, [escalation("ventola"), escalation("lampada")])
        b.ask_brain.assert_not_awaited()
        self.assertEqual(await tools["ventola"].get_tool_value(), "OFF")

    async def test_after_the_answer_one_call_for_the_rest(self):
        b, tools = brain("DECISIONE: APPROVA\nMOTIVAZIONE: ok")
        with patch.object(b, "_richiede_approvazione_umana", side_effect=lambda stato, t: t == "lampada"), \
                patch("langgraph.types.interrupt", return_value={"decision": "RESPINGI", "reasoning": "no"}):
            await esegui(b, [escalation("ventola"), escalation("lampada")])
        b.ask_brain.assert_awaited_once()
        self.assertIn("ventola", b.ask_brain.await_args.args[1])
        self.assertNotIn("lampada", b.ask_brain.await_args.args[1])
        self.assertEqual(await tools["ventola"].get_tool_value(), "ON")
        self.assertEqual(await tools["lampada"].get_tool_value(), "REJECTED")

    async def test_all_human_decisions_no_call(self):
        b, tools = brain("")
        with patch.object(b, "_richiede_approvazione_umana", return_value=True), \
                patch("langgraph.types.interrupt", return_value={"decision": "APPROVA", "reasoning": "sì"}):
            await esegui(b, [escalation("ventola"), escalation("lampada")])
        b.ask_brain.assert_not_awaited()
        self.assertEqual((await tools["lampada"].get_tool_value(), await tools["ventola"].get_tool_value()), ("ON", "ON"))


if __name__ == "__main__":
    unittest.main()
