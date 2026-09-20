"""
Dimostrazione Omeostasi Fisiologica: Simulazione di una Patologia e Risoluzione da parte degli Agenti Medicali.
Posizione: examples/medical_homeostasis/demo_medical_homeostasis.py
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Risoluzione dinamica della radice del progetto
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Database dedicato alla demo: i tuoi dati non vengono toccati. Va impostato prima di importare l'applicazione.
os.environ.setdefault("DB_PATH", str(ROOT / "demo_medica.db"))

from app.agents.agent_registry import AgentRegistry
from app.agents.medical_agents import CardiovascularOrganAgent, RespiratoryOrganAgent
from app.db.database import DB_PATH
from app.db.scenario import crea_scenario
from app.graph.builder import build_graph
from app.tools.event_log import EventLog
from app.tools.sensor_tools import registra_tool
from app.tools.medical_tools import (
    HeartRateRegulatorTool,
    LungVentilatorTool,
    deterministic_biometric_normalizer,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("demo_medical_homeostasis")


async def main():
    logger.info("=== AVVIO DEMO PATOLOGIA MEDICA & OMEOSTASI FISIOLOGICA ===")

    # 1. Database della demo azzerato a ogni esecuzione, così gli eventi di una prova non influenzano la successiva
    await crea_scenario(DB_PATH, "vuoto")
    event_log = EventLog()

    # Istanzia i tool medici
    pacemaker = HeartRateRegulatorTool()
    ventilator = LungVentilatorTool()
    medical_tools = {
        "cardiac_pacemaker": pacemaker,
        "oxygen_regulator": ventilator,
    }

    # Istanzia gli agenti di organo fisiologico
    # Registrati nel registry condiviso, i tool medici li trova anche il Brain quando approva un'escalation
    for nome, tool in medical_tools.items():
        registra_tool(nome, tool)
    cardio_agent = CardiovascularOrganAgent(tools=medical_tools)
    resp_agent = RespiratoryOrganAgent(tools=medical_tools)

    custom_instances = {
        "organ_cardiovascular": cardio_agent,
        "organ_respiratory": resp_agent,
    }

    # 2. Compila il grafo con gli agenti medici
    graph, shared_tools = build_graph(custom_agent_instances=custom_instances)
    config_cardio = {"configurable": {"thread_id": "medical_cardio"}}
    config_respiro = {"configurable": {"thread_id": "medical_respiratory"}}

    # -------------------------------------------------------------------
    # FASE 1: STATO DI SALUTE INIZIALE (OMEOSTASI NORMALE)
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 1] Stato Fisiologico Iniziale (Sano) ---")
    val_bpm = await pacemaker.get_tool_value()
    val_spo2 = await ventilator.get_tool_value()
    logger.info("Frequenza Cardiaca Attuale: %s | Normalizzazione: %s", val_bpm, pacemaker.normalize_current_state())
    logger.info("Ossigenazione SpO2 Attuale: %s | Normalizzazione: %s", val_spo2, ventilator.normalize_current_state())

    # -------------------------------------------------------------------
    # FASE 2: INSORGENZA PATOLOGIA (CRISI CARDIACA & IPOSSICA)
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 2] Insorgenza Patologia: Tachicardia Severa (160 BPM) & Ipossia (82% SpO2) ---")
    await pacemaker.set_tool_value(160.0)
    await ventilator.set_tool_value(82.0)

    logger.info("Dati Biometrici Alterati:")
    logger.info(" - Pacemaker Alterato: %s | %s", await pacemaker.get_tool_value(), pacemaker.normalize_current_state())
    logger.info(" - Ventilatore Alterato: %s | %s", await ventilator.get_tool_value(), ventilator.normalize_current_state())

    # -------------------------------------------------------------------
    # FASE 3: INTERVENTO DELL'AGENTE CARDIOVASCOLARE (ARITMIA SEVERA -> ESCALATION)
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 3] Invocazione Agente Cardiovascolare per Risoluzione Tachicardia ---")
    state_cardio = {
        "messages": [],
        "readings": [{"sensor_id": "cardiac_pacemaker", "agent_owner": "organ_cardiovascular", "value": "160.0", "unit": "BPM"}],
        "recent_events": [],
        "pending_escalations": [],
        "next_agent": "organ_cardiovascular",
        "hitl_required": False,
        "config": {},
    }

    res_cardio = await graph.ainvoke(state_cardio, config=config_cardio)
    messages_cardio = res_cardio.get("messages", [])
    if messages_cardio:
        logger.info("Esito Agente Cardiovascolare: %s", messages_cardio[-1].content)

    # -------------------------------------------------------------------
    # FASE 3b: ESITO DELL'ESCALATION E, SE SERVE, INTERVENTO DELL'OPERATORE
    # -------------------------------------------------------------------
    # Il Brain valuta l'escalation con il suo modello e può approvarla (il pacemaker è già a 100 BPM) oppure respingerla.
    # Un rifiuto del Brain (priorità massima) blocca il dispositivo: l'agente cardiaco non può forzarlo, serve un
    # operatore che sblocchi. È la catena di responsabilità prevista dal framework.
    logger.info("\n--- [FASE 3b] Esito dell'escalation al Brain sul Pacemaker Cardiaco ---")
    await event_log.mark_resolved("cardiac_pacemaker")
    if pacemaker.normalize_current_state()["is_in_range"]:
        logger.info("Il Brain ha approvato: il pacemaker è già a %s.", await pacemaker.get_tool_value())
    else:
        logger.info("Il Brain ha respinto l'escalation: il pacemaker è bloccato a %s. L'operatore lo sblocca.", await pacemaker.get_tool_value())
        await event_log.unblock_target("cardiac_pacemaker", "Sblocco dell'operatore dopo il rifiuto del Brain", actor="operatore_umano")
        esito = await cardio_agent.applica_stato(
            target="cardiac_pacemaker",
            action="HOMEOSTASIS_BPM_RESTORATION",
            new_value="100.0 BPM",
            reasoning="Ripristino del target omeostatico a 100 BPM dopo lo sblocco dell'operatore.",
            escalated=False,
            tools_map=medical_tools,
        )
        logger.info("Ripristino dopo lo sblocco: %s", esito["status"])

    # -------------------------------------------------------------------
    # FASE 4: INTERVENTO DELL'AGENTE RESPIRATORIO PER RISOLUZIONE IPOSSIA
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 4] Invocazione Agente Respiratorio per Risoluzione Ipossia ---")
    state_resp = {
        "messages": [],
        "readings": [{"sensor_id": "oxygen_regulator", "agent_owner": "organ_respiratory", "value": "82.0", "unit": "%"}],
        "recent_events": await event_log.get_recent_events(),
        "pending_escalations": [],
        "next_agent": "organ_respiratory",
        "hitl_required": False,
        "config": {},
    }

    res_resp = await graph.ainvoke(state_resp, config=config_respiro)
    messages_resp = res_resp.get("messages", [])
    if messages_resp:
        logger.info("Esito Agente Respiratorio: %s", messages_resp[-1].content)

    # -------------------------------------------------------------------
    # FASE 5: VERIFICA POST-INTERVENTO (OMEOSTASI RIPRISTINATA)
    # -------------------------------------------------------------------
    logger.info("\n--- [FASE 5] Verifica Parametri Biometrici dopo l'Intervento degli Agenti ---")
    post_bpm = await pacemaker.get_tool_value()
    post_spo2 = await ventilator.get_tool_value()

    norm_post_bpm = pacemaker.normalize_current_state()
    norm_post_spo2 = ventilator.normalize_current_state()

    logger.info("Post-Intervento Frequenza Cardiaca: %s | In Range: %s | Score: %s", post_bpm, norm_post_bpm["is_in_range"], norm_post_bpm["normalized_score"])
    logger.info("Post-Intervento Ossigenazione SpO2: %s | In Range: %s | Score: %s", post_spo2, norm_post_spo2["is_in_range"], norm_post_spo2["normalized_score"])

    assert norm_post_bpm["is_in_range"] is True, "La frequenza cardiaca deve essere rientrata nel range omeostatico"
    assert norm_post_spo2["is_in_range"] is True, "La saturazione SpO2 deve essere rientrata nel range omeostatico"

    logger.info("\n=== PATOLOGIA RISOLTA CON SUCCESSO DAGLI AGENTI MEDICALI ===")


if __name__ == "__main__":
    asyncio.run(main())