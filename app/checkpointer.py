"""
Checkpointer LangGraph persistente su SQLite.

Usa lo stesso file del database applicativo (`DB_PATH`): le tabelle `checkpoints` e `writes`
conservano thread, stato e interrupt HITL pendenti, che sopravvivono a riavvii e ricompilazioni del grafo.
La connessione è dedicata al checkpointer e va aperta nel lifespan dell'applicazione e chiusa allo spegnimento
(un thread aiosqlite non chiuso impedisce l'uscita del processo).

Con più processi (`python -m app.serve --workers N`) il file è in modalità WAL e lo stato che prima stava solo in
memoria (configurazione HITL, valore degli effettori simulati, grafo compilato) è condiviso: vedi `app/core/multiprocesso.py`.
"""

import logging

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core import multiprocesso
from app.db.database import DB_PATH

logger = logging.getLogger(__name__)

# Attesa massima (secondi) su un DB bloccato da un'altra connessione, prima di sollevare "database is locked".
TIMEOUT_DB_SECONDI = 30.0

_connessione: aiosqlite.Connection | None = None
_saver: AsyncSqliteSaver | None = None
_percorso_aperto: str | None = None


async def apri_checkpointer(db_path: str = DB_PATH) -> AsyncSqliteSaver:
    """
    Apre il checkpointer sul file indicato e crea le tabelle se mancano.
    Chiamarla di nuovo con lo stesso file restituisce l'istanza già aperta.
    """
    global _connessione, _saver, _percorso_aperto
    if _saver is not None and _percorso_aperto == db_path:
        return _saver
    if _saver is not None:
        await chiudi_checkpointer()

    connessione = await aiosqlite.connect(db_path, timeout=TIMEOUT_DB_SECONDI)
    try:
        await multiprocesso.abilita_wal(connessione)
        saver = AsyncSqliteSaver(connessione)
        await saver.setup()
    except Exception:
        await connessione.close()
        raise

    _connessione, _saver, _percorso_aperto = connessione, saver, db_path
    logger.info("[Checkpointer] Persistenza LangGraph attiva su SQLite: %s", db_path)
    return saver


async def chiudi_checkpointer() -> None:
    """Chiude la connessione del checkpointer. Sicura da chiamare anche se non è stato aperto."""
    global _connessione, _saver, _percorso_aperto
    connessione = _connessione
    _connessione = _saver = _percorso_aperto = None
    if connessione is not None:
        await connessione.close()


def get_checkpointer() -> AsyncSqliteSaver | None:
    """Restituisce il checkpointer aperto, oppure None se `apri_checkpointer()` non è stata chiamata."""
    return _saver


async def svuota_checkpoint() -> None:
    """Elimina tutti i thread persistiti (usato dal reset di sistema)."""
    if _saver is None:
        return
    async with _saver.lock:
        await _saver.conn.execute("DELETE FROM writes")
        await _saver.conn.execute("DELETE FROM checkpoints")
        await _saver.conn.commit()
