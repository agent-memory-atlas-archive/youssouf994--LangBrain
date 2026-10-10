"""
Più processi (worker) che servono la stessa API.

Con `python -m app.serve --workers N` (o `LANGBRAIN_WORKERS=N`) l'API gira in N processi che condividono porta e
database SQLite. Ciò che per natura è unico (i compiti in background, un'azione su una stessa risorsa) e ciò che prima
viveva solo in memoria (la configurazione HITL a runtime, il valore di un effettore simulato, l'interruttore di un nodo, il
grafo compilato) passa da qui:

  - `compito_leader`: un compito in background gira in un solo processo alla volta (un contratto di affitto su SQLite:
    se il processo muore, un altro lo rileva entro pochi secondi);
  - `blocco`: mutua esclusione tra processi su una risorsa (es. la decisione su una pratica);
  - `leggi` / `scrivi` / `versione`: piccolo stato condiviso (tabella `runtime_state`) con un numero di versione, così
    ogni processo si accorge che un altro ha cambiato qualcosa.

Con un solo processo (il caso predefinito) nulla di tutto ciò tocca il database: `condiviso()` è falso, i compiti partono
direttamente e i blocchi non fanno nulla. Perciò i test e il comportamento di sempre non cambiano.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

import aiosqlite

from app.db.database import DB_PATH

logger = logging.getLogger(__name__)

ASSENTE: Any = object()
DURATA_CONTRATTO_SECONDI = 15.0
_PROCESSO = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_pronte: set[str] = set()


def lavoratori() -> int:
    """Quanti processi servono l'API (`LANGBRAIN_WORKERS`, impostato da `app.serve`); 1 se non indicato."""
    try:
        return max(1, int(os.getenv("LANGBRAIN_WORKERS", "1")))
    except ValueError:
        return 1


def condiviso() -> bool:
    """True se più processi condividono lo stato (o se `LANGBRAIN_SHARED_STATE=1`, per provarlo con un processo solo)."""
    return lavoratori() > 1 or os.getenv("LANGBRAIN_SHARED_STATE") == "1"


async def _connetti(percorso: str | None = None) -> aiosqlite.Connection:
    percorso = percorso or DB_PATH
    if os.path.dirname(percorso):
        os.makedirs(os.path.dirname(percorso), exist_ok=True)
    db = await aiosqlite.connect(percorso, timeout=30)
    if percorso not in _pronte:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                         "version INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL)")
        await db.execute("CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)")
        await db.commit()
        _pronte.add(percorso)
    return db


async def abilita_wal(db: aiosqlite.Connection) -> None:
    """Con più processi SQLite va in modalità WAL (i lettori non bloccano chi scrive); con uno solo resta com'è."""
    if condiviso():
        await db.execute("PRAGMA journal_mode=WAL")


# ------------------------------------------------------------------------------------------ stato condiviso

async def leggi(chiave: str, default: Any = None, percorso: str | None = None) -> Any:
    db = await _connetti(percorso)
    try:
        async with db.execute("SELECT value FROM runtime_state WHERE key = ?", (chiave,)) as cursor:
            riga = await cursor.fetchone()
    finally:
        await db.close()
    return default if riga is None else json.loads(riga[0])


async def scrivi(chiave: str, valore: Any, percorso: str | None = None) -> int:
    """Scrive il valore e restituisce la nuova versione (cresce a ogni scrittura)."""
    db = await _connetti(percorso)
    try:
        await db.execute(
            "INSERT INTO runtime_state (key, value, version, updated_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, version = version + 1, updated_at = excluded.updated_at",
            (chiave, json.dumps(valore, default=str), time.time()))
        await db.commit()
        async with db.execute("SELECT version FROM runtime_state WHERE key = ?", (chiave,)) as cursor:
            return (await cursor.fetchone())[0]
    finally:
        await db.close()


async def versioni(*chiavi: str, percorso: str | None = None) -> dict[str, int]:
    """La versione di ciascuna chiave (0 se non è mai stata scritta)."""
    db = await _connetti(percorso)
    try:
        segnaposto = ",".join("?" for _ in chiavi)
        async with db.execute(f"SELECT key, version FROM runtime_state WHERE key IN ({segnaposto})", chiavi) as cursor:
            trovate = dict(await cursor.fetchall())
    finally:
        await db.close()
    return {c: trovate.get(c, 0) for c in chiavi}


# ------------------------------------------------------------------------------------------ contratti (leader e blocchi)

async def _acquisisci(nome: str, proprietario: str, durata: float, percorso: str | None = None) -> bool:
    """Prende (o rinnova) il contratto `nome` se è libero, scaduto o già nostro."""
    db = await _connetti(percorso)
    try:
        await db.execute("BEGIN IMMEDIATE")
        adesso = time.time()
        async with db.execute("SELECT owner, expires FROM leases WHERE name = ?", (nome,)) as cursor:
            riga = await cursor.fetchone()
        if riga is None or riga[1] <= adesso or riga[0] == proprietario:
            await db.execute("INSERT INTO leases (name, owner, expires) VALUES (?, ?, ?) "
                             "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, expires = excluded.expires",
                             (nome, proprietario, adesso + durata))
            await db.commit()
            return True
        await db.rollback()
        return False
    finally:
        await db.close()


async def _rilascia(nome: str, proprietario: str, percorso: str | None = None) -> None:
    db = await _connetti(percorso)
    try:
        await db.execute("DELETE FROM leases WHERE name = ? AND owner = ?", (nome, proprietario))
        await db.commit()
    finally:
        await db.close()


async def compito_leader(nome: str, fabbrica: Callable[[], Awaitable[None]], durata: float = DURATA_CONTRATTO_SECONDI,
                         percorso: str | None = None, proprietario: str | None = None) -> None:
    """
    Esegue `fabbrica()` in un solo processo alla volta. Con un processo solo la esegue e basta. Con più processi chi
    ottiene il contratto `nome` la lancia e lo rinnova; gli altri riprovano ogni `durata / 3` secondi e subentrano se il
    leader sparisce. Se il contratto si perde il compito viene fermato.
    """
    if not condiviso():
        await fabbrica()
        return
    contratto = f"leader:{nome}"
    proprietario = proprietario or _PROCESSO
    compito: asyncio.Task | None = None
    try:
        while True:
            try:
                avuto = await _acquisisci(contratto, proprietario, durata, percorso)
            except Exception as errore:  # il database non risponde: non si può essere leader
                logger.warning("Contratto %s non rinnovato: %s", nome, errore)
                avuto = False
            if avuto and compito is None:
                logger.info("Questo processo (%s) esegue il compito %s.", os.getpid(), nome)
                compito = asyncio.create_task(fabbrica())
            elif avuto and compito.done() and (compito.cancelled() or compito.exception()):
                logger.error("Il compito %s si è fermato (%s): riparte.", nome, compito.exception() or "annullato")
                compito = asyncio.create_task(fabbrica())
            elif not avuto and compito is not None:
                logger.warning("Contratto %s perso: il compito si ferma in questo processo.", nome)
                compito.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await compito
                compito = None
            await asyncio.sleep(durata / 3)
    finally:
        if compito is not None:
            compito.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await compito
            with contextlib.suppress(Exception):
                await _rilascia(contratto, proprietario, percorso)


@contextlib.asynccontextmanager
async def blocco(nome: str, attesa: float = 60.0, durata: float = 120.0, percorso: str | None = None) -> AsyncIterator[None]:
    """
    Mutua esclusione tra processi su `nome` (es. `pratica:p_1`). Con un processo solo non fa nulla. Se non si ottiene entro
    `attesa` secondi solleva TimeoutError; il contratto scade da solo dopo `durata` secondi se il processo muore.
    """
    if not condiviso():
        yield
        return
    contratto = f"blocco:{nome}"
    mio = f"{_PROCESSO}:{uuid.uuid4().hex[:6]}"
    limite = time.monotonic() + attesa
    pausa = 0.02
    while not await _acquisisci(contratto, mio, durata, percorso):
        if time.monotonic() >= limite:
            raise TimeoutError(f"Risorsa '{nome}' occupata da un altro processo.")
        await asyncio.sleep(pausa)
        pausa = min(pausa * 1.5, 0.25)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await _rilascia(contratto, mio, percorso)
