"""Fase 2: gli esempi di dominio vivono in `examples/`, il core li carica per nome e i vecchi percorsi restano alias."""
import unittest
import warnings

from app.agents.agenti_nativi import AGENTI_NATIVI, crea_agente_nativo


class DomainExamplesTest(unittest.TestCase):
    def test_nuovi_percorsi_e_alias_coincidono(self):
        from examples.smart_home.climate_agent import ClimateAgent
        from examples.medical_homeostasis.medical_agents import CardiovascularOrganAgent
        from examples.medical_homeostasis.medical_tools import LungVentilatorTool
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from app.agents.agent_climate import ClimateAgent as VecchioClima
            from app.agents.medical_agents import CardiovascularOrganAgent as VecchioCardio
            from app.tools.medical_tools import LungVentilatorTool as VecchioPolmone
        self.assertIs(ClimateAgent, VecchioClima)
        self.assertIs(CardiovascularOrganAgent, VecchioCardio)
        self.assertIs(LungVentilatorTool, VecchioPolmone)

    def test_il_core_crea_l_agente_nativo_per_nome(self):
        self.assertIn("agent_climate", AGENTI_NATIVI)
        agente = crea_agente_nativo("agent_climate")
        self.assertEqual(agente.name, "agent_climate")
        self.assertIsNone(crea_agente_nativo("sconosciuto"))

    def test_il_core_non_importa_piu_l_agente_clima_direttamente(self):
        import pathlib
        for percorso in ("app/graph/builder.py", "app/agents/agent_registry.py"):
            self.assertNotIn("agent_climate import", pathlib.Path(percorso).read_text(encoding="utf-8"))
