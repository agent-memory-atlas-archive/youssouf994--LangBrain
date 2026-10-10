"""Pagina /config del core: editor di .env e configurazione.toml (più i file aggiunti da un'estensione), riavvio, rotte e testi."""
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.core import settings_editor as se
from app.core.configurazione import costruisci_configurazione
from app.core.estensioni import Estensione, SorgenteImpostazioni

CONFIG_IT = """# commento iniziale
[generale]
lingua = "it"

[llm]
fallback = false   # nota sul ripiego
ordine_provider = ["google_studio", "mistral"]

[hitl]
timer_attivo = 0
livello = "entrambi"

[dispositivi.lampada]
valori = ["ON", "OFF"]
"""
NODE = """[mynode]
limit = 50.0
names = []   # esempio
mode = "off"

[[mynode.sources]]
name = "primary"
"""
ENV = "# prova\nDEFAULT_PROVIDER=mistral\nMISTRAL_API_KEY=abc\n"


class TomlEditorTest(unittest.TestCase):
    def test_sostituisce_il_valore_e_tiene_il_commento_in_linea(self):
        out = se.set_toml_value(CONFIG_IT, ("llm",), ("fallback",), True, new_section="llm", new_key="fallback")
        self.assertIn("fallback = true  # nota sul ripiego", out)
        self.assertIn("# commento iniziale", out)
        self.assertEqual(tomllib.loads(out)["llm"]["fallback"], True)

    def test_aggiunge_la_chiave_in_fondo_alla_sezione_e_non_in_un_array_di_tabelle(self):
        out = se.set_toml_value(NODE, ("mynode",), ("provider",), "mistral", new_section="mynode", new_key="provider")
        data = tomllib.loads(out)
        self.assertEqual(data["mynode"]["provider"], "mistral")
        self.assertEqual(data["mynode"]["sources"][0]["name"], "primary")

    def test_crea_la_sezione_mancante(self):
        out = se.set_toml_value("[a]\nx = 1\n", ("b",), ("y",), 2, new_section="b", new_key="y")
        self.assertEqual(tomllib.loads(out), {"a": {"x": 1}, "b": {"y": 2}})

    def test_valore_none_rimuove_la_riga(self):
        out = se.set_toml_value(NODE, ("mynode",), ("mode",), None, new_section="mynode", new_key="mode")
        self.assertNotIn("mode", tomllib.loads(out)["mynode"])

    def test_testo_su_piu_righe_con_virgolette_e_backslash_sopravvive(self):
        text = 'Ruolo "A" con \\ e\nuna seconda riga\n'
        out = se.set_toml_value("[prompt]\n", ("prompt",), ("override",), text, new_section="prompt", new_key="override")
        self.assertEqual(tomllib.loads(out)["prompt"]["override"], text)

    def test_sostituisce_anche_un_valore_su_piu_righe(self):
        base = '[prompt]\noverride = """\nvecchio\ntesto\n"""\naltro = 1\n'
        out = se.set_toml_value(base, ("prompt",), ("override",), "nuovo", new_section="prompt", new_key="override")
        self.assertEqual(tomllib.loads(out)["prompt"], {"override": "nuovo", "altro": 1})

    def test_un_file_piatto_resta_piatto(self):
        out = se.set_toml_value("limit = 0.02\n", ("",), ("limit",), 0.5, new_section="mynode", new_key="limit")
        self.assertEqual(tomllib.loads(out), {"limit": 0.5})


class EnvEditorTest(unittest.TestCase):
    def test_sostituisce_aggiunge_e_cita_i_valori_speciali(self):
        out = se.set_env_value(ENV, "DEFAULT_PROVIDER", "local")
        out = se.set_env_value(out, "NUOVA", "a b #c")
        self.assertIn("DEFAULT_PROVIDER=local\n", out)
        self.assertIn('NUOVA="a b #c"\n', out)
        self.assertIn("# prova", out)

    def test_aggiunge_a_capo_se_manca(self):
        self.assertEqual(se.set_env_value("A=1", "B", "2"), "A=1\nB=2\n")


def _demo_node_extension(directory: Path) -> Estensione:
    """Un nodo finto con un suo file TOML, per provare le sorgenti aggiunte da un'estensione."""
    def validate(path: Path) -> None:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("mynode", {})
        if data.get("limit", 0) > 1000:
            raise ValueError("limit troppo alto")

    source = SorgenteImpostazioni("mynode", lambda: directory / "mynode.toml", "mynode", validate, "config")
    fields = (se.Field("mynode:limit", "mynode", "limits", "float", "limit", 1.0, minimum=0, restart=True),
              se.Field("mynode:mode", "mynode", "limits", "choice", "mode", "off", choices=("off", "on")))
    return Estensione(nome="mynode", sorgenti=(source,), campi=fields)


class FilesTest(unittest.TestCase):
    """Salvataggio, validazione e ripristino su copie dei file: mai sui file veri."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="settings_test_"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "configurazione.toml").write_text(CONFIG_IT, encoding="utf-8")
        (self.dir / "mynode.toml").write_text(NODE, encoding="utf-8")
        (self.dir / ".env").write_text(ENV, encoding="utf-8")
        for patcher in (mock.patch.dict(os.environ, {"LANGBRAIN_CONFIG": str(self.dir / "configurazione.toml"),
                                                     "LANGBRAIN_ENV_FILE": str(self.dir / ".env")}),
                        mock.patch.object(se, "estensioni", lambda: [_demo_node_extension(self.dir)])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_describe_non_restituisce_mai_i_segreti(self):
        fields = {f["id"]: f for f in se.describe()["fields"]}
        key = fields["env:MISTRAL_API_KEY"]
        self.assertTrue(key["is_set"])
        self.assertIsNone(key["value"])
        self.assertNotIn("abc", json.dumps(se.describe()))
        self.assertEqual(fields["config:hitl.level"]["value"], "both")  # il file dice "entrambi"
        self.assertEqual(fields["mynode:limit"]["value"], 50.0)

    def test_i_file_dei_nodi_diventano_schede_della_vista_indicata(self):
        files = se.describe()["files"]
        self.assertEqual({k: v["view"] for k, v in files.items()}, {"env": "config", "config": "config", "mynode": "config"})
        self.assertEqual(files["mynode"]["name"], "mynode.toml")

    def test_salva_scrive_nei_file_con_i_nomi_del_file_e_fa_la_copia(self):
        result = se.save({"config:hitl.level": "nodes", "config:llm.fallback": True, "config:hitl.timer_enabled": True,
                          "mynode:limit": 75, "env:DEFAULT_PROVIDER": "local"})
        config = (self.dir / "configurazione.toml").read_text(encoding="utf-8")
        self.assertIn('livello = "nodi"', config)  # file italiano: valore italiano
        self.assertIn("timer_attivo = 1", config)
        self.assertTrue((self.dir / "configurazione.toml.bak").is_file())
        self.assertEqual(tomllib.loads((self.dir / "mynode.toml").read_text(encoding="utf-8"))["mynode"]["limit"], 75.0)
        self.assertIn("mynode:limit", result["restart"])
        self.assertEqual(os.environ["DEFAULT_PROVIDER"], "local")

    def test_tutto_o_niente_se_un_valore_non_e_valido(self):
        before = (self.dir / "mynode.toml").read_text(encoding="utf-8")
        with self.assertRaises(se.SettingsError) as raised:
            se.save({"mynode:limit": 99, "mynode:mode": "boh"})
        self.assertIn("mynode:mode", raised.exception.errors)
        self.assertEqual((self.dir / "mynode.toml").read_text(encoding="utf-8"), before)

    def test_un_file_che_il_caricatore_del_nodo_rifiuta_non_viene_scritto(self):
        before = (self.dir / "mynode.toml").read_text(encoding="utf-8")
        with self.assertRaises(se.SettingsError) as raised:
            se.save({"mynode:limit": 5000})
        self.assertIn("limit troppo alto", raised.exception.errors["mynode:limit"])
        self.assertEqual((self.dir / "mynode.toml").read_text(encoding="utf-8"), before)

    def test_un_file_che_il_caricatore_rifiuta_non_viene_scritto(self):
        before = (self.dir / "configurazione.toml").read_text(encoding="utf-8")
        with mock.patch.object(se, "costruisci_configurazione", side_effect=se.ErroreConfigurazione("no")):
            with self.assertRaises(se.SettingsError):
                se.save({"config:hitl.level": "brain"})
        self.assertEqual((self.dir / "configurazione.toml").read_text(encoding="utf-8"), before)

    def test_impostazione_sconosciuta_rifiutata(self):
        with self.assertRaises(se.SettingsError):
            se.save({"env:NON_ESISTE": "x"})

    def test_ripristino_riporta_la_versione_precedente_e_si_puo_rifare(self):
        se.save({"mynode:limit": 0.5})
        self.assertEqual(se.restore_backups(), ["mynode.toml"])
        self.assertEqual(tomllib.loads((self.dir / "mynode.toml").read_text(encoding="utf-8"))["mynode"]["limit"], 50.0)
        se.restore_backups()
        self.assertEqual(tomllib.loads((self.dir / "mynode.toml").read_text(encoding="utf-8"))["mynode"]["limit"], 0.5)

    def test_il_file_di_configurazione_inglese_resta_inglese(self):
        (self.dir / "configurazione.toml").write_text('[hitl]\nlevel = "both"\non_timeout = "human"\n', encoding="utf-8")
        se.save({"config:hitl.level": "brain", "config:hitl.on_timeout": "reject"})
        text = (self.dir / "configurazione.toml").read_text(encoding="utf-8")
        self.assertIn('level = "brain"', text)
        self.assertIn('on_timeout = "reject"', text)
        costruisci_configurazione(tomllib.loads(text))

    def test_prompt_vuoto_rimuove_la_voce(self):
        se.save({"config:prompts.override": "Ruolo di prova"})
        self.assertIn("override", tomllib.loads((self.dir / "configurazione.toml").read_text(encoding="utf-8"))["prompt"])
        se.save({"config:prompts.override": ""})
        self.assertNotIn("override", tomllib.loads((self.dir / "configurazione.toml").read_text(encoding="utf-8")).get("prompt", {}))

    def test_la_finestra_dei_segnali_si_scrive_nel_file_con_i_nomi_del_file(self):
        se.save({"config:signals.window_hours": 6})
        text = (self.dir / "configurazione.toml").read_text(encoding="utf-8")
        self.assertEqual(tomllib.loads(text)["segnali"]["finestra_ore"], 6.0)  # file italiano: sezione italiana
        self.assertEqual(costruisci_configurazione(tomllib.loads(text)).finestra_segnali_ore, 6.0)
        with self.assertRaises(se.SettingsError):
            se.save({"config:signals.window_hours": 0})


class RoutesAndPageTest(unittest.TestCase):
    def setUp(self):
        from app.api.main import app
        self.client = TestClient(app)
        self.dir = Path(tempfile.mkdtemp(prefix="settings_routes_"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "configurazione.toml").write_text(CONFIG_IT, encoding="utf-8")
        (self.dir / ".env").write_text(ENV, encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"LANGBRAIN_CONFIG": str(self.dir / "configurazione.toml"),
                                               "LANGBRAIN_ENV_FILE": str(self.dir / ".env"),
                                               "API_KEY_PRIMARIO": "", "API_KEY_TIROCINANTE": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_la_pagina_si_apre_senza_chiave_e_nelle_due_lingue(self):
        for lang, word in (("en", "Settings"), ("it", "Impostazioni")):
            page = self.client.get("/config", params={"lang": lang})
            self.assertEqual(page.status_code, 200)
            self.assertIn(word, page.text)
            self.assertNotIn("{{", page.text.split('id="i18n"')[0])
            self.assertNotIn("/*SETTINGS_FORM_JS*/", page.text)  # il modulo del modulo è stato inserito
            self.assertIn("async function restart()", page.text)

    def test_leggere_e_salvare_dalle_rotte(self):
        self.assertEqual(self.client.get("/config/api/fields").status_code, 200)
        ok = self.client.post("/config/api/save", json={"changes": {"config:hitl.level": "brain"}})
        self.assertEqual(ok.status_code, 200)
        bad = self.client.post("/config/api/save", json={"changes": {"config:hitl.level": "x"}})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("config:hitl.level", bad.json()["errors"])
        self.assertEqual(self.client.post("/config/api/restore").status_code, 200)

    def test_salvare_richiede_il_primario_leggere_il_tirocinante(self):
        with mock.patch.dict(os.environ, {"API_KEY_PRIMARIO": "p", "API_KEY_TIROCINANTE": "t"}):
            self.assertEqual(self.client.get("/config/api/fields", headers={"X-API-Key": "t"}).status_code, 200)
            self.assertEqual(self.client.post("/config/api/save", json={"changes": {}}, headers={"X-API-Key": "t"}).status_code, 403)
            self.assertEqual(self.client.post("/config/api/save", json={"changes": {}}, headers={"X-API-Key": "p"}).status_code, 200)
            self.assertEqual(self.client.get("/config/api/fields").status_code, 401)


class StringsTest(unittest.TestCase):
    """Ogni campo del core ha il suo aiuto, ogni testo usato dalla pagina esiste nelle due lingue."""

    def test_ogni_campo_ha_aiuto_etichetta_e_gruppo_in_entrambe_le_lingue(self):
        from app.core.pagine_web import testi
        for lang in ("it", "en"):
            strings = testi(lang, "config")
            for f in se.CORE_FIELDS:
                self.assertIn(f"hlp_{f.slug}", strings, f"{lang}: {f.id}")
                if f.source != "env":
                    self.assertIn(f"fld_{f.slug}", strings, f"{lang}: {f.id}")
                self.assertIn(f"grp_{f.source}_{f.group}", strings, f"{lang}: {f.id}")

    def test_i_testi_usati_dalla_pagina_esistono(self):
        from app.core.pagine_web import testi
        root = Path(__file__).resolve().parent.parent / "app" / "static"
        source = (root / "config.html").read_text(encoding="utf-8") + (root / "settings_form.js").read_text(encoding="utf-8")
        used = set(re.findall(r"\{\{([a-z0-9_]+)\}\}", source)) | set(re.findall(r'\bt\("([a-z0-9_]+)"', source))
        for lang in ("it", "en"):
            self.assertEqual(used - testi(lang, "config").keys(), set(), lang)


class RestartTest(unittest.TestCase):
    """Pulsante «Riavvia app»: solo il primario; qui il processo non viene davvero fermato."""

    def setUp(self):
        from app.api.main import app
        self.client = TestClient(app)
        patcher = mock.patch.dict(os.environ, {"API_KEY_PRIMARIO": "adm", "API_KEY_MEDICO_DI_GUARDIA": "", "API_KEY_TIROCINANTE": "viewer", "API_KEY": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_il_riavvio_richiede_il_primario(self):
        with mock.patch("app.core.riavvio.programma_riavvio", return_value="process") as plan:
            self.assertEqual(self.client.post("/config/api/restart", headers={"X-API-Key": "viewer"}).status_code, 403)
            self.assertEqual(self.client.post("/config/api/restart").status_code, 401)
            plan.assert_not_called()
            ok = self.client.post("/config/api/restart", headers={"X-API-Key": "adm"})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json()["mode"], "process")
            plan.assert_called_once()

    def test_i_campi_dicono_chi_puo_riavviare(self):
        self.assertTrue(self.client.get("/config/api/fields", headers={"X-API-Key": "adm"}).json()["can_restart"])
        self.assertFalse(self.client.get("/config/api/fields", headers={"X-API-Key": "viewer"}).json()["can_restart"])

    def test_il_riavvio_ferma_il_processo_e_ne_lancia_uno_uguale(self):
        from app.core import riavvio

        async def run(docker):
            with mock.patch.object(riavvio, "RITARDO_SECONDI", 0), mock.patch.object(riavvio, "in_docker", return_value=docker), \
                    mock.patch.object(riavvio.subprocess, "Popen") as popen, mock.patch.object(riavvio.signal, "raise_signal") as stop:
                riavvio.programma_riavvio()
                await riavvio._compito
                return popen, stop

        popen, stop = asyncio.run(run(False))
        stop.assert_called_once()
        args = popen.call_args.args[0]
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[4:], riavvio.comando_di_avvio())
        popen, stop = asyncio.run(run(True))  # Docker: basta uscire, ci pensa la policy di riavvio
        popen.assert_not_called()
        stop.assert_called_once()


class ExtensionsTest(unittest.TestCase):
    """Il core carica le estensioni da app/nodes e le applica a ruoli, navigazione, testi e prompt."""

    def test_senza_estensioni_il_core_funziona_da_solo(self):
        from app.core import estensioni as modulo
        with mock.patch.object(modulo, "_moduli_estensione", return_value=[]), mock.patch.object(modulo, "_caricate", None):
            self.assertEqual(modulo.carica_estensioni(), [])
            self.assertEqual(modulo.voci_nav(), [])

    def test_un_modulo_che_non_si_carica_non_ferma_il_resto(self):
        from app.core import estensioni as modulo
        with mock.patch.object(modulo, "_moduli_estensione", return_value=["app.nodes.non_esiste"]), mock.patch.object(modulo, "_caricate", None):
            self.assertEqual(modulo.carica_estensioni(), [])

    def test_i_permessi_e_le_pagine_pubbliche_di_un_nodo_entrano_nella_matrice(self):
        from app.core import estensioni as modulo, ruoli
        ext = Estensione(nome="x", permessi={("GET", "/x/api/y"): ruoli.Ruolo.TIROCINANTE}, rotte_pubbliche=frozenset({("GET", "/x")}))
        fake = mock.Mock(ESTENSIONE=ext)
        with mock.patch.object(modulo, "_moduli_estensione", return_value=["app.nodes.x"]), mock.patch.object(modulo, "_caricate", None), \
                mock.patch.object(modulo.importlib, "import_module", return_value=fake), \
                mock.patch.dict(ruoli.PERMESSI), mock.patch.object(ruoli, "ROTTE_PUBBLICHE", set(ruoli.ROTTE_PUBBLICHE)):
            self.assertEqual(modulo.carica_estensioni(), [ext])
            self.assertEqual(ruoli.ruolo_minimo("GET", "/x/api/y"), ruoli.Ruolo.TIROCINANTE)
            self.assertIn(("GET", "/x"), ruoli.ROTTE_PUBBLICHE)

    def test_la_navigazione_e_i_testi_di_un_nodo_si_uniscono_a_quelli_del_core(self):
        from app.core import estensioni as modulo, pagine_web
        folder = Path(tempfile.mkdtemp(prefix="ext_texts_"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "en.json").write_text(json.dumps({"common": {"my_page": "My page"}, "config": {"extra": "Extra"}}), encoding="utf-8")
        ext = Estensione(nome="x", nav=(modulo.VoceNav("mine", "/mine", "my_page", 10),), testi=folder)
        with mock.patch.object(modulo, "_caricate", [ext]):
            strings = pagine_web.testi("en", "config")
            self.assertEqual((strings["extra"], strings["my_page"], strings["title"]), ("Extra", "My page", "Settings"))
            nav = pagine_web.navigazione("settings", strings)
            self.assertLess(nav.index('href="/mine"'), nav.index('href="/config"'))
            self.assertIn('aria-current="page"', nav.split('href="/config"')[1][:40])


if __name__ == "__main__":
    unittest.main()
