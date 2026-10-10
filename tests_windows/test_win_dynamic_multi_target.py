"""
DynamicAgent con più target: una sola chiamata al modello per ciclo decide tutti i target che lo richiedono; ogni
target ha il proprio esito; una risposta incompleta non comanda nessun dispositivo.
"""

import unittest
from unittest.mock import AsyncMock

from app.agents.dynamic_agent import DynamicAgent
from app.core.errori_llm import ErroreLLM
from app.tools.sensor_tools import IoTDeviceTool


def agente(risposta: str, lampada: str = "OFF", ventola: str = "OFF", eventi: list[dict] | None = None, **kw):
    tools = {"lampada": IoTDeviceTool("lampada", lampada), "ventola": IoTDeviceTool("ventola", ventola)}
    a = DynamicAgent("organo_casa", ["lampada", "ventola"], tools=tools, **kw)
    a.event_log.get_recent_events = AsyncMock(return_value=eventi or [])
    a.event_log.log_event = AsyncMock()
    a.ask_brain = AsyncMock(return_value=risposta)
    return a, tools


async def valore(tools, nome):
    return await tools[nome].get_tool_value()


class MultiTargetTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_call_decides_every_target(self):
        a, tools = agente("TARGET: lampada\nDECISIONE: ACTION\nMOTIVAZIONE: buio\n\nTARGET: ventola\nDECISIONE: NONE\nMOTIVAZIONE: fresco")
        risultato = await a.process({"config": {}}, [], [], [])
        a.ask_brain.assert_awaited_once()
        self.assertEqual((await valore(tools, "lampada"), await valore(tools, "ventola")), ("ON", "OFF"))
        self.assertNotIn("pending_escalations", risultato)
        self.assertEqual(risultato["next_agent"], "brain")

    async def test_the_second_target_is_no_longer_ignored(self):
        a, tools = agente("TARGET: lampada\nDECISIONE: NONE\nMOTIVAZIONE: ok\nTARGET: ventola\nDECISIONE: ACTION\nMOTIVAZIONE: caldo")
        await a.process({"config": {}}, [], [], [])
        self.assertEqual(await valore(tools, "ventola"), "ON")

    async def test_each_target_has_its_own_outcome(self):
        a, tools = agente("**TARGET:** Lampada\nDECISION: escalate\nREASON: strano\nTARGET: ventola\nDECISION: ACTION\nREASON: caldo")
        risultato = await a.process({"config": {}}, [], [], [])
        [esc] = risultato["pending_escalations"]
        self.assertEqual(esc["target_device"], "lampada")
        self.assertIn("strano", esc["reason"])
        self.assertEqual((await valore(tools, "lampada"), await valore(tools, "ventola")), ("OFF", "ON"))

    async def test_an_incomplete_answer_commands_no_device(self):
        a, tools = agente("TARGET: lampada\nDECISIONE: ACTION\nMOTIVAZIONE: buio")
        with self.assertRaises(ErroreLLM) as ctx:
            await a.process({"config": {}}, [], [], [])
        self.assertEqual(ctx.exception.codice, "RISPOSTA_NON_UTILIZZABILE")
        self.assertEqual(await valore(tools, "lampada"), "OFF")

    async def test_an_answer_without_blocks_is_refused_with_several_targets(self):
        a, tools = agente("DECISIONE: ACTION\nMOTIVAZIONE: accendi tutto")
        with self.assertRaises(ErroreLLM):
            await a.process({"config": {}}, [], [], [])
        self.assertEqual((await valore(tools, "lampada"), await valore(tools, "ventola")), ("OFF", "OFF"))

    async def test_a_locked_target_is_not_asked_to_the_model(self):
        a, tools = agente("DECISIONE: ACTION\nMOTIVAZIONE: caldo", lampada="REJECTED")
        await a.process({"config": {}}, [], [], [])
        prompt_utente = a.ask_brain.await_args.args[1]
        self.assertIn("ventola", prompt_utente)
        self.assertNotIn("lampada", prompt_utente)
        self.assertEqual((await valore(tools, "lampada"), await valore(tools, "ventola")), ("REJECTED", "ON"))

    async def test_no_call_when_no_target_needs_a_decision(self):
        conflitto = [{"target": "ventola", "action": "SECURITY_LOCK", "actor": "agent_security", "timestamp": "2026-10-10 10:00:00"}]
        a, tools = agente("", lampada="REJECTED", eventi=conflitto)
        risultato = await a.process({"config": {}}, conflitto, [], [])
        a.ask_brain.assert_not_awaited()
        [esc] = risultato["pending_escalations"]
        self.assertEqual(esc["target_device"], "ventola")

    async def test_the_user_prompt_lists_the_state_of_each_target(self):
        a, _ = agente("TARGET: lampada\nDECISIONE: NONE\nMOTIVAZIONE: -\nTARGET: ventola\nDECISIONE: NONE\nMOTIVAZIONE: -",
                      ventola="ON")
        await a.process({"config": {}}, [], [], [])
        self.assertIn("lampada: OFF; ventola: ON", a.ask_brain.await_args.args[1])

    def test_a_custom_prompt_with_the_single_format_gets_the_block_format(self):
        custom = "Sei la casa.\nDECISION: [ACTION|ESCALATE|NONE]"
        a, _ = agente("", system_prompt_template=custom)
        self.assertIn("TARGET:", a.system_prompt_template)
        self.assertTrue(a.system_prompt_template.startswith("Sei la casa."))
        single = DynamicAgent("solo", ["lampada"], system_prompt_template=custom, tools={})
        self.assertEqual(single.system_prompt_template, custom)


if __name__ == "__main__":
    unittest.main()
