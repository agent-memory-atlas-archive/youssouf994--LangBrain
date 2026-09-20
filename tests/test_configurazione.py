import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.agents.base_agent import BaseAgent
from app.agents.dynamic_agent import DynamicAgent
from app.core import configurazione as modulo
from app.core.configurazione import (
    Configurazione, ErroreConfigurazione, carica_configurazione, costruisci_configurazione, get_configurazione,
    imposta_configurazione,
)
from app.graph.orchestrator import BrainAgent, _normalize_action_value, _tool_value_catalog
from app.tools.sensor_tools import IoTDeviceTool
from app.tools.tool_wrapper import execute_tool_safely, force_execute_tool

RADICE = Path(__file__).resolve().parents[1]


def configurazione_di_prova(**extra):
    dati = {
        "politica": {"dispositivi_non_elencati": "rifiuta"},
        "dispositivi": {
            "front_door_lock": {"valori": ["LOCKED", "UNLOCKED"], "valore_attivo": "LOCKED"},
            "ac_living_room": {"valori": ["OFF"], "intervallo": [10, 35], "unita": "°C", "valore_attivo": "22.5°C"},
            "device_l*": {"valori": ["ON", "OFF"]},
            "device_l7": {"valori": ["OPEN", "CLOSED"]},
        },
        **extra,
    }
    return costruisci_configurazione(dati)


class ConfigurazioneDiProvaMixin:
    def usa_configurazione(self, configurazione=None):
        imposta_configurazione(configurazione or configurazione_di_prova())
        self.addCleanup(imposta_configurazione, None)


class LetturaConfigurazioneTest(unittest.TestCase):
    def test_il_file_fornito_e_valido_e_copre_i_dispositivi_noti(self):
        cfg = carica_configurazione(RADICE / "configurazione.toml")

        self.assertEqual(cfg.dispositivi_non_elencati, "rifiuta")
        for dispositivo, valore in [
            ("front_door_lock", "LOCKED"), ("alarm_system", "ARMED"), ("ac_living_room", "22.5°C"),
            ("ac_living_room", "30.0"), ("living_room_lights", "50%"), ("cardiac_pacemaker", 72.0),
            ("oxygen_regulator", "98.5%"), ("device_l3", "ON"), ("vault_door", "OPEN"),
        ]:
            with self.subTest(dispositivo=dispositivo, valore=valore):
                self.assertTrue(cfg.valida_comando(dispositivo, valore).ammesso)

    def test_il_file_fornito_rifiuta_valori_e_dispositivi_non_previsti(self):
        cfg = carica_configurazione(RADICE / "configurazione.toml")
        for dispositivo, valore in [
            ("front_door_lock", "ON"), ("front_door_lock", "FORCE_SHUTDOWN"), ("ac_living_room", "80°C"),
            ("cardiac_pacemaker", 500), ("device_l3", "OPEN"), ("frigorifero", "ON"),
        ]:
            with self.subTest(dispositivo=dispositivo, valore=valore):
                self.assertFalse(cfg.valida_comando(dispositivo, valore).ammesso)

    def test_i_valori_di_attivazione_del_file_fornito_sono_essi_stessi_ammessi(self):
        cfg = carica_configurazione(RADICE / "configurazione.toml")
        for regola in cfg.regole:
            if regola.valore_attivo:
                self.assertTrue(cfg.valida_comando(regola.nome, regola.valore_attivo).ammesso, regola.nome)

    def test_il_file_fornito_elenca_solo_modelli_di_ripiego_gratuiti_o_economici(self):
        cfg = carica_configurazione(RADICE / "configurazione.toml")
        self.assertTrue(all(m.endswith(":free") for m in cfg.modelli_di_ripiego["openrouter"]))
        self.assertFalse(cfg.llm_fallback)

    def test_configurazioni_non_valide_vengono_rifiutate_con_un_messaggio(self):
        casi = [
            {"politica": {"dispositivi_non_elencati": "forse"}},
            {"llm": {"fallback": "si"}},
            {"dispositivi": {"x": {"valori": "ON"}}},
            {"dispositivi": {"x": {"intervallo": [5]}}},
            {"dispositivi": {"x": {"intervallo": [9, 1]}}},
            {"dispositivi": {"x": {"valori": ["ON"], "valore_attivo": "OFF"}}},
            {"dispositivi": {"x": "ON"}},
        ]
        for dati in casi:
            with self.subTest(dati=dati), self.assertRaises(ErroreConfigurazione):
                costruisci_configurazione(dati)

    def test_file_mancante_usa_i_default_permissivi_con_avviso(self):
        with self.assertLogs("app.core.configurazione", level="WARNING"):
            cfg = carica_configurazione("/percorso/che/non/esiste.toml")
        self.assertEqual(cfg.dispositivi_non_elencati, "consenti")
        self.assertTrue(cfg.valida_comando("qualsiasi", "QUALSIASI").ammesso)

    def test_toml_malformato_solleva_errore_di_configurazione(self):
        with tempfile.TemporaryDirectory() as cartella:
            file = Path(cartella) / "c.toml"
            file.write_text("[dispositivi\nvalori = ")
            with self.assertRaises(ErroreConfigurazione):
                carica_configurazione(file)


class ValidazioneComandiTest(unittest.TestCase):
    def setUp(self):
        self.cfg = configurazione_di_prova()

    def test_forma_canonica_del_valore(self):
        self.assertEqual(self.cfg.valida_comando("front_door_lock", " locked ").valore, "LOCKED")

    def test_intervallo_con_e_senza_unita_e_virgola_decimale(self):
        for valore in ("22.5°C", "22.5", "22,5 °C", 22.5, "10", "35"):
            with self.subTest(valore=valore):
                self.assertTrue(self.cfg.valida_comando("ac_living_room", valore).ammesso)
        for valore in ("9.9", "35.1°C", "caldo", "22.5 %"):
            with self.subTest(valore=valore):
                self.assertFalse(self.cfg.valida_comando("ac_living_room", valore).ammesso)

    def test_il_nome_esatto_ha_la_precedenza_sul_pattern(self):
        self.assertTrue(self.cfg.valida_comando("device_l7", "OPEN").ammesso)
        self.assertFalse(self.cfg.valida_comando("device_l7", "ON").ammesso)
        self.assertTrue(self.cfg.valida_comando("device_l8", "ON").ammesso)

    def test_dispositivo_non_elencato_segue_la_politica(self):
        self.assertFalse(self.cfg.valida_comando("frigorifero", "ON").ammesso)
        self.assertIn("non è elencato", self.cfg.valida_comando("frigorifero", "ON").motivo)
        permissiva = Configurazione(dispositivi_non_elencati="consenti")
        self.assertTrue(permissiva.valida_comando("frigorifero", "ON").ammesso)

    def test_i_flag_interni_non_vengono_validati(self):
        for flag in ("REJECTED", "RECONCILED_FORCE_SHUTDOWN", "BLOCKED", "INVALID_COMMAND_X"):
            self.assertTrue(self.cfg.valida_comando("front_door_lock", flag).ammesso, flag)

    def test_valore_mancante_e_rifiutato(self):
        self.assertFalse(self.cfg.valida_comando("front_door_lock", None).ammesso)
        self.assertFalse(self.cfg.valida_comando("front_door_lock", "  ").ammesso)

    def test_il_motivo_indica_i_valori_ammessi(self):
        motivo = self.cfg.valida_comando("front_door_lock", "ON").motivo
        self.assertIn("LOCKED", motivo)
        self.assertIn("UNLOCKED", motivo)

    def test_sezione_senza_vincoli_accetta_qualsiasi_valore(self):
        cfg = costruisci_configurazione({"dispositivi": {"libero": {}}, "politica": {"dispositivi_non_elencati": "rifiuta"}})
        self.assertTrue(cfg.valida_comando("libero", "QUALUNQUE").ammesso)
        self.assertIsNone(cfg.descrizione_valori("libero"))

    def test_valore_attivo_e_descrizione(self):
        self.assertEqual(self.cfg.valore_attivo("front_door_lock"), "LOCKED")
        self.assertEqual(self.cfg.valore_attivo("device_l3"), "ON")
        self.assertIn("numero tra 10 e 35 °C", self.cfg.descrizione_valori("ac_living_room"))


class RicaricaConfigurazioneTest(unittest.TestCase):
    def setUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.file = Path(self._cartella.name) / "c.toml"
        self.scrivi('[politica]\ndispositivi_non_elencati = "rifiuta"\n[dispositivi.a]\nvalori = ["ON"]\n')
        self.enterContext(patch.dict(os.environ, {"LANGBRAIN_CONFIG": str(self.file)}))
        imposta_configurazione(None)
        self.addCleanup(imposta_configurazione, None)

    def scrivi(self, testo):
        self.file.write_text(testo)
        stat = self.file.stat()
        os.utime(self.file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000 * (1 + getattr(self, "_n", 0))))
        self._n = getattr(self, "_n", 0) + 1

    def test_il_file_modificato_viene_riletto_senza_riavvio(self):
        self.assertFalse(get_configurazione().valida_comando("a", "OFF").ammesso)
        self.scrivi('[politica]\ndispositivi_non_elencati = "rifiuta"\n[dispositivi.a]\nvalori = ["ON", "OFF"]\n')
        self.assertTrue(get_configurazione().valida_comando("a", "OFF").ammesso)

    def test_una_modifica_errata_lascia_attiva_la_versione_precedente(self):
        self.assertTrue(get_configurazione().valida_comando("a", "ON").ammesso)
        self.scrivi("[dispositivi.a\nrotto")
        with self.assertLogs("app.core.configurazione", level="ERROR"):
            attuale = get_configurazione()
        self.assertTrue(attuale.valida_comando("a", "ON").ammesso)


class AttuazioneDegliAgentiTest(ConfigurazioneDiProvaMixin, unittest.IsolatedAsyncioTestCase):
    def crea_agente(self, nome="agente_cfg"):
        class Agente(BaseAgent):
            async def process(self, *a):
                return {}

        agente = Agente(nome, ["front_door_lock"], 30, 1.0)
        agente.event_log.get_recent_events = AsyncMock(return_value=[])
        agente.event_log.log_event = AsyncMock()
        return agente

    async def test_valore_non_ammesso_non_raggiunge_il_dispositivo_e_viene_registrato(self):
        self.usa_configurazione()
        agente = self.crea_agente()
        tool = IoTDeviceTool("front_door_lock", "LOCKED")

        risultato = await agente.applica_stato("front_door_lock", "TURN_ON", "ON", "t", tools_map={"front_door_lock": tool})

        self.assertEqual((risultato["status"], risultato["success"]), ("COMMAND_NOT_ALLOWED", False))
        self.assertIn("LOCKED", risultato["response"])
        self.assertEqual(await tool.get_tool_value(), "LOCKED")
        self.assertEqual(agente.event_log.log_event.await_args.kwargs["action"], "INVALID_COMMAND_TURN_ON")

    async def test_il_valore_ammesso_viene_inviato_nella_forma_canonica(self):
        self.usa_configurazione()
        agente = self.crea_agente()
        tool = IoTDeviceTool("front_door_lock", "LOCKED")

        risultato = await agente.applica_stato("front_door_lock", "SET", "unlocked", "t", tools_map={"front_door_lock": tool})

        self.assertEqual(risultato["status"], "APPLIED")
        self.assertEqual(await tool.get_tool_value(), "UNLOCKED")

    async def test_dispositivo_non_elencato_non_viene_comandato(self):
        self.usa_configurazione()
        agente = self.crea_agente()
        tool = IoTDeviceTool("frigorifero", "OFF")
        risultato = await agente.applica_stato("frigorifero", "TURN_ON", "ON", "t", tools_map={"frigorifero": tool})
        self.assertEqual(risultato["status"], "COMMAND_NOT_ALLOWED")
        self.assertEqual(await tool.get_tool_value(), "OFF")

    async def test_execute_tool_safely_e_force_execute_rispettano_l_elenco(self):
        self.usa_configurazione()
        tool = IoTDeviceTool("front_door_lock", "LOCKED")
        log = self.crea_agente().event_log

        ok_sicuro, motivo_sicuro = await execute_tool_safely("Brain", 1000.0, "front_door_lock", tool, "ON", event_log=log)
        ok_forzato, motivo_forzato = await force_execute_tool("front_door_lock", tool, "OVERRIDE", "ON", event_log=log)

        self.assertFalse(ok_sicuro)
        self.assertFalse(ok_forzato)
        self.assertIn("non ammesso", motivo_sicuro)
        self.assertIn("non ammesso", motivo_forzato)
        self.assertEqual(await tool.get_tool_value(), "LOCKED")
        log.log_event.assert_not_awaited()

    async def test_dynamic_agent_usa_il_valore_attivo_del_dispositivo(self):
        self.usa_configurazione()
        tool = IoTDeviceTool("front_door_lock", "UNLOCKED")
        agente = DynamicAgent("agente_porta", ["front_door_lock"], tools={"front_door_lock": tool})
        agente.event_log.get_recent_events = AsyncMock(return_value=[])
        agente.event_log.log_event = AsyncMock()
        agente.ask_brain = AsyncMock(return_value="DECISIONE: ACTION\nMOTIVAZIONE: chiudere")

        await agente.process({"config": {}}, [], [], [])

        self.assertEqual(await tool.get_tool_value(), "LOCKED")

    async def test_dynamic_agent_con_valore_attivo_non_ammesso_fa_escalation_con_il_motivo(self):
        cfg = costruisci_configurazione({"politica": {"dispositivi_non_elencati": "rifiuta"}, "dispositivi": {"x": {"valori": ["A", "B"]}}})
        self.usa_configurazione(cfg)
        tool = IoTDeviceTool("x", "A")
        agente = DynamicAgent("agente_x", ["x"], tools={"x": tool})
        agente.event_log.get_recent_events = AsyncMock(return_value=[])
        agente.event_log.log_event = AsyncMock()
        agente.ask_brain = AsyncMock(return_value="DECISIONE: ACTION\nMOTIVAZIONE: agire")

        risultato = await agente.process({"config": {}}, [], [], [])

        self.assertEqual(await tool.get_tool_value(), "A")
        self.assertIn("non ammesso", risultato["pending_escalations"][0]["reason"])
        self.assertIn("Comando non ammesso", risultato["messages"][0].content)

    def test_i_cataloghi_per_l_llm_derivano_dalla_configurazione(self):
        self.usa_configurazione()
        catalogo = _tool_value_catalog({"front_door_lock": IoTDeviceTool("front_door_lock")})
        self.assertIn("['LOCKED', 'UNLOCKED']", catalogo)
        self.assertIn("front_door_lock: ['LOCKED', 'UNLOCKED']", DynamicAgent._voce_catalogo("front_door_lock"))

    def test_il_valore_di_ripiego_dell_override_e_quello_attivo_configurato(self):
        self.usa_configurazione()
        self.assertEqual(_normalize_action_value("front_door_lock", "UNBLOCK_AND_SET", None, "FORCE_SHUTDOWN"), "LOCKED")
        self.assertEqual(_normalize_action_value("device_l3", "UNBLOCK_AND_SET", None, "SECURITY_LOCK"), "ON")


class PromptPersonalizzatoTest(ConfigurazioneDiProvaMixin, unittest.TestCase):
    def test_un_prompt_senza_il_formato_viene_completato(self):
        self.usa_configurazione()
        agente = DynamicAgent("agente_prompt", ["front_door_lock"], system_prompt_template="Sei il Componente Serratura.")

        prompt = agente.system_prompt_template

        self.assertTrue(prompt.startswith("Sei il Componente Serratura."))
        self.assertIn("DECISIONE: [ACTION|ESCALATE|NONE]", prompt)
        self.assertIn("['LOCKED', 'UNLOCKED']", prompt)

    def test_un_prompt_che_descrive_gia_il_formato_resta_invariato(self):
        completo = "Sei un agente.\nRispondi nel formato:\nDECISIONE: [ACTION|NONE]"
        self.assertEqual(DynamicAgent("agente_prompt", ["x"], system_prompt_template=completo).system_prompt_template, completo)

    def test_senza_prompt_personalizzato_resta_quello_predefinito(self):
        self.assertIn("DECISIONE: [ACTION|ESCALATE|NONE]", DynamicAgent("agente_prompt", ["x"]).system_prompt_template)


class OverrideDispositiviNonElencatiTest(ConfigurazioneDiProvaMixin, unittest.IsolatedAsyncioTestCase):
    async def test_l_override_non_crea_ne_comanda_dispositivi_non_configurati(self):
        self.usa_configurazione()
        serratura = IoTDeviceTool("front_door_lock", "LOCKED")
        brain = BrainAgent(tools=[serratura])
        brain.ask_brain = AsyncMock(return_value=json.dumps([
            {"target": "dispositivo_inventato_dall_llm", "action": "TURN_ON", "value": None},
            {"target": "front_door_lock", "action": "UNBLOCK_AND_SET", "value": "UNLOCKED"},
        ]))
        brain.event_log.mark_resolved = AsyncMock()
        brain.event_log.log_event = AsyncMock()

        messaggi = await brain._execute_semantic_override("apri la porta e accendi qualcosa", "front_door_lock", "UNBLOCK_AND_SET")

        self.assertIn("RIFIUTATO", messaggi[0])
        self.assertNotIn("dispositivo_inventato_dall_llm", brain.tools)
        self.assertEqual(await serratura.get_tool_value(), "UNLOCKED")

    async def test_l_override_con_valore_non_ammesso_fallisce_senza_toccare_il_dispositivo(self):
        self.usa_configurazione()
        serratura = IoTDeviceTool("front_door_lock", "LOCKED")
        brain = BrainAgent(tools=[serratura])
        brain.ask_brain = AsyncMock(return_value='[{"target": "front_door_lock", "action": "UNBLOCK_AND_SET", "value": "ON"}]')
        brain.event_log.mark_resolved = AsyncMock()
        brain.event_log.log_event = AsyncMock()

        messaggi = await brain._execute_semantic_override("accendi la porta", "front_door_lock", "UNBLOCK_AND_SET")

        self.assertIn("FALLITO", messaggi[0])
        self.assertEqual(await serratura.get_tool_value(), "LOCKED")


class OverrideTooliCreatiDopoTest(ConfigurazioneDiProvaMixin, unittest.IsolatedAsyncioTestCase):
    async def test_il_brain_vede_e_comanda_i_tool_creati_dopo_la_sua_costruzione(self):
        from app.tools.sensor_tools import get_tool

        self.usa_configurazione()
        brain = BrainAgent(tools=[IoTDeviceTool("front_door_lock", "LOCKED")])
        creato_dopo = get_tool("device_l9", initial_value="OFF")
        brain.ask_brain = AsyncMock(return_value='[{"target": "device_l9", "action": "TURN_ON", "value": null}]')
        brain.event_log.mark_resolved = AsyncMock()
        brain.event_log.log_event = AsyncMock()

        messaggi = await brain._execute_semantic_override("accendi device_l9", "front_door_lock", "UNBLOCK_AND_SET")

        prompt_utente = brain.ask_brain.await_args.args[1]
        self.assertIn("device_l9", prompt_utente)
        self.assertIn("ESEGUITO", messaggi[0])
        self.assertEqual(await creato_dopo.get_tool_value(), "ON")


class EndpointDispositiviTest(ConfigurazioneDiProvaMixin, unittest.IsolatedAsyncioTestCase):
    async def test_scrittura_con_valore_non_ammesso_risponde_422(self):
        from app.api.main import ToolWriteRequest, set_tool_endpoint

        self.usa_configurazione()
        tool = IoTDeviceTool("front_door_lock", "LOCKED")
        with patch("app.api.main._shared_tools", {"front_door_lock": tool}):
            with self.assertRaises(HTTPException) as ctx:
                await set_tool_endpoint(ToolWriteRequest(target="front_door_lock", value="ON"))
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("LOCKED", ctx.exception.detail)
            risposta = await set_tool_endpoint(ToolWriteRequest(target="front_door_lock", value="unlocked"))
        self.assertEqual(risposta["new_value"], "UNLOCKED")

    async def test_dispositivo_non_elencato_non_viene_creato_on_demand(self):
        from app.api.main import ToolWriteRequest, get_tool_endpoint, set_tool_endpoint

        self.usa_configurazione()
        with patch("app.api.main._shared_tools", {}) as tools:
            with self.assertRaises(HTTPException) as scrittura:
                await set_tool_endpoint(ToolWriteRequest(target="frigorifero", value="ON"))
            with self.assertRaises(HTTPException) as lettura:
                await get_tool_endpoint("frigorifero")
            self.assertEqual((scrittura.exception.status_code, lettura.exception.status_code), (422, 404))
            self.assertEqual(tools, {})


if __name__ == "__main__":
    unittest.main()
