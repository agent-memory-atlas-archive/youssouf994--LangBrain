import functools
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi import HTTPException

from app.agents.agent_registry import AgentRegistry
from app.agents.base_agent import BaseAgent
from app.core import modelli_agenti as modulo
from app.core.modelli_agenti import (
    ModelloRisolto, imposta_modello, leggi_modello, risolvi_modello, rimuovi_modello,
)
from app.MAO.model_access_object import Mao, normalizza_provider


class DatabaseTemporaneoMixin:
    async def preparazione_db(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.db = os.path.join(self._cartella.name, "modelli.db")
        self.registro = AgentRegistry(db_path=self.db)
        await self.registro.init_registry_db()
        await modulo.assicura_tabella(self.db)

    async def agente(self, nome, padre="Brain", target=None):
        await self.registro.register_agent_config({"name": nome, "parent_agent_name": padre, "managed_targets": target or [f"dev_{nome}"]})


class RisoluzioneModelloTest(DatabaseTemporaneoMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.preparazione_db()
        await self.agente("organo")
        await self.agente("componente", padre="organo")
        await self.agente("sottocomponente", padre="componente")

    async def risolvi(self, nome):
        return await risolvi_modello(nome, self.db)

    async def test_senza_impostazioni_vale_il_predefinito_globale(self):
        risolto = await self.risolvi("sottocomponente")
        self.assertEqual((risolto.provider, risolto.model, risolto.origine), (None, None, "predefinito"))
        self.assertFalse(risolto.impostato)

    async def test_l_impostazione_propria_ha_la_precedenza(self):
        await imposta_modello("organo", "mistral", "ministral-8b-latest", self.db)
        await imposta_modello("componente", "openrouter", "qwen/qwen3.8-27b:free", self.db)

        risolto = await self.risolvi("componente")

        self.assertEqual((risolto.provider, risolto.model, risolto.origine), ("openrouter", "qwen/qwen3.8-27b:free", "componente"))

    async def test_senza_impostazione_propria_si_eredita_dal_padre_e_dal_nonno(self):
        await imposta_modello("organo", "mistral", "ministral-8b-latest", self.db)

        for nome in ("componente", "sottocomponente"):
            risolto = await self.risolvi(nome)
            self.assertEqual((risolto.provider, risolto.model, risolto.origine), ("mistral", "ministral-8b-latest", "organo"), nome)

    async def test_il_brain_e_la_radice_dell_ereditarieta(self):
        await imposta_modello("brain", "google_studio", None, self.db)

        risolto = await self.risolvi("sottocomponente")

        self.assertEqual((risolto.provider, risolto.model, risolto.origine), ("google_studio", None, "Brain"))
        self.assertEqual((await leggi_modello("Brain", self.db))[0], "google_studio")

    async def test_il_provider_del_padre_non_si_mescola_con_il_modello_del_figlio(self):
        await imposta_modello("organo", "mistral", "ministral-8b-latest", self.db)
        await imposta_modello("componente", "openrouter", None, self.db)

        risolto = await self.risolvi("sottocomponente")

        self.assertEqual((risolto.provider, risolto.model), ("openrouter", None))

    async def test_rimuovere_l_impostazione_ripristina_l_ereditarieta(self):
        await imposta_modello("organo", "mistral", None, self.db)
        await imposta_modello("componente", "openrouter", None, self.db)

        self.assertTrue(await rimuovi_modello("componente", self.db))
        self.assertFalse(await rimuovi_modello("componente", self.db))

        self.assertEqual((await self.risolvi("componente")).provider, "mistral")

    async def test_spostare_un_agente_cambia_il_modello_ereditato(self):
        await self.agente("altro_organo")
        await imposta_modello("organo", "mistral", None, self.db)
        await imposta_modello("altro_organo", "openrouter", None, self.db)

        await self.agente("componente", padre="altro_organo")

        self.assertEqual((await self.risolvi("componente")).provider, "openrouter")

    async def test_i_nomi_si_confrontano_senza_distinguere_le_maiuscole(self):
        await imposta_modello("Organo", "mistral", None, self.db)
        self.assertEqual((await self.risolvi("ORGANO")).provider, "mistral")
        self.assertEqual((await leggi_modello("organo", self.db))[0], "mistral")

    async def test_un_agente_fuori_dal_registro_eredita_dal_brain(self):
        await imposta_modello("Brain", "mistral", None, self.db)
        self.assertEqual((await self.risolvi("agente_non_registrato")).provider, "mistral")

    async def test_database_senza_tabelle_o_illeggibile_non_solleva_errori(self):
        vuoto = os.path.join(self._cartella.name, "vuoto.db")
        self.assertEqual((await risolvi_modello("organo", vuoto)).origine, "predefinito")
        inesistente = os.path.join(self._cartella.name, "cartella_inesistente", "x.db")
        self.assertEqual((await risolvi_modello("organo", inesistente)).origine, "predefinito")

    async def test_un_ciclo_nei_dati_non_causa_un_ciclo_infinito(self):
        async with aiosqlite.connect(self.db) as db:
            await db.execute("UPDATE agents_registry SET parent_agent_name = 'sottocomponente' WHERE name = 'organo'")
            await db.commit()
        self.assertEqual((await self.risolvi("componente")).origine, "predefinito")


class ModelloInAskBrainTest(unittest.IsolatedAsyncioTestCase):
    def crea_agente(self):
        class Agente(BaseAgent):
            async def process(self, *a):
                return {}

        agente = Agente("agente_modello_prova", ["lampada"], 30, 1.0)
        agente.mao.call_model = AsyncMock(return_value="DECISIONE: NONE")
        return agente

    async def test_l_agente_usa_il_modello_risolto(self):
        agente = self.crea_agente()
        with patch("app.agents.base_agent.risolvi_modello", AsyncMock(return_value=ModelloRisolto("mistral", "ministral-8b-latest", "organo"))):
            await agente.ask_brain("s", "u")

        kwargs = agente.mao.call_model.await_args.kwargs
        self.assertEqual((kwargs["provider"], kwargs["model"]), ("mistral", "ministral-8b-latest"))

    async def test_senza_impostazioni_si_usa_il_predefinito_globale(self):
        agente = self.crea_agente()
        with patch("app.agents.base_agent.risolvi_modello", AsyncMock(return_value=ModelloRisolto(None, None, "predefinito"))):
            await agente.ask_brain("s", "u")

        kwargs = agente.mao.call_model.await_args.kwargs
        self.assertEqual((kwargs["provider"], kwargs["model"]), (None, None))

    async def test_un_provider_indicato_esplicitamente_ha_la_precedenza(self):
        agente = self.crea_agente()
        risolvi = AsyncMock(return_value=ModelloRisolto("mistral", None, "organo"))
        with patch("app.agents.base_agent.risolvi_modello", risolvi):
            await agente.ask_brain("s", "u", provider="openrouter", model="altro")

        risolvi.assert_not_awaited()
        self.assertEqual(agente.mao.call_model.await_args.kwargs["provider"], "openrouter")

    async def test_ogni_agente_usa_il_nome_proprio_per_risolvere(self):
        agente = self.crea_agente()
        risolvi = AsyncMock(return_value=ModelloRisolto(None, None, "predefinito"))
        with patch("app.agents.base_agent.risolvi_modello", risolvi):
            await agente.ask_brain("s", "u")
        risolvi.assert_awaited_once_with("agente_modello_prova")


class ProviderTest(unittest.IsolatedAsyncioTestCase):
    def test_alias_e_nomi_canonici(self):
        for alias, canonico in [("MistralAI", "mistral"), ("google", "google_studio"), ("Gemini", "google_studio"),
                                ("or", "openrouter"), ("local", "local"), (None, ""), ("auto", "auto")]:
            self.assertEqual(normalizza_provider(alias), canonico)

    async def test_descrizione_del_provider_con_i_default_del_suo_modello(self):
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "chiave", "MISTRAL_MODEL": "ministral-8b-latest", "DEFAULT_PROVIDER": "mistral"}):
            mao = Mao()
        self.addAsyncCleanup(mao.aclose)
        self.assertEqual(mao.descrivi_provider("mistral", None)["model"], "ministral-8b-latest")
        self.assertEqual(mao.descrivi_provider(None, None)["provider"], "mistral")
        self.assertEqual(mao.descrivi_provider("mistral", "altro")["model"], "altro")


class EndpointModelliTest(DatabaseTemporaneoMixin, unittest.IsolatedAsyncioTestCase):
    """Chiamate dirette agli endpoint con un database temporaneo e chiavi di prova per i provider."""

    async def asyncSetUp(self):
        await self.preparazione_db()
        self.enterContext(patch.dict(os.environ, {
            "MISTRAL_API_KEY": "chiave-mistral", "OPENROUTER_API_KEY": "chiave-openrouter", "GEMINI_API_KEY": "nessuna",
            "GOOGLE_API_KEY": "", "DEFAULT_PROVIDER": "openrouter", "OPENROUTER_MODEL": "qwen/qwen3.8-27b:free",
        }))
        self.enterContext(patch("app.api.main.registry", self.registro))
        self.enterContext(patch("app.api.main._recompile_system_graph", AsyncMock()))
        for nome in ("imposta_modello", "leggi_modello", "rimuovi_modello", "risolvi_modello"):
            self.enterContext(patch(f"app.api.main.{nome}", functools.partial(getattr(modulo, nome), db_path=self.db)))

    def api(self):
        from app import api
        return api.main

    async def crea(self, **definizione):
        from app.api.main import CreateSubAgentRequest, create_sub_agent
        return await create_sub_agent(CreateSubAgentRequest(agent_definition=json.dumps(definizione)))

    async def imposta(self, agente, provider, model=None):
        from app.api.main import ModelloAgenteRequest, set_agent_model
        return await set_agent_model(agente, ModelloAgenteRequest(provider=provider, model=model))

    async def test_impostare_e_leggere_il_modello_di_un_agente(self):
        await self.agente("organo")

        risposta = await self.imposta("organo", "mistral", "ministral-8b-latest")

        self.assertEqual(risposta["impostazione"], {"provider": "mistral", "model": "ministral-8b-latest"})
        self.assertEqual(risposta["effettivo"]["provider"], "mistral")
        self.assertFalse(risposta["effettivo"]["ereditato"])

    async def test_senza_modello_si_usa_il_default_del_provider(self):
        await self.agente("organo")
        with patch.dict(os.environ, {"MISTRAL_MODEL": "modello-di-default"}):
            risposta = await self.imposta("organo", "MistralAI")
        self.assertEqual((risposta["impostazione"]["provider"], risposta["effettivo"]["model"]), ("mistral", "modello-di-default"))

    async def test_il_figlio_eredita_e_lo_mostra(self):
        from app.api.main import get_agent_model

        await self.agente("organo")
        await self.agente("componente", padre="organo")
        await self.imposta("organo", "mistral", "ministral-8b-latest")

        figlio = await get_agent_model("componente")

        self.assertIsNone(figlio["impostazione"])
        self.assertEqual((figlio["effettivo"]["provider"], figlio["effettivo"]["origine"], figlio["effettivo"]["ereditato"]), ("mistral", "organo", True))

    async def test_senza_impostazioni_vale_il_predefinito_del_sistema(self):
        from app.api.main import get_agent_model

        await self.agente("organo")
        effettivo = (await get_agent_model("organo"))["effettivo"]

        self.assertEqual((effettivo["provider"], effettivo["model"], effettivo["origine"]), ("openrouter", "qwen/qwen3.8-27b:free", "predefinito"))
        self.assertFalse(effettivo["ereditato"])

    async def test_rimuovere_l_impostazione_ripristina_l_ereditarieta(self):
        from app.api.main import delete_agent_model

        await self.agente("organo")
        await self.agente("componente", padre="organo")
        await self.imposta("organo", "mistral")
        await self.imposta("componente", "openrouter")

        risposta = await delete_agent_model("componente")

        self.assertIsNone(risposta["impostazione"])
        self.assertEqual((risposta["effettivo"]["provider"], risposta["effettivo"]["ereditato"]), ("mistral", True))

    async def test_il_brain_puo_avere_il_proprio_modello_e_i_figli_lo_ereditano(self):
        from app.api.main import get_agent_model

        await self.agente("organo")
        risposta = await self.imposta("brain", "mistral", "ministral-3b-latest")

        self.assertEqual(risposta["agent"], "Brain")
        self.assertEqual((await get_agent_model("organo"))["effettivo"]["origine"], "Brain")

    async def test_agente_inesistente_provider_sconosciuto_o_senza_chiave_sono_rifiutati(self):
        await self.agente("organo")

        with self.assertRaises(HTTPException) as inesistente:
            await self.imposta("fantasma", "mistral")
        with self.assertRaises(HTTPException) as sconosciuto:
            await self.imposta("organo", "provider_inventato")
        with self.assertRaises(HTTPException) as senza_chiave:
            await self.imposta("organo", "google_studio")

        self.assertEqual(inesistente.exception.status_code, 404)
        self.assertEqual(sconosciuto.exception.status_code, 422)
        self.assertEqual(senza_chiave.exception.status_code, 422)
        self.assertIn("chiave", senza_chiave.exception.detail)
        self.assertIsNone(await leggi_modello("organo", self.db))

    async def test_la_creazione_di_un_agente_accetta_provider_e_modello(self):
        risposta = await self.crea(name="organ_nuovo", managed_targets=["dev_nuovo"], provider="mistral", model="ministral-8b-latest")

        self.assertEqual(risposta["llm"]["provider"], "mistral")
        self.assertEqual((await leggi_modello("organ_nuovo", self.db)), ("mistral", "ministral-8b-latest"))

    async def test_la_creazione_con_un_provider_non_valido_non_registra_l_agente(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.crea(name="organ_nuovo", managed_targets=["dev_nuovo"], provider="inventato")

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertNotIn("organ_nuovo", [c["name"] for c in await self.registro.get_all_agent_configs()])

    async def test_un_modello_senza_provider_e_rifiutato(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.crea(name="organ_nuovo", managed_targets=["dev_nuovo"], model="ministral-8b-latest")
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_eliminare_un_agente_elimina_la_sua_impostazione(self):
        from app.api.main import delete_agent

        await self.agente("organo")
        await self.imposta("organo", "mistral")

        await delete_agent("organo")

        self.assertIsNone(await leggi_modello("organo", self.db))

    async def test_l_elenco_degli_agenti_riporta_il_modello_effettivo(self):
        from app.api.main import list_agents

        await self.agente("organo")
        await self.agente("componente", padre="organo")
        await self.imposta("organo", "mistral")

        elenco = {a["name"]: a["llm"] for a in (await list_agents())["agents"]}

        self.assertEqual((elenco["organo"]["provider"], elenco["organo"]["ereditato"]), ("mistral", False))
        self.assertEqual((elenco["componente"]["provider"], elenco["componente"]["ereditato"]), ("mistral", True))


if __name__ == "__main__":
    unittest.main()
