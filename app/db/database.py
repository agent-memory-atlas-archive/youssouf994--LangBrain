import aiosqlite
import os
import logging

from app.core.configurazione import get_configurazione

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "langbrain.db")

class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    async def init_db(self):
        dirname = os.path.dirname(self.db_path)
        if dirname:
            try:
                os.makedirs(dirname, exist_ok=True)
            except OSError as e:
                logger.error(f"Errore durante la creazione della cartella: {e}")
                raise

        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                from app.core import multiprocesso  # import tardivo: multiprocesso usa DB_PATH di questo modulo

                await multiprocesso.abilita_wal(db)
                # 1. Tabella Eventi / Audit Log
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target TEXT NOT NULL,
                        old_value TEXT,
                        new_value TEXT,
                        reasoning TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        escalated BOOLEAN DEFAULT 0
                    )
                """)

                # 2. Auto-migrazione: aggiunta colonne mancanti se lo schema è obsoleto
                async with db.execute("PRAGMA table_info(events)") as cursor:
                    columns = [row[1] for row in await cursor.fetchall()]
                    if "old_value" not in columns:
                        logger.info("Migrazione DB: aggiunta colonna 'old_value'...")
                        await db.execute("ALTER TABLE events ADD COLUMN old_value TEXT")
                    if "new_value" not in columns:
                        logger.info("Migrazione DB: aggiunta colonna 'new_value'...")
                        await db.execute("ALTER TABLE events ADD COLUMN new_value TEXT")
                
                # 3. Indice per ricerche veloci
                await db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_events_target_ts 
                    ON events(target, timestamp DESC)
                """)

                # 4. Tabella Segnali (ex `readings`: letture dei recettori)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        receptor_id TEXT NOT NULL,
                        agent_owner TEXT NOT NULL,
                        value REAL NOT NULL,
                        unit TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Nome storico della tabella dei segnali: accettato come alias (vuoto e non usato) finché esistono i vecchi test.
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS readings (
                        reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sensor_id TEXT NOT NULL,
                        agent_owner TEXT NOT NULL,
                        value REAL NOT NULL,
                        unit TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # 5. Esecuzioni dei nodi del grafo (traccia per le statistiche di `GET /graph/stats`)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS node_runs (
                        run_no INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL DEFAULT '',
                        node TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        duration_ms REAL NOT NULL DEFAULT 0,
                        outcome TEXT NOT NULL,
                        detail TEXT NOT NULL DEFAULT ''
                    )
                """)
                await db.execute("CREATE INDEX IF NOT EXISTS idx_node_runs_run ON node_runs(run_id, run_no)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_node_runs_started ON node_runs(started_at)")

                # 6. Conflitto dimostrativo: solo se richiesto da configurazione.toml ([demo] conflitto_all_avvio = 1).
                # Di default il database non riceve dati di prova in automatico.
                if get_configurazione().demo_conflitto_all_avvio:
                    # Elimina vecchi eventi di test simili per evitare accumuli a ogni riavvio
                    await db.execute(
                        "DELETE FROM events WHERE actor = 'agent_security' AND target = 'ac_living_room'"
                    )
                    await db.execute("""
                        INSERT INTO events (actor, action, target, old_value, new_value, reasoning, escalated)
                        VALUES ('agent_security', 'FORCE_SHUTDOWN', 'ac_living_room', '22.5°C', 'OFF',
                                'Simulazione: Finestra Aperta!', 0)
                    """)
                    logger.info("Conflitto dimostrativo inserito su 'events' ([demo] conflitto_all_avvio = 1).")

                await db.commit()
                logger.info("Database SQLite inizializzato.")

        except Exception as e:
            logger.error(f"Errore durante l'inizializzazione/migrazione del database: {e}")
            raise
