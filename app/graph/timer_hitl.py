"""
Timer di attesa dell'operatore per le richieste Human-in-the-Loop.

Quando il grafo si ferma in attesa dell'operatore (interrupt di qualunque tipo: approvazione di un nodo o di
un'escalation, guasto di un dispositivo, LLM non utilizzabile) e il timer è attivo in `configurazione.toml`
([hitl] timer_attivo = 1), il gestore registra la scadenza in `hitl_scadenze` e ne espone il tempo rimanente.
Alla scadenza applica l'azione scelta in configurazione ([hitl] azione_alla_scadenza):

  sistema    decide il sistema: la richiesta torna al grafo con la decisione SISTEMA e prosegue come se l'HITL non
             fosse configurato (il Brain la valuta con il suo modello). Ogni tipo di pausa la interpreta a modo suo,
             vedi `DECISIONE_SISTEMA` in hitl_config.
  respingi   la richiesta viene respinta in automatico (RESPINGI): nessuna azione fisica viene eseguita.
  umano      il grafo resta in pausa e l'API segnala soltanto che il timer è scaduto.

La durata è `max_wait_seconds` di POST /hitl/config se impostato, altrimenti `timer_predefinito_secondi`.
Le scadenze sono su SQLite, quindi sopravvivono ai riavvii insieme agli interrupt del checkpointer.

Lo stesso gestore serve anche altri grafi (per esempio quello di un nodo installato): a chi lo crea basta passare il
grafo, il database, e se servono regole proprie quando il timer è attivo, quanto dura, che cosa fa alla scadenza e
per quali tipi di interrupt vale.
"""

import asyncio
import contextlib
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Callable

import aiosqlite

from app.core import multiprocesso
from langgraph.types import Command

from app.core.configurazione import SCADENZA_SISTEMA, SCADENZA_UMANO, get_configurazione
from app.db.database import DB_PATH
from app.graph.hitl_config import DECISIONE_SISTEMA, hitl_manager

logger = logging.getLogger(__name__)

INTERVALLO_CONTROLLO_SECONDI = 1.0

STATO_ATTESA = "attesa"
STATO_SCADUTA_IN_ATTESA = "scaduta_in_attesa"
STATO_ERRORE = "errore"

_CREAZIONE_TABELLA = """
    CREATE TABLE IF NOT EXISTS hitl_scadenze (
        thread_id TEXT PRIMARY KEY,
        interrupt_id TEXT NOT NULL,
        tipo TEXT,
        creato_il REAL NOT NULL,
        scade_il REAL NOT NULL,
        durata_secondi INTEGER NOT NULL,
        stato TEXT NOT NULL DEFAULT 'attesa'
    )
"""


def timer_attivo() -> bool:
    return get_configurazione().hitl_timer_attivo


def durata_timer_secondi() -> int | None:
    """Durata del timer in secondi, oppure None se il timer non è attivo."""
    if not timer_attivo():
        return None
    da_api = hitl_manager.get_config().max_wait_seconds
    return da_api if isinstance(da_api, int) and da_api > 0 else get_configurazione().hitl_timer_secondi


def attesa_massima_secondi() -> int | None:
    """Attesa massima mostrata all'operatore nei payload: la durata del timer se attivo, altrimenti il solo metadato."""
    return durata_timer_secondi() or hitl_manager.get_config().max_wait_seconds


def _iso(epoca: float) -> str:
    return datetime.fromtimestamp(epoca, tz=timezone.utc).isoformat(timespec="seconds")


class GestoreTimerHitl:
    def __init__(self, ottieni_grafo: Callable[[], Any], db_path: str = DB_PATH, orologio: Callable[[], float] = time.time,
                 attivo: Callable[[], bool] = timer_attivo, durata: Callable[[], int | None] = durata_timer_secondi,
                 azione: Callable[[], str] | None = None, tipi: frozenset[str] | None = None):
        """`tipi`: i tipi di interrupt (campo "type" del payload) con il timer; None: tutti."""
        self._ottieni_grafo = ottieni_grafo
        self.db_path = db_path
        self._orologio = orologio
        self._attivo = attivo
        self._durata = durata
        self._azione = azione or (lambda: get_configurazione().hitl_azione_alla_scadenza)
        self._tipi = tipi
        self._blocchi: dict[str, asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def blocco(self, thread_id: str):
        """
        Serializza le operazioni sullo stesso thread: una ripresa dell'operatore e una scadenza non si sovrappongono, né
        nello stesso processo né, con più processi, tra processi diversi (app/core/multiprocesso.py).
        """
        async with self._blocchi.setdefault(thread_id, asyncio.Lock()):
            async with multiprocesso.blocco(f"thread:{thread_id}", attesa=300, durata=600):
                yield

    async def assicura_tabella(self) -> None:
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            await db.execute(_CREAZIONE_TABELLA)
            await db.commit()

    # ------------------------------------------------------------------ lettura dello stato del grafo

    async def _interrupt_corrente(self, thread_id: str) -> tuple[str | None, str | None]:
        """(id, tipo) dell'interrupt in attesa sul thread, oppure (None, None)."""
        grafo = self._ottieni_grafo()
        if grafo is None:
            return None, None
        istantanea = await grafo.aget_state({"configurable": {"thread_id": thread_id}})
        for task in istantanea.tasks:
            for interruzione in task.interrupts:
                valore = getattr(interruzione, "value", None)
                tipo = str(valore.get("type", "sconosciuto")) if isinstance(valore, dict) else "sconosciuto"
                return str(getattr(interruzione, "id", None) or interruzione), tipo
        return None, None

    # ------------------------------------------------------------------ registrazione e stato

    async def _riga(self, thread_id: str):
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            async with db.execute(
                "SELECT interrupt_id, tipo, creato_il, scade_il, durata_secondi, stato FROM hitl_scadenze WHERE thread_id = ?",
                (thread_id,),
            ) as cursore:
                return await cursore.fetchone()

    async def _elimina(self, thread_id: str) -> None:
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            await db.execute("DELETE FROM hitl_scadenze WHERE thread_id = ?", (thread_id,))
            await db.commit()

    async def sincronizza(self, thread_id: str) -> dict[str, Any]:
        """
        Allinea la scadenza registrata con l'interrupt realmente in attesa sul thread: ne registra una nuova se serve
        (senza azzerare quella di un interrupt già noto) o la elimina se l'attesa è finita. Restituisce lo stato del timer.
        """
        interrupt_id, tipo = await self._interrupt_corrente(thread_id)
        if interrupt_id is None:
            await self._elimina(thread_id)
            return self._stato(None, in_pausa=False)
        if not self._attivo() or (self._tipi is not None and tipo not in self._tipi):
            await self._elimina(thread_id)
            return self._stato(None, in_pausa=True, tipo=tipo)

        riga = await self._riga(thread_id)
        if riga is None or riga[0] != interrupt_id:
            ora = self._orologio()
            durata = self._durata()
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await db.execute(
                    """INSERT OR REPLACE INTO hitl_scadenze (thread_id, interrupt_id, tipo, creato_il, scade_il, durata_secondi, stato)
                       VALUES (?, ?, ?, ?, ?, ?, 'attesa')""",
                    (thread_id, interrupt_id, tipo, ora, ora + durata, durata),
                )
                await db.commit()
            riga = await self._riga(thread_id)
        return self._stato(riga, in_pausa=True, tipo=tipo)

    async def stato(self, thread_id: str) -> dict[str, Any]:
        """Stato del timer del thread (registra la scadenza se l'interrupt è nuovo)."""
        try:
            return await self.sincronizza(thread_id)
        except aiosqlite.OperationalError:
            await self.assicura_tabella()
            return await self.sincronizza(thread_id)

    def _stato(self, riga, in_pausa: bool, tipo: str | None = None) -> dict[str, Any]:
        stato: dict[str, Any] = {
            "attivo": self._attivo(),
            "in_pausa": in_pausa,
            "tipo": tipo,
            "azione_alla_scadenza": self._azione(),
        }
        if riga is None:
            stato.update(secondi_totali=None, secondi_rimanenti=None, scade_il=None, scaduto=False)
            return stato
        _, tipo_riga, _, scade_il, durata, stato_riga = riga
        rimanenti = max(0, math.ceil(scade_il - self._orologio()))
        stato.update(
            tipo=tipo_riga or tipo,
            secondi_totali=durata,
            secondi_rimanenti=rimanenti,
            scade_il=_iso(scade_il),
            scaduto=rimanenti == 0 or stato_riga != STATO_ATTESA,
            stato_timer=stato_riga,
        )
        return stato

    async def elenco(self) -> list[dict[str, Any]]:
        """Tutte le richieste in attesa con scadenza registrata, dalla più urgente."""
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            try:
                async with db.execute(
                    "SELECT thread_id, interrupt_id, tipo, creato_il, scade_il, durata_secondi, stato FROM hitl_scadenze ORDER BY scade_il"
                ) as cursore:
                    righe = await cursore.fetchall()
            except aiosqlite.OperationalError:
                return []
        risultato = []
        for thread_id, interrupt_id, tipo, creato, scade, durata, stato_riga in righe:
            voce = self._stato((interrupt_id, tipo, creato, scade, durata, stato_riga), in_pausa=True, tipo=tipo)
            risultato.append({"thread_id": thread_id, **voce})
        return risultato

    # ------------------------------------------------------------------ scadenza

    async def processa_scadute(self) -> list[str]:
        """
        Applica l'azione di scadenza alle richieste il cui timer è finito. Restituisce i thread ripresi dal sistema
        (respinti o lasciati decidere al Brain). Se nel frattempo l'operatore ha già risposto non fa nulla.
        """
        if not self._attivo():
            return []
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            try:
                async with db.execute(
                    "SELECT thread_id, interrupt_id, durata_secondi FROM hitl_scadenze WHERE stato = ? AND scade_il <= ?",
                    (STATO_ATTESA, self._orologio()),
                ) as cursore:
                    scadute = await cursore.fetchall()
            except aiosqlite.OperationalError:
                return []

        respinti: list[str] = []
        for thread_id, interrupt_id, durata in scadute:
            async with self.blocco(thread_id):
                corrente, _ = await self._interrupt_corrente(thread_id)
                if corrente != interrupt_id:
                    await self.sincronizza(thread_id)  # già gestita dall'operatore
                    continue
                azione = self._azione()
                if azione == SCADENZA_UMANO:
                    await self._imposta_stato(thread_id, STATO_SCADUTA_IN_ATTESA)
                    logger.warning("[TimerHITL] Timer scaduto sul thread '%s': il grafo resta in attesa dell'operatore.", thread_id)
                    continue
                if azione == SCADENZA_SISTEMA:
                    decisione = {
                        "decision": DECISIONE_SISTEMA,
                        "reasoning": f"Timer HITL scaduto: nessuna risposta dell'operatore entro {durata}s, decide il sistema.",
                    }
                else:
                    decisione = {
                        "decision": "RESPINGI",
                        "reasoning": f"Timer HITL scaduto: nessuna risposta dell'operatore entro {durata}s, respinto automaticamente.",
                    }
                try:
                    logger.warning(
                        "[TimerHITL] Timer di %ss scaduto sul thread '%s': azione '%s'.", durata, thread_id, azione,
                    )
                    await self._ottieni_grafo().ainvoke(
                        Command(resume=decisione), config={"configurable": {"thread_id": thread_id}},
                    )
                except Exception:
                    logger.exception("[TimerHITL] Errore nel respingere la richiesta scaduta del thread '%s'.", thread_id)
                    await self._imposta_stato(thread_id, STATO_ERRORE)
                    continue
                respinti.append(thread_id)
                await self._elimina(thread_id)
                await self.sincronizza(thread_id)  # se il grafo si è fermato di nuovo parte un nuovo timer
        return respinti

    async def _imposta_stato(self, thread_id: str, stato: str) -> None:
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            await db.execute("UPDATE hitl_scadenze SET stato = ? WHERE thread_id = ?", (stato, thread_id))
            await db.commit()

    async def sorveglia(self) -> None:
        """Ciclo di controllo da eseguire come task in background per tutta la vita dell'applicazione."""
        while True:
            try:
                await self.processa_scadute()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[TimerHITL] Errore nel controllo delle scadenze.")
            await asyncio.sleep(INTERVALLO_CONTROLLO_SECONDI)
