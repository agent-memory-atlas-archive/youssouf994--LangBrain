"""
Statistiche per i grafici: come il grafo ha eseguito i nodi (Brain, organi, componenti) e come si sono comportati i
segnali numerici dei recettori nel tempo.

Funzioni pure, come `statistiche_eventi`: ricevono le righe già lette dal sistema nervoso e restituiscono strutture
JSON, così si testano senza database. Parametri generici (finestra, dimensione degli intervalli, filtro per recettore
o proprietario): valgono per qualunque dominio, non solo per la casa intelligente.
"""

import math
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_INTERVALLI_SERIE = 96
INIZIO, FINE = "__start__", "__end__"
_FORMATO_DB = "%Y-%m-%d %H:%M:%S"


def _istante_iso(valore: Any) -> datetime | None:
    try:
        istante = datetime.fromisoformat(str(valore))
    except ValueError:
        return None
    return istante if istante.tzinfo else istante.replace(tzinfo=timezone.utc)


def _istante_db(valore: Any) -> datetime | None:
    try:
        return datetime.strptime(str(valore)[:19], _FORMATO_DB).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _quantile(valori: list[float], q: float) -> float | None:
    if not valori:
        return None
    ordinati = sorted(valori)
    return round(ordinati[min(len(ordinati) - 1, int(q * len(ordinati)))], 2)


def riassumi_esecuzioni(righe: list[dict[str, Any]], finestra_ore: float, ora: datetime | None = None) -> dict[str, Any]:
    """
    Le esecuzioni dei nodi (tabella `node_runs`, in ordine di esecuzione) riassunte per nodo e per transizione:
    quante volte è girato ogni nodo, con quali esiti, in quanto tempo (mediana e 90° percentile, l'attesa di una
    persona non conta), da quale nodo a quale si è passati (INIZIO e FINE inclusi), quante run sono ferme in attesa
    e da quanto tempo di mediana hanno aspettato quelle riprese.
    """
    ora = ora or datetime.now(timezone.utc)
    per_run: dict[str, list[dict[str, Any]]] = {}
    for riga in righe:
        per_run.setdefault(str(riga["run_id"]), []).append(riga)

    nodi: dict[str, dict[str, Any]] = {}
    durate: dict[str, list[float]] = {}
    archi: Counter = Counter()
    attese: list[float] = []
    in_attesa = 0
    for esecuzioni in per_run.values():
        percorso: list[str] = []
        for e in esecuzioni:
            nodo = nodi.setdefault(e["node"], {"node": e["node"], "runs": 0, "outcomes": Counter()})
            nodo["runs"] += 1
            nodo["outcomes"][e["outcome"]] += 1
            if e["outcome"] != "waiting":
                durate.setdefault(e["node"], []).append(float(e["duration_ms"]))
            if not percorso or percorso[-1] != e["node"]:
                percorso.append(e["node"])
        for nodo_attesa in {e["node"] for e in esecuzioni if e["outcome"] == "waiting"}:
            ferme = [e for e in esecuzioni if e["node"] == nodo_attesa and e["outcome"] == "waiting"]
            riprese = [e for e in esecuzioni if e["node"] == nodo_attesa and e["outcome"] != "waiting"]
            if ferme and riprese and (a := _istante_iso(ferme[0]["started_at"])) and (b := _istante_iso(riprese[-1]["started_at"])):
                attese.append((b - a).total_seconds() / 3600)
        finita = esecuzioni[-1]["outcome"] != "waiting"
        if not finita:
            in_attesa += 1
        passi = [INIZIO, *percorso] + ([FINE] if finita else [])
        archi.update(zip(passi, passi[1:]))

    for nome, nodo in nodi.items():
        nodo["outcomes"] = dict(nodo["outcomes"].most_common())
        nodo["median_ms"] = _quantile(durate.get(nome, []), 0.5)
        nodo["p90_ms"] = _quantile(durate.get(nome, []), 0.9)
    return {
        "generated_at": ora.isoformat(timespec="seconds"),
        "window_hours": finestra_ore,
        "runs": len(per_run),
        "waiting": in_attesa,
        "nodes": sorted(nodi.values(), key=lambda n: -n["runs"]),
        "edges": [{"from": a, "to": b, "count": n} for (a, b), n in archi.most_common()],
        "wait_hours_median": round(statistics.median(attese), 2) if attese else None,
    }


def riassumi_segnali(righe: list[dict[str, Any]], finestra_ore: float, intervallo_minuti: float | None = None,
                     ora: datetime | None = None) -> dict[str, Any]:
    """
    I segnali numerici (tabella `signals`) per recettore: quanti, minimo, massimo, media, ultimo valore e una serie nel
    tempo a intervalli (`intervallo_minuti`; di default la finestra divisa in al più 48 intervalli). Gli intervalli
    senza segnali restano nella serie con `count` 0, così il grafico non inventa valori.
    """
    ora = ora or datetime.now(timezone.utc)
    finestra_minuti = max(finestra_ore * 60, 1.0)
    if not intervallo_minuti or intervallo_minuti <= 0:
        intervallo_minuti = math.ceil(finestra_minuti / 48)
    intervallo_minuti = max(intervallo_minuti, finestra_minuti / MAX_INTERVALLI_SERIE)
    inizio = ora - timedelta(minutes=finestra_minuti)
    n_intervalli = max(1, math.ceil(finestra_minuti / intervallo_minuti))

    per_recettore: dict[str, list[tuple[datetime, float, dict[str, Any]]]] = {}
    for riga in righe:
        istante = _istante_db(riga.get("timestamp"))
        try:
            valore = float(riga["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if istante is None or istante < inizio or valore != valore or valore in (float("inf"), float("-inf")):
            continue
        per_recettore.setdefault(str(riga["receptor_id"]), []).append((istante, valore, riga))

    recettori = []
    for nome, punti in per_recettore.items():
        punti.sort(key=lambda p: p[0])
        valori = [p[1] for p in punti]
        ultimo = punti[-1]
        celle: list[list[float]] = [[] for _ in range(n_intervalli)]
        for istante, valore, _ in punti:
            indice = int((istante - inizio).total_seconds() // (intervallo_minuti * 60))
            if 0 <= indice < n_intervalli:
                celle[indice].append(valore)
        serie = [{"start": (inizio + timedelta(minutes=i * intervallo_minuti)).isoformat(timespec="seconds"), "count": len(c),
                  "min": min(c) if c else None, "max": max(c) if c else None,
                  "mean": round(sum(c) / len(c), 4) if c else None} for i, c in enumerate(celle)]
        recettori.append({
            "receptor_id": nome,
            "owners": sorted({str(p[2].get("agent_owner", "")) for p in punti} - {""}),
            "unit": ultimo[2].get("unit", ""),
            "count": len(valori), "min": min(valori), "max": max(valori), "mean": round(sum(valori) / len(valori), 4),
            "last": {"value": ultimo[1], "timestamp": ultimo[0].isoformat(timespec="seconds")},
            "series": serie,
        })
    recettori.sort(key=lambda r: -r["count"])
    return {"generated_at": ora.isoformat(timespec="seconds"), "window_hours": finestra_ore,
            "bucket_minutes": round(intervallo_minuti, 2), "signals": sum(r["count"] for r in recettori), "receptors": recettori}
