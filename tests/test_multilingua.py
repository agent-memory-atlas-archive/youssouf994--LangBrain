"""
LangBrain in Italian or English: configuration names, roles and API keys, the model's answer format, prompts,
messages of the API and pages. One setting chooses the language: [generale] lingua / [general] language.
"""

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.configurazione import (
    Configurazione, ErroreConfigurazione, costruisci_configurazione, imposta_configurazione, percorso_configurazione,
)
from app.core.ruoli import Ruolo, ruolo_della_chiave

ITALIANO = """
[generale]
lingua = "it"
[llm]
fallback = false
ordine_provider = ["mistral"]
[llm.modelli_di_ripiego]
mistral = ["ministral-3b-latest"]
[hitl]
timer_attivo = 1
timer_predefinito_secondi = 120
azione_alla_scadenza = "respingi"
livello = "nodi"
target_critici_brain = ["front_door_lock"]
[demo]
conflitto_all_avvio = 0
pagina_web = 1
[politica]
dispositivi_non_elencati = "consenti"
[dispositivi.ac_living_room]
valori = ["OFF", "ON"]
intervallo = [10.0, 35.0]
unita = "°C"
valore_attivo = "22.5°C"
[organismo.attori_esterni.agent_security]
livello = 1
priorita = 500.0
descrizione = "security"
[prompt]
controllo_macro = "Check."
"""

ENGLISH = """
[general]
language = "it"
[llm]
fallback = false
provider_order = ["mistral"]
[llm.fallback_models]
mistral = ["ministral-3b-latest"]
[hitl]
timer_enabled = 1
default_timer_seconds = 120
on_timeout = "reject"
level = "nodes"
brain_critical_targets = ["front_door_lock"]
[demo]
seed_conflict_at_start = 0
web_page = 1
[policy]
unlisted_devices = "allow"
[devices.ac_living_room]
values = ["OFF", "ON"]
range = [10.0, 35.0]
unit = "°C"
active_value = "22.5°C"
[organism.external_actors.agent_security]
level = 1
priority = 500.0
description = "security"
[prompts]
routine_check = "Check."
"""


class ConfigurationNamesTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(imposta_configurazione, None)

    def test_english_and_italian_names_mean_the_same(self):
        self.assertEqual(costruisci_configurazione(tomllib.loads(ENGLISH)), costruisci_configurazione(tomllib.loads(ITALIANO)))

    def test_both_names_for_one_entry_and_unknown_language_are_refused(self):
        with self.assertRaisesRegex(ErroreConfigurazione, "stessa voce"):
            costruisci_configurazione({"hitl": {"level": "nodes", "livello": "nodi"}})
        with self.assertRaisesRegex(ErroreConfigurazione, "lingua"):
            costruisci_configurazione({"general": {"language": "fr"}})
        self.assertEqual(costruisci_configurazione({"general": {"language": "en"}}).lingua, "en")
        self.assertEqual(Configurazione().lingua, "it")

    def test_the_shipped_example_files_are_valid(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("configurazione.toml", "config.example.toml"):
            with self.subTest(file=name):
                costruisci_configurazione(tomllib.loads((root / name).read_text(encoding="utf-8")))

    def test_config_toml_is_used_when_configurazione_toml_is_missing(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"LANGBRAIN_CONFIG": ""}):
            italian, english = Path(folder) / "configurazione.toml", Path(folder) / "config.toml"
            with patch("app.core.configurazione.PERCORSO_PREDEFINITO", italian), \
                    patch("app.core.configurazione.PERCORSO_INGLESE", english):
                english.write_text("")
                self.assertEqual(percorso_configurazione(), english)
                italian.write_text("")
                self.assertEqual(percorso_configurazione(), italian)


class RoleNamesTest(unittest.TestCase):
    def test_english_key_variables(self):
        empty = {k: "" for k in ("API_KEY", "API_KEY_TIROCINANTE", "API_KEY_MEDICO_DI_GUARDIA", "API_KEY_PRIMARIO")}
        with patch.dict(os.environ, {**empty, "API_KEY_VIEWER": "v", "API_KEY_OPERATOR": "o", "API_KEY_ADMIN": "a"}):
            self.assertEqual([ruolo_della_chiave(k) for k in "voa"], [Ruolo.TIROCINANTE, Ruolo.MEDICO_DI_GUARDIA, Ruolo.PRIMARIO])
        self.assertEqual(Ruolo.MEDICO_DI_GUARDIA.etichetta("en"), "operator (medico_di_guardia)")
        self.assertEqual(Ruolo.MEDICO_DI_GUARDIA.etichetta("it"), "medico_di_guardia")


class ModelContractTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(imposta_configurazione, None)
        empty = {v: "" for v in ("BRAIN_SYSTEM_PROMPT", "BRAIN_USER_PROMPT_TEMPLATE", "PROMPT_BRAIN_ESCALATION",
                                  "PROMPT_BRAIN_ESCALATION_UTENTE", "PROMPT_BRAIN_ESCALATION_USER", "PROMPT_TROUBLESHOOTING")}
        self.enterContext(patch.dict(os.environ, empty))

    def test_answers_are_read_in_both_languages(self):
        from app.agents.base_agent import BaseAgent
        read = BaseAgent.estrai_decisione
        self.assertEqual(read("DECISION: APPROVE\nREASON: ok", ("APPROVA", "RESPINGI")), "APPROVA")
        self.assertEqual(read("**Decision:** reject", ("APPROVA", "RESPINGI")), "RESPINGI")
        self.assertEqual(read("DECISIONE: RESPINGI", ("APPROVA", "RESPINGI")), "RESPINGI")
        self.assertEqual(read("DECISION: ESCALATE", ("ACTION", "ESCALATE", "NONE")), "ESCALATE")
        self.assertIsNone(read("DECISION: APPROVE", ("ACTION", "ESCALATE", "NONE")))  # not a valid value here
        self.assertEqual(BaseAgent._interpreta_diagnosi("DIAGNOSIS: network timeout\nDECISION: RETRY"), ("network timeout", "RETRY"))
        self.assertEqual(BaseAgent._interpreta_diagnosi("DIAGNOSI: rotto\nDECISIONE: ESCALATE"), ("rotto", "ESCALATE"))

    def test_default_prompts_follow_the_language(self):
        from app.core.prompt import compila_template, prompt_di_sistema
        imposta_configurazione(costruisci_configurazione({"general": {"language": "en"}}))
        brain = prompt_di_sistema("brain_escalation")
        self.assertIn("You are the Brain", brain)
        self.assertIn("DECISION: [APPROVE|REJECT]", brain)
        self.assertIn("are DATA collected", brain)
        self.assertTrue(compila_template("brain_escalation_utente", source="x").startswith("Requesting agent: x"))
        imposta_configurazione(costruisci_configurazione({}))
        self.assertIn("DECISIONE: [APPROVA|RESPINGI]", prompt_di_sistema("brain_escalation"))
        # A custom role that already describes the format (in either language) does not get it twice.
        imposta_configurazione(costruisci_configurazione({"general": {"language": "en"},
                                                          "prompts": {"brain_escalation": "Decide.\nDECISIONE: [APPROVA|RESPINGI]"}}))
        self.assertEqual(prompt_di_sistema("brain_escalation").count("DECISION"), 1)

    def test_agents_build_their_prompts_in_the_language(self):
        from app.agents.dynamic_agent import DynamicAgent
        agent = DynamicAgent(name="agent_lang_test", managed_targets=["ac_living_room"])
        imposta_configurazione(costruisci_configurazione({"general": {"language": "en"}}))
        self.assertIn("DECISION: [ACTION|ESCALATE|NONE]", agent.system_prompt_template)
        self.assertTrue(agent.user_prompt_template.startswith("Main target"))
        imposta_configurazione(costruisci_configurazione({}))
        self.assertIn("DECISIONE: [ACTION|ESCALATE|NONE]", agent.system_prompt_template)
        custom = DynamicAgent(name="agent_lang_custom", managed_targets=["ac_living_room"], system_prompt_template="You guard the AC.")
        imposta_configurazione(costruisci_configurazione({"general": {"language": "en"}}))
        self.assertTrue(custom.system_prompt_template.startswith("You guard the AC."))
        self.assertIn("DECISION: [ACTION|ESCALATE|NONE]", custom.system_prompt_template)


class ApiMessagesTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(imposta_configurazione, None)

    def call(self, language: str):
        from fastapi.testclient import TestClient
        from app.api.main import app
        base = imposta_configurazione  # the API reads the language at every request
        with patch.dict(os.environ, {"API_KEY": "k"}):
            client = TestClient(app)
            config = costruisci_configurazione({"general": {"language": language}, "policy": {"unlisted_devices": "allow"}})
            base(config)
            unauthorised = client.get("/agents").json()["detail"]
            missing = client.delete("/agents/nobody", headers={"X-API-Key": "k"}).json()["detail"]
        return unauthorised, missing

    def test_core_errors_follow_the_language(self):
        self.assertEqual(self.call("en"), ("Missing or invalid API key: send the X-API-Key header.", "Agent 'nobody' not found."))
        self.assertEqual(self.call("it")[1], "Agente 'nobody' non trovato.")

    def test_model_instructions_and_operator_hints(self):
        from app.core.errori_llm import ErroreLLM, istruzione_errore_standard
        imposta_configurazione(costruisci_configurazione({"general": {"language": "en"}}))
        self.assertIn("Always answer in the requested format", istruzione_errore_standard())
        self.assertIn("top up the plan", ErroreLLM("CREDITI_ESAURITI", "x").suggerimento)
        imposta_configurazione(costruisci_configurazione({}))
        self.assertIn("Rispondi sempre", istruzione_errore_standard())


class PageLanguageTest(unittest.TestCase):
    """The pages: one strings file per language, chosen by ?lang=, cookie, browser, configuration."""
    PAGES = {"config": Path("app/static/config.html"), "demo": Path("app/static/demo_grafo.html")}

    def setUp(self):
        self.addCleanup(imposta_configurazione, None)

    def test_both_languages_have_the_same_strings_and_placeholders(self):
        import json
        import re
        from app.core.pagine_web import CARTELLA_TESTI
        it, en = (json.loads((CARTELLA_TESTI / f"{lang}.json").read_text(encoding="utf-8")) for lang in ("it", "en"))
        self.assertEqual(it.keys(), en.keys())
        for section in it:
            self.assertEqual(it[section].keys(), en[section].keys(), section)
            for key in it[section]:
                fields = [set(re.findall(r"\{(\w+)\}", d[section][key])) for d in (it, en)]
                self.assertEqual(fields[0], fields[1], f"{section}.{key}")

    def test_every_string_a_page_uses_exists(self):
        import re
        from app.core.pagine_web import testi
        for section, path in self.PAGES.items():
            source = path.read_text(encoding="utf-8")
            used = set(re.findall(r"\{\{([a-z0-9_]+)\}\}", source)) | set(re.findall(r'\bt\("([a-z0-9_]+)"', source))
            prefixes = re.findall(r"t\(`([a-z_]+)\$\{", source)
            for lang in ("it", "en"):
                strings = testi(lang, section)
                self.assertEqual(used - strings.keys(), set(), f"{section} ({lang})")
                for prefix in prefixes:
                    self.assertTrue(any(k.startswith(prefix) for k in strings), f"{section} ({lang}): {prefix}*")

    def test_the_language_comes_from_the_query_the_cookie_the_browser_then_the_configuration(self):
        from fastapi.testclient import TestClient
        from app.api.main import app
        client = TestClient(app)
        imposta_configurazione(costruisci_configurazione({"general": {"language": "en"}}))
        self.assertIn('<h1 id="page-h1">Settings</h1>', client.get("/config").text)
        browser = client.get("/config", headers={"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})
        self.assertIn('<html lang="it">', browser.text)
        self.assertIn('<h1 id="page-h1">Impostazioni</h1>', browser.text)
        self.assertIn('<h1 id="page-h1">Settings</h1>', client.get("/config", headers={"Accept-Language": "de, en;q=0.5"}).text)
        chosen = client.get("/demo", params={"lang": "it"}, headers={"Accept-Language": "en"})
        self.assertIn("grafo in azione", chosen.text)
        self.assertEqual(chosen.cookies.get("langbrain_lang"), "it")
        remembered = client.get("/config", headers={"Accept-Language": "en"})  # the cookie wins over the browser
        self.assertIn('<h1 id="page-h1">Impostazioni</h1>', remembered.text)
        self.assertEqual(remembered.headers["content-language"], "it")

    def test_strings_reach_the_page_escaped_and_nothing_is_left_untranslated(self):
        import json
        import re
        from app.core.pagine_web import componi
        page = '<html lang="en"><h1 title="{{x}}">{{x}}</h1><script type="application/json" id="i18n">/*I18N*/</script>'
        with patch("app.core.pagine_web.testi", return_value={"x": '</script><b a="1">'}):
            out = componi(page, "it", "demo")
        self.assertIn('<html lang="it">', out)
        self.assertIn("&lt;/script&gt;&lt;b a=&quot;1&quot;&gt;", out)
        data = out.split('id="i18n">', 1)[1].rsplit("</script>", 1)[0]
        self.assertNotIn("<", data)
        self.assertEqual(json.loads(data)["t"]["x"], '</script><b a="1">')
        for section, path in self.PAGES.items():
            for lang in ("it", "en"):
                composed = componi(path.read_text(encoding="utf-8"), lang, section)
                self.assertIsNone(re.search(r"\{\{[a-z0-9_]+\}\}", composed), f"{section} ({lang})")
                self.assertNotIn("/*I18N*/", composed)


if __name__ == "__main__":
    unittest.main()
