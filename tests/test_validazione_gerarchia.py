import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.agents.agent_registry import AgenteConFigliError, AgentRegistry, ErroreGerarchia


def definizione(nome, padre="Brain", target=None, **extra):
    return {"name": nome, "parent_agent_name": padre, "managed_targets": target or [f"dev_{nome}"], **extra}


class ValidazioneGerarchiaTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        self.registro = AgentRegistry(db_path=os.path.join(self._cartella.name, "registro.db"))
        await self.registro.init_registry_db()

    async def configs(self):
        return {c["name"]: c for c in await self.registro.get_all_agent_configs()}

    async def test_padre_inesistente_viene_rifiutato(self):
        with self.assertRaisesRegex(ErroreGerarchia, "non è registrato"):
            await self.registro.register_agent_config(definizione("componente", padre="organo_fantasma"))
        self.assertNotIn("componente", await self.configs())

    async def test_agente_non_puo_essere_padre_di_se_stesso(self):
        with self.assertRaisesRegex(ErroreGerarchia, "padre di sé stesso"):
            await self.registro.register_agent_config(definizione("organo", padre="organo"))

    async def test_ciclo_viene_rifiutato_e_il_registro_resta_invariato(self):
        await self.registro.register_agent_config(definizione("a"))
        await self.registro.register_agent_config(definizione("b", padre="a"))
        await self.registro.register_agent_config(definizione("c", padre="b"))
        prima = await self.configs()

        with self.assertRaisesRegex(ErroreGerarchia, "Ciclo"):
            await self.registro.register_agent_config(definizione("a", padre="c"))

        self.assertEqual(await self.configs(), prima)

    async def test_livello_derivato_dal_padre_quando_omesso(self):
        await self.registro.register_agent_config(definizione("organo"))
        registrato = await self.registro.register_agent_config(definizione("componente", padre="organo"))

        self.assertEqual(registrato["level"], 2)
        self.assertEqual((await self.configs())["organo"]["level"], 1)

    async def test_livello_incoerente_viene_rifiutato(self):
        await self.registro.register_agent_config(definizione("organo"))
        with self.assertRaisesRegex(ErroreGerarchia, "incoerente"):
            await self.registro.register_agent_config(definizione("componente", padre="organo", level=5))

    async def test_padre_brain_e_normalizzato_senza_distinguere_le_maiuscole(self):
        registrato = await self.registro.register_agent_config(definizione("organo", padre="bRaIn"))
        self.assertEqual(registrato["parent_agent_name"], "Brain")

        senza_padre = await self.registro.register_agent_config(definizione("altro", padre=None))
        self.assertEqual(senza_padre["parent_agent_name"], "Brain")

    async def test_padre_risolto_ignorando_le_maiuscole_con_nome_canonico(self):
        await self.registro.register_agent_config(definizione("Organo_Sicurezza"))
        registrato = await self.registro.register_agent_config(definizione("porta", padre="organo_sicurezza"))
        self.assertEqual(registrato["parent_agent_name"], "Organo_Sicurezza")

    async def test_figli_derivati_da_parent_agent_name(self):
        await self.registro.register_agent_config(definizione("organo"))
        await self.registro.register_agent_config(definizione("basso", padre="organo", priority_weight=1.0))
        await self.registro.register_agent_config(definizione("alto", padre="organo", priority_weight=9.0))

        self.assertEqual((await self.configs())["organo"]["sub_agent_names"], ["alto", "basso"])

        istanze = await self.registro.build_agent_instances()
        self.assertEqual(istanze["organo"].sub_agent_names, ["alto", "basso"])

    async def test_sub_agent_names_non_registrati_vengono_rifiutati(self):
        with self.assertRaisesRegex(ErroreGerarchia, "non è registrato"):
            await self.registro.register_agent_config(definizione("organo", sub_agent_names=["futuro"]))

    async def test_sub_agent_names_con_altro_padre_vengono_rifiutati(self):
        await self.registro.register_agent_config(definizione("organo_a"))
        await self.registro.register_agent_config(definizione("organo_b"))
        await self.registro.register_agent_config(definizione("figlio", padre="organo_a"))

        with self.assertRaisesRegex(ErroreGerarchia, "unica fonte di verità"):
            await self.registro.register_agent_config(definizione("organo_b", sub_agent_names=["figlio"]))

    async def test_sub_agent_names_coerenti_sono_accettati(self):
        await self.registro.register_agent_config(definizione("organo"))
        await self.registro.register_agent_config(definizione("figlio", padre="organo"))

        registrato = await self.registro.register_agent_config(definizione("organo", sub_agent_names=["figlio"]))
        self.assertEqual(registrato["sub_agent_names"], ["figlio"])

    async def test_nomi_riservati_o_non_validi_vengono_rifiutati(self):
        for nome in ("Brain", "brain", "END", "", "con spazi", "1numero", "x" * 65):
            with self.subTest(nome=nome), self.assertRaises(ErroreGerarchia):
                await self.registro.register_agent_config(definizione(nome))

    async def test_nome_duplicato_con_maiuscole_diverse_viene_rifiutato(self):
        await self.registro.register_agent_config(definizione("Organo"))
        with self.assertRaisesRegex(ErroreGerarchia, "duplicato"):
            await self.registro.register_agent_config(definizione("organo", target=["altro_dev"]))

    async def test_target_condiviso_tra_rami_diversi_viene_rifiutato(self):
        await self.registro.register_agent_config(definizione("ramo_a", target=["lampada"]))
        with self.assertRaisesRegex(ErroreGerarchia, "lampada"):
            await self.registro.register_agent_config(definizione("ramo_b", target=["lampada"]))

    async def test_target_condiviso_con_il_seme_agent_climate_viene_rifiutato(self):
        with self.assertRaisesRegex(ErroreGerarchia, "ac_living_room"):
            await self.registro.register_agent_config(definizione("clima_nuovo", target=["ac_living_room"]))

    async def test_target_condiviso_tra_antenato_e_discendente_e_ammesso(self):
        await self.registro.register_agent_config(definizione("organo", target=["serratura"]))
        await self.registro.register_agent_config(definizione("componente", padre="organo", target=["serratura"]))
        self.assertIn("componente", await self.configs())

    async def test_target_non_stringa_viene_rifiutato(self):
        with self.assertRaisesRegex(ErroreGerarchia, "managed_targets"):
            await self.registro.register_agent_config(definizione("organo", target="serratura"))

    async def test_spostare_un_sottoalbero_ricalcola_i_livelli_dei_discendenti(self):
        await self.registro.register_agent_config(definizione("organo_a"))
        await self.registro.register_agent_config(definizione("organo_b"))
        await self.registro.register_agent_config(definizione("mezzo", padre="organo_a"))
        await self.registro.register_agent_config(definizione("foglia", padre="mezzo"))

        await self.registro.register_agent_config(definizione("mezzo", padre="Brain"))

        configs = await self.configs()
        self.assertEqual((configs["mezzo"]["level"], configs["foglia"]["level"]), (1, 2))
        self.assertEqual(configs["organo_a"]["sub_agent_names"], [])
        self.assertEqual(configs["mezzo"]["sub_agent_names"], ["foglia"])

    async def test_agente_nativo_senza_figli_non_puo_essere_padre(self):
        with self.assertRaisesRegex(ErroreGerarchia, "agent_climate"):
            await self.registro.register_agent_config(definizione("figlio_clima", padre="agent_climate"))

    async def test_eliminazione_con_figli_viene_rifiutata(self):
        await self.registro.register_agent_config(definizione("organo"))
        await self.registro.register_agent_config(definizione("figlio", padre="organo"))

        with self.assertRaises(AgenteConFigliError) as ctx:
            await self.registro.delete_agent("organo")

        self.assertEqual(ctx.exception.figli, ["figlio"])
        self.assertIn("organo", await self.configs())

    async def test_eliminazione_dal_basso_aggiorna_i_figli_del_padre(self):
        await self.registro.register_agent_config(definizione("organo"))
        await self.registro.register_agent_config(definizione("figlio", padre="organo"))

        self.assertTrue(await self.registro.delete_agent("figlio"))
        self.assertEqual((await self.configs())["organo"]["sub_agent_names"], [])
        self.assertTrue(await self.registro.delete_agent("organo"))

    async def test_eliminazione_di_agente_inesistente_restituisce_false(self):
        self.assertFalse(await self.registro.delete_agent("non_esiste"))

    async def test_registrazioni_concorrenti_non_violano_le_regole(self):
        import asyncio

        risultati = await asyncio.gather(
            *[self.registro.register_agent_config(definizione(f"agente_{i}", target=["condiviso"])) for i in range(5)],
            return_exceptions=True,
        )

        riuscite = [r for r in risultati if not isinstance(r, Exception)]
        self.assertEqual(len(riuscite), 1)
        self.assertTrue(all(isinstance(r, ErroreGerarchia) for r in risultati if isinstance(r, Exception)))


class EndpointGerarchiaTest(unittest.IsolatedAsyncioTestCase):
    """Verifica la mappatura degli errori sugli status HTTP, senza avviare il lifespan né toccare il DB reale."""

    async def asyncSetUp(self):
        self._cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self._cartella.cleanup)
        registro = AgentRegistry(db_path=os.path.join(self._cartella.name, "registro.db"))
        await registro.init_registry_db()

        for bersaglio in (
            patch("app.api.main.registry", registro),
            patch("app.api.main._recompile_system_graph", AsyncMock()),
        ):
            bersaglio.start()
            self.addCleanup(bersaglio.stop)

    async def crea(self, **definizione_agente):
        from app.api.main import CreateSubAgentRequest, create_sub_agent

        return await create_sub_agent(CreateSubAgentRequest(agent_definition=json.dumps(definizione_agente)))

    async def test_creazione_con_padre_inesistente_risponde_422(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.crea(name="componente", parent_agent_name="assente", managed_targets=["dev"])
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_creazione_riporta_livello_e_figli_derivati(self):
        await self.crea(name="organo", managed_targets=["dev_organo"])
        risposta = await self.crea(name="componente", parent_agent_name="organo", managed_targets=["dev_comp"])

        self.assertEqual(risposta["level"], 2)
        self.assertEqual(risposta["parent_agent_name"], "organo")

    async def test_eliminazione_con_figli_risponde_409(self):
        from app.api.main import delete_agent

        await self.crea(name="organo", managed_targets=["dev_organo"])
        await self.crea(name="componente", parent_agent_name="organo", managed_targets=["dev_comp"])

        with self.assertRaises(HTTPException) as ctx:
            await delete_agent("organo")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("componente", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
