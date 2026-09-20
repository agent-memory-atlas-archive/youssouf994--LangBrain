"""
Modello LLM specifico per agente, con ereditarietà lungo la gerarchia.

Ogni agente (compreso il Brain) può avere un provider e un modello propri, impostati via API e salvati nella tabella
`agent_models`. Se un agente non ne ha, usa quello del padre, poi quello del nonno e così via fino al Brain; se nessuno
li ha impostati vale il provider predefinito globale (`DEFAULT_PROVIDER` e il modello di quel provider nel `.env`).

Un'impostazione è un'unità: `provider` è obbligatorio e `model` facoltativo (se manca si usa il modello di default di
quel provider). Il modello di un padre non viene mai applicato a un provider diverso.

La risoluzione legge il database a ogni chiamata (poche righe, pochi millisecondi rispetto a una chiamata LLM), così
una modifica via API ha effetto immediato e non serve invalidare cache.
"""

import logging
from dataclasses import dataclass

import aiosqlite

from app.db.database import DB_PATH

logger = logging.getLogger(__name__)

NOME_BRAIN = "Brain"
ORIGINE_PREDEFINITO = "predefinito"

_CREAZIONE_TABELLA = """
    CREATE TABLE IF NOT EXISTS agent_models (
        agent_name TEXT PRIMARY KEY COLLATE NOCASE,
        provider TEXT NOT NULL,
        model TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
"""


@dataclass(frozen=True)
class ModelloRisolto:
    provider: str | None
    model: str | None
    origine: str
    """Nome dell'agente da cui deriva l'impostazione, oppure 'predefinito' se non ce n'è nessuna nella catena."""

    @property
    def impostato(self) -> bool:
        return self.provider is not None


def nome_canonico_brain(nome: str) -> str:
    return NOME_BRAIN if nome.casefold() == NOME_BRAIN.casefold() else nome


async def assicura_tabella(db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_CREAZIONE_TABELLA)
        await db.commit()


async def imposta_modello(nome: str, provider: str, modello: str | None, db_path: str = DB_PATH) -> None:
    """Imposta (o sostituisce) provider e modello propri dell'agente."""
    await assicura_tabella(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO agent_models (agent_name, provider, model) VALUES (?, ?, ?)
               ON CONFLICT(agent_name) DO UPDATE SET provider = excluded.provider, model = excluded.model,
               updated_at = CURRENT_TIMESTAMP""",
            (nome_canonico_brain(nome), provider, modello),
        )
        await db.commit()


async def rimuovi_modello(nome: str, db_path: str = DB_PATH) -> bool:
    """Toglie l'impostazione propria: l'agente torna a ereditare. Restituisce False se non ce n'era una."""
    await assicura_tabella(db_path)
    async with aiosqlite.connect(db_path) as db:
        risultato = await db.execute("DELETE FROM agent_models WHERE agent_name = ?", (nome,))
        await db.commit()
        return risultato.rowcount > 0


async def leggi_modello(nome: str, db_path: str = DB_PATH) -> tuple[str, str | None] | None:
    """Impostazione propria dell'agente (senza ereditarietà), oppure None."""
    await assicura_tabella(db_path)
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT provider, model FROM agent_models WHERE agent_name = ?", (nome,)) as cursore:
            riga = await cursore.fetchone()
    return (riga[0], riga[1]) if riga else None


async def risolvi_modello(nome: str, db_path: str = DB_PATH) -> ModelloRisolto:
    """
    Modello effettivo dell'agente: la propria impostazione, altrimenti quella del padre, e così via fino al Brain.
    Non solleva mai: se il database non è leggibile si usa il predefinito globale.
    """
    predefinito = ModelloRisolto(None, None, ORIGINE_PREDEFINITO)
    try:
        async with aiosqlite.connect(db_path) as db:
            corrente = nome_canonico_brain(nome)
            visitati: set[str] = set()
            while corrente.casefold() not in visitati:
                visitati.add(corrente.casefold())
                try:
                    async with db.execute(
                        "SELECT agent_name, provider, model FROM agent_models WHERE agent_name = ?", (corrente,)
                    ) as cursore:
                        riga = await cursore.fetchone()
                except aiosqlite.OperationalError:
                    return predefinito  # tabella non ancora creata
                if riga:
                    return ModelloRisolto(riga[1], riga[2], riga[0])
                if corrente.casefold() == NOME_BRAIN.casefold():
                    break
                padre = None
                try:
                    async with db.execute(
                        "SELECT parent_agent_name FROM agents_registry WHERE name = ?", (corrente,)
                    ) as cursore:
                        riga_padre = await cursore.fetchone()
                    padre = riga_padre[0] if riga_padre else None
                except aiosqlite.OperationalError:
                    pass
                corrente = nome_canonico_brain(padre or NOME_BRAIN)
    except Exception as e:
        logger.warning("[ModelliAgenti] Risoluzione del modello di '%s' non riuscita (%s): uso il predefinito.", nome, e)
    return predefinito
