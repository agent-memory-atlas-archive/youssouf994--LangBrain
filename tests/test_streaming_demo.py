"""Streaming del grafo (SSE) e pagina dimostrativa. Avviano il lifespan vero (database temporaneo) ma nessun LLM reale."""

import json
import os
import re
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.agents.base_agent import BaseAgent
from app.api.main import _PAGINA_DEMO, app
from app.core.configurazione import costruisci_configurazione, imposta_configurazione
from app.core.errori_llm import ErroreLLM
from app.graph.hitl_config import hitl_manager


def leggi_eventi(risposta) -> list[dict]:
    return [json.loads(blocco[len("data: "):]) for blocco in risposta.text.split("\n\n") if blocco.startswith("data: ")]


class StreamingGrafoTest(unittest.TestCase):
    def setUp(self):
        self.thread = f"test-{uuid.uuid4().hex[:8]}"
        hitl_manager.update_config(hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
        self.addCleanup(hitl_manager.update_config, hitl_all=False, hitl_nodes=[], hitl_targets=[], hitl_actions=[], max_wait_seconds=None)
        self.enterContext(patch.object(BaseAgent, "ask_brain", AsyncMock(side_effect=ErroreLLM("CREDITI_ESAURITI", "crediti finiti"))))
        self.client = self.enterContext(TestClient(app))  # avvia e chiude il lifespan

    def avvia(self):
        return self.client.post("/graph/run/stream", json={"thread_id": self.thread})

    def test_un_ciclo_produce_un_evento_per_nodo_e_si_ferma_quando_il_modello_non_risponde(self):
        risposta = self.avvia()
        eventi = leggi_eventi(risposta)

        self.assertEqual(risposta.status_code, 200)
        self.assertTrue(risposta.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual([e["tipo"] for e in eventi][0], "inizio")
        self.assertEqual(eventi[1]["nodo"], "brain")
        pausa = next(e for e in eventi if e["tipo"] == "pausa")
        self.assertEqual(pausa["richiesta"]["type"], "llm_failure_human_intervention")
        self.assertEqual(pausa["richiesta"]["errore"]["codice"], "CREDITI_ESAURITI")
        self.assertEqual(eventi[-1]["tipo"], "fine")
        self.assertTrue(eventi[-1]["in_pausa"])

    def test_la_ripresa_in_streaming_chiude_la_pausa(self):
        self.avvia()

        risposta = self.client.post("/graph/resume/stream", json={"thread_id": self.thread, "decision": "RESPINGI", "reasoning": "prova"})
        eventi = leggi_eventi(risposta)

        self.assertEqual(risposta.status_code, 200)
        self.assertEqual(eventi[-1]["tipo"], "fine")
        self.assertFalse(eventi[-1]["in_pausa"])
        self.assertNotIn("errore", [e["tipo"] for e in eventi])

    def test_la_ripresa_senza_richiesta_in_attesa_risponde_409(self):
        risposta = self.client.post("/graph/resume/stream", json={"thread_id": "thread-senza-pause", "decision": "APPROVA"})
        self.assertEqual(risposta.status_code, 409)

    def test_un_errore_imprevisto_del_grafo_diventa_un_evento_senza_segreti(self):
        segreto = "sk" + "-" + "abcdef1234567890" + "XYZ"
        with patch.object(BaseAgent, "ask_brain", AsyncMock(side_effect=ValueError(f"boom con {segreto}"))):
            eventi = leggi_eventi(self.avvia())

        errore = next(e for e in eventi if e["tipo"] == "errore")
        self.assertNotIn(segreto, json.dumps(errore))
        self.assertEqual(eventi[-1]["tipo"], "fine")

    def test_l_override_in_streaming_richiede_il_primario(self):
        chiavi = {"API_KEY_PRIMARIO": "chiave-primario-1", "API_KEY_MEDICO_DI_GUARDIA": "chiave-medico-1"}
        with patch.dict(os.environ, chiavi):
            senza = self.client.post("/graph/resume/stream", json={"decision": "OVERRIDE", "reasoning": "x"})
            medico = self.client.post(
                "/graph/resume/stream", json={"decision": "OVERRIDE", "reasoning": "x"}, headers={"X-API-Key": "chiave-medico-1"},
            )
        self.assertEqual(senza.status_code, 401)
        self.assertEqual(medico.status_code, 403)


class PaginaDemoTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)  # senza lifespan: la pagina è statica

    def test_la_pagina_e_pubblica_e_ha_una_csp_restrittiva(self):
        with patch.dict(os.environ, {"API_KEY": "prova"}):
            risposta = self.client.get("/demo")

        self.assertEqual(risposta.status_code, 200)
        self.assertTrue(risposta.headers["content-type"].startswith("text/html"))
        csp = risposta.headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("http", csp)

    def test_la_pagina_non_carica_risorse_esterne_ne_contiene_segreti(self):
        testo = Path(_PAGINA_DEMO).read_text(encoding="utf-8")
        indirizzi = set(re.findall(r"https?://[^\s\"'<>)]+", testo)) - {"http://www.w3.org/2000/svg"}
        self.assertEqual(indirizzi, set())
        self.assertNotRegex(testo, r"(?i)api[_-]?key\s*[:=]\s*[\"'][^\"']{4,}")

    def test_la_pagina_si_disattiva_da_configurazione(self):
        imposta_configurazione(costruisci_configurazione({"demo": {"pagina_web": 0}}))
        self.addCleanup(imposta_configurazione, None)
        self.assertEqual(self.client.get("/demo").status_code, 404)


if __name__ == "__main__":
    unittest.main()
