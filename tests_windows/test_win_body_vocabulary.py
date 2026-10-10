"""Vocabolario del corpo umano (fase 1): nuovi nomi, vecchi nomi come alias, config e rotte."""

import unittest
import warnings

from app.core.configurazione import normalizza_nomi
from app.core.constants import nome_livello
from app.tools import effectors, motor_control, nervous_system, reflex


class AliasTest(unittest.TestCase):
    def test_i_vecchi_nomi_sono_gli_stessi_oggetti(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from app.tools import baseTool, event_log, sensor_tools, tool_wrapper
        self.assertIs(baseTool.BaseTool, reflex.Reflex)
        self.assertIs(sensor_tools.IoTDeviceTool, effectors.SimulatedEffector)
        self.assertIs(sensor_tools.get_default_iot_tools, effectors.default_effectors)
        self.assertIs(sensor_tools._TOOL_REGISTRY, effectors._TOOL_REGISTRY)
        self.assertIs(event_log.EventLog, nervous_system.NervousSystem)
        self.assertIs(tool_wrapper.execute_tool_safely, motor_control.execute_tool_safely)

    def test_il_recettore_e_un_riflesso(self):
        self.assertTrue(issubclass(reflex.Receptor, reflex.Reflex))

        class Eye(reflex.Receptor):
            pass

        self.assertTrue(issubclass(Eye, reflex.Reflex))
        self.assertFalse(issubclass(reflex.Reflex, reflex.Receptor))

    def test_nomi_dei_livelli(self):
        self.assertEqual([nome_livello(n) for n in range(5)], ["Brain", "Organ", "Tissue", "Cell", "Cell"])


class ConfigTest(unittest.TestCase):
    def test_effectors_e_devices_sono_la_stessa_sezione(self):
        dati = normalizza_nomi({"effectors": {"lamp": {"values": ["ON", "OFF"]}}, "policy": {"unlisted_effectors": "reject"}})
        self.assertEqual(dati["dispositivi"]["lamp"]["valori"], ["ON", "OFF"])
        self.assertEqual(dati["politica"]["dispositivi_non_elencati"], "rifiuta")

    def test_usare_entrambi_i_nomi_e_rifiutato(self):
        from app.core.configurazione import ErroreConfigurazione
        with self.assertRaises(ErroreConfigurazione):
            normalizza_nomi({"effectors": {}, "devices": {}})


class RotteTest(unittest.TestCase):
    def test_effectors_risponde_come_tools(self):
        from fastapi.testclient import TestClient
        from app.api.main import app
        with TestClient(app) as client:
            self.assertEqual(client.get("/effectors").json().keys(), client.get("/tools").json().keys())
            self.assertEqual(client.get("/effectors/lampada_vocab").status_code, 200)

    def test_i_segnali_sostituiscono_sensor_readings(self):
        from app.api.main import RunCycleRequest
        self.assertIsNone(RunCycleRequest().signals)
        self.assertEqual(RunCycleRequest(signals=[{"a": 1}]).signals, [{"a": 1}])


if __name__ == "__main__":
    unittest.main()
