"""
Indicatori del sistema nervoso: riassume gli eventi del DB per strato dell'organismo (Brain, organi, componenti),
per esito (azioni autonome, escalation, riconciliazioni, rifiuti, override…), per attore, per dispositivo e nel tempo.

Funzioni pure: ricevono gli eventi già letti (NervousSystem) e i livelli degli agenti (AgentRegistry), così si testano
senza database.
"""

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.modelli_agenti import NOME_BRAIN

STRATI = ("brain", "organi", "componenti", "sistema")
CATEGORIE = ("azione", "escalation_aperta", "riconciliata", "respinta", "risolta", "scaduta", "sbloccata",
             "override", "forzata", "guasto_gestito")
MAX_INTERVALLI = 48
LAYER_EN = {"brain": "brain", "organi": "organs", "componenti": "components", "sistema": "other"}
OUTCOME_EN = {"azione": "action", "escalation_aperta": "escalation_open", "riconciliata": "approved_by_brain",
              "respinta": "rejected_by_brain", "risolta": "escalation_resolved", "scaduta": "expired",
              "sbloccata": "unblocked", "override": "override", "forzata": "forced", "guasto_gestito": "fault_handled"}
_FORMATO_DB = "%Y-%m-%d %H:%M:%S"


def categoria_evento(evento: dict[str, Any]) -> str:
    """Esito di un evento, dal nome dell'azione (i prefissi RESOLVED_/EXPIRED_ li aggiunge l'NervousSystem)."""
    azione = str(evento.get("action") or "").upper()
    for prefisso, categoria in (("RESOLVED_", "risolta"), ("EXPIRED_", "scaduta"), ("RECONCILED_", "riconciliata"),
                                ("REJECTED_", "respinta"), ("UNBLOCKED", "sbloccata"), ("TOOL_FAILURE", "guasto_gestito")):
        if azione.startswith(prefisso):
            return categoria
    if str(evento.get("actor") or "") == f"{NOME_BRAIN}_Override":
        return "override"
    if evento.get("escalated") in (1, True, "1"):
        return "escalation_aperta"
    if azione.startswith("FORCE_"):
        return "forzata"
    return "azione"


def _escalation(evento: dict[str, Any], categoria: str) -> bool:
    """Evento nato come escalation, anche se poi risolto o scaduto."""
    return categoria == "escalation_aperta" or "ESCALATION" in str(evento.get("action") or "").upper()


def strato_attore(attore: str, livelli: dict[str, int]) -> tuple[str, int | None]:
    """Strato e livello di chi ha scritto l'evento: il Brain (0), un organo (1), un componente (2+) o il sistema."""
    if attore in (NOME_BRAIN, f"{NOME_BRAIN}_Override"):
        return "brain", 0
    livello = livelli.get(attore)
    if livello is None:
        return "sistema", None
    return ("organi" if livello <= 1 else "componenti"), livello


def _istante(valore: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(valore)[:19], _FORMATO_DB).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def riassumi_eventi(eventi: list[dict[str, Any]], livelli: dict[str, int], finestra_ore: int,
                    adesso: datetime | None = None, esterni: frozenset[str] | set[str] = frozenset()) -> dict[str, Any]:
    """`livelli`: livello di ogni agente noto (registro e organi esterni); `esterni`: quali sono organi esterni."""
    adesso = adesso or datetime.now(timezone.utc)
    per_strato: Counter = Counter()
    per_categoria: Counter = Counter()
    attori: dict[str, dict[str, Any]] = {}
    per_target: Counter = Counter()
    escalation = {"totali": 0, "aperte": 0, "chiuse": 0}
    azioni_periferia = escalation_periferia = 0

    ampiezza = max(1, math.ceil(finestra_ore / MAX_INTERVALLI))
    numero = math.ceil(finestra_ore / ampiezza)
    inizio = adesso.replace(minute=0, second=0, microsecond=0) - timedelta(hours=ampiezza * (numero - 1))
    intervalli = [{"inizio": (inizio + timedelta(hours=ampiezza * i)).isoformat(timespec="minutes"), "per_strato": Counter()}
                  for i in range(numero)]

    for evento in eventi:
        attore = str(evento.get("actor") or "?")
        strato, livello = strato_attore(attore, livelli)
        categoria = categoria_evento(evento)
        per_strato[strato] += 1
        per_categoria[categoria] += 1
        per_target[str(evento.get("target") or "?")] += 1
        voce = attori.setdefault(attore, {"attore": attore, "strato": strato, "livello": livello, "eventi": 0,
                                          "escalation": 0, "esterno": attore in esterni})
        voce["eventi"] += 1
        if _escalation(evento, categoria):
            escalation["totali"] += 1
            voce["escalation"] += 1
            escalation["aperte" if categoria == "escalation_aperta" else "chiuse"] += 1
        if strato != "brain":
            if categoria == "azione":
                azioni_periferia += 1
            elif _escalation(evento, categoria):
                escalation_periferia += 1
        istante = _istante(evento.get("timestamp"))
        if istante is not None and istante >= inizio:
            indice = int((istante - inizio).total_seconds() // (3600 * ampiezza))
            if 0 <= indice < numero:
                intervalli[indice]["per_strato"][strato] += 1

    gestiti = azioni_periferia + escalation_periferia
    # Le chiavi della risposta sono identificatori stabili in inglese (le stesse in ogni lingua).
    return {
        "generated_at": adesso.isoformat(timespec="seconds"),
        "window_hours": finestra_ore,
        "total": len(eventi),
        "by_layer": {LAYER_EN[s]: per_strato.get(s, 0) for s in STRATI},
        "by_outcome": {OUTCOME_EN[c]: per_categoria.get(c, 0) for c in CATEGORIE},
        "escalations": {"total": escalation["totali"], "open": escalation["aperte"], "closed": escalation["chiuse"]},
        "brain_decisions": {"reconciled": per_categoria["riconciliata"], "rejected": per_categoria["respinta"],
                            "override": per_categoria["override"]},
        # Quota dei casi che la periferia (tutti tranne il Brain) ha gestito da sola, senza salire al padre (arco riflesso).
        "autonomy_rate": round(azioni_periferia / gestiti, 4) if gestiti else None,
        "by_actor": [{"actor": a["attore"], "layer": LAYER_EN[a["strato"]], "level": a["livello"], "events": a["eventi"],
                      "escalations": a["escalation"], "external": a["esterno"]}
                     for a in sorted(attori.values(), key=lambda a: (-a["eventi"], a["attore"]))],
        "by_device": [{"device": t, "events": n} for t, n in per_target.most_common(10)],
        "bucket_hours": ampiezza,
        "over_time": [{"start": i["inizio"], "by_layer": {LAYER_EN[s]: i["per_strato"].get(s, 0) for s in STRATI}} for i in intervalli],
        "latest": [
            {k: e.get(k) for k in ("timestamp", "actor", "action", "target", "old_value", "new_value", "escalated")}
            | {"outcome": OUTCOME_EN[categoria_evento(e)]} for e in eventi[:20]
        ],
    }
