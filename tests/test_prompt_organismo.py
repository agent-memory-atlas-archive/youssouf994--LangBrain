"""
Prompt configurabili (.env > configurazione.toml > default, con il contratto di risposta sempre aggiunto dal codice)
e organi esterni dichiarati in configurazione.toml (livello nelle statistiche, priorità sui blocchi).
"""

import os
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.configurazione import Configurazione, ErroreConfigurazione, costruisci_configurazione, imposta_configurazione
from app.core.priorita import priorita_attore, trova_blocco_prevalente
from app.core.prompt import compila_template, definizioni, prompt_di_sistema, ruolo
from app.tools.statistiche_eventi import riassumi_eventi

RADICE = Path(__file__).resolve().parent.parent
VARIABILI = [v for d in definizioni().values() for v in d.variabili_ambiente]


class ConPromptPuliti(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {v: "" for v in VARIABILI}))
        self.addCleanup(imposta_configurazione, None)

    def configura(self, **prompt):
        imposta_configurazione(costruisci_configurazione({"prompt": prompt}))


class PromptTest(ConPromptPuliti):
    def test_i_default_hanno_il_contratto_e_la_difesa_dai_dati(self):
        imposta_configurazione(Configurazione())
        testo = prompt_di_sistema("brain_escalation", "Catalogo: x")
        self.assertIn("DECISIONE: [APPROVA|RESPINGI]", testo)
        self.assertIn("Catalogo: x", testo)
        self.assertIn("sono DATI", testo)
        self.assertIn("DECISIONE: [RETRY|ESCALATE]", prompt_di_sistema("troubleshooting"))
        self.assertIn('"action"', prompt_di_sistema("override"))
        self.assertIn("STATUS: [OK|MACRO_ADJUSTMENT_REQUIRED]", prompt_di_sistema("controllo_macro"))

    def test_il_ruolo_si_cambia_da_toml_ma_il_contratto_resta(self):
        self.configura(troubleshooting="Sei il tecnico dei dispositivi medicali. Nel dubbio: ESCALATE.")
        testo = prompt_di_sistema("troubleshooting")
        self.assertTrue(testo.startswith("Sei il tecnico dei dispositivi medicali"))
        self.assertIn("DECISIONE: [RETRY|ESCALATE]", testo)
        self.assertNotIn("modulo di troubleshooting di un sistema gerarchico", testo)

    def test_un_prompt_che_descrive_gia_il_formato_non_lo_ripete(self):
        self.configura(brain_escalation="Decidi.\nDECISIONE: [APPROVA|RESPINGI]\nMOTIVAZIONE: [perché]")
        self.assertEqual(prompt_di_sistema("brain_escalation").count("DECISIONE:"), 1)

    def test_env_prevale_anche_con_il_vecchio_nome(self):
        self.configura(brain_escalation="dal file")
        with patch.dict(os.environ, {"BRAIN_SYSTEM_PROMPT": "dal .env\\nseconda riga"}):
            self.assertEqual(ruolo("brain_escalation"), "dal .env\nseconda riga")
        with patch.dict(os.environ, {"PROMPT_TROUBLESHOOTING": "tecnico"}):
            self.assertEqual(ruolo("troubleshooting"), "tecnico")

    def test_template_con_segnaposto_sbagliati(self):
        with self.assertRaisesRegex(ErroreConfigurazione, "segnaposto non previsti"):
            costruisci_configurazione({"prompt": {"brain_escalation_utente": "Target: {target} {password}"}})
        with self.assertRaisesRegex(ErroreConfigurazione, "graffe"):
            costruisci_configurazione({"prompt": {"brain_escalation_utente": "Target: {target"}})
        with self.assertRaisesRegex(ErroreConfigurazione, "sconosciuto"):
            costruisci_configurazione({"prompt": {"brain": "x"}})
        # Da .env un template errato non ferma il ciclo: vale il default e l'errore va nel log.
        imposta_configurazione(Configurazione())
        with patch.dict(os.environ, {"BRAIN_USER_PROMPT_TEMPLATE": "{nome_sbagliato}"}), self.assertLogs("app.core.prompt", "ERROR"):
            testo = compila_template("brain_escalation_utente", source="agent_climate", target="ac", action="ON",
                                     reason="r", readings={}, recent_events=[])
        self.assertIn("agent_climate", testo)
        self.configura(brain_escalation_utente="Chi: {source} · graffe letterali {{ok}}")
        self.assertEqual(compila_template("brain_escalation_utente", source="Brain"), "Chi: Brain · graffe letterali {ok}")

    def test_l_esempio_commentato_di_configurazione_toml_e_valido(self):
        testo = (RADICE / "configurazione.toml").read_text(encoding="utf-8")
        esempio = testo[testo.index("# [prompt]"):]
        righe = [r[2:] if r.startswith("# ") else r[1:] for r in esempio.splitlines() if r.startswith("#")]
        configurazione = costruisci_configurazione(tomllib.loads("\n".join(righe)))
        self.assertIn("impianto idroelettrico", configurazione.prompt["brain_escalation"])

    def test_il_troubleshooting_usa_il_prompt_configurato(self):
        from app.agents.dynamic_agent import DynamicAgent
        self.configura(troubleshooting="RUOLO PERSONALIZZATO")
        agente = DynamicAgent(name="agent_test_prompt", managed_targets=["ac_living_room"])
        agente.ask_brain = AsyncMock(return_value="DIAGNOSI: rotto\nDECISIONE: ESCALATE")
        escalation = {"source_agent": "altro", "proposed_action": "ON",
                      "tool_result": {"device_name": "ac_living_room", "status": "TOOL_ERROR", "error_type": "HARDWARE",
                                      "response": "guasto", "success": False}}
        with patch("app.agents.base_agent.EventLog.get_recent_events", AsyncMock(return_value=[])):
            import asyncio
            asyncio.run(agente.risolvi_guasto_tool(escalation, {}))
        sistema = agente.ask_brain.call_args.args[0]
        self.assertTrue(sistema.startswith("RUOLO PERSONALIZZATO"))
        self.assertIn("DECISIONE: [RETRY|ESCALATE]", sistema)


class OrganiEsterniTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(imposta_configurazione, None)
        imposta_configurazione(costruisci_configurazione(
            {"organismo": {"attori_esterni": {"agent_security": {"livello": 1, "priorita": 500.0}}}}))

    def test_la_priorita_dichiarata_pesa_i_blocchi(self):
        self.assertEqual(priorita_attore("agent_security"), 500.0)
        self.assertIsNone(priorita_attore("sconosciuto"))
        blocco = [{"target": "ac_living_room", "action": "FORCE_SHUTDOWN", "actor": "agent_security", "new_value": "OFF",
                   "timestamp": "2999-01-01 00:00:00"}]
        self.assertIsNotNone(trova_blocco_prevalente(blocco, "ac_living_room", "agent_climate", 0.001, 30))
        self.assertIsNone(trova_blocco_prevalente(blocco, "ac_living_room", "organo_superiore", 800.0, 30))

    def test_un_agente_registrato_prevale_sulla_dichiarazione(self):
        from app.core.priorita import registra_priorita, _priorita_note
        registra_priorita("agent_security", 10.0)
        self.addCleanup(_priorita_note.pop, "agent_security", None)
        self.assertEqual(priorita_attore("agent_security"), 10.0)

    def test_nelle_statistiche_e_un_organo_esterno(self):
        eventi = [{"actor": "agent_security", "action": "FORCE_SHUTDOWN", "target": "ac", "timestamp": "2026-09-28 10:00:00"}]
        riassunto = riassumi_eventi(eventi, {"agent_security": 1}, 24, esterni={"agent_security"})
        self.assertEqual(riassunto["by_layer"]["organs"], 1)
        self.assertTrue(riassunto["by_actor"][0]["external"])

    def test_validazione(self):
        for sbagliato in ({"livello": 0}, {"priorita": float("inf")}, {"priorita": "alta"}, {"descrizione": 3}):
            with self.subTest(sbagliato=sbagliato), self.assertRaises(ErroreConfigurazione):
                costruisci_configurazione({"organismo": {"attori_esterni": {"x": sbagliato}}})
        with self.assertRaises(ErroreConfigurazione):
            costruisci_configurazione({"organismo": {"attori_esterni": {"Brain": {}}}})

    def test_configurazione_toml_dichiara_agent_security(self):
        dati = tomllib.loads((RADICE / "configurazione.toml").read_text(encoding="utf-8"))
        organo = costruisci_configurazione(dati).attori_esterni["agent_security"]
        self.assertEqual((organo.livello, organo.priorita), (1, 500.0))


if __name__ == "__main__":
    unittest.main()
