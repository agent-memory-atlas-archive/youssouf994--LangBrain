import logging
import aiosqlite
from datetime import datetime, timedelta, timezone
from app.db.database import DB_PATH

logger = logging.getLogger(__name__)

class NervousSystem:
    """
    Gestisce la lettura e la registrazione degli eventi sul database con storico transizioni old_value -> new_value.
    """
    def __init__(self, target: list[str] | None = None, frequency: int = 240, db_path: str = DB_PATH) -> None:
        self.target = target if target is not None else ["all"]
        self.frequency = frequency
        self.db_path = db_path

    async def get_recent_events(self) -> list[dict]:
        """Recupera gli eventi registrati nella finestra temporale impostata."""
        cutoff_time = (datetime.now(timezone.utc) - timedelta(minutes=self.frequency)).strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                if "all" in self.target:
                    query = "SELECT * FROM events WHERE timestamp >= ? ORDER BY timestamp DESC"
                    params = (cutoff_time,)
                else:
                    placeholders = ",".join("?" for _ in self.target)
                    query = f"SELECT * FROM events WHERE target IN ({placeholders}) AND timestamp >= ? ORDER BY timestamp DESC"
                    params = (*self.target, cutoff_time)
                
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
                    
        except Exception as e:
            logger.error(f"Errore durante il recupero degli eventi recenti: {e}")
            raise

    async def log_event(
        self, 
        actor: str, 
        action: str, 
        target: str, 
        old_value: str,
        new_value: str, 
        reasoning: str, 
        escalated: bool = False
    ) -> None:
        """Registra un nuovo evento con old_value e new_value nel DB."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO events (actor, action, target, old_value, new_value, reasoning, escalated) 
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (actor, action, target, str(old_value), str(new_value), reasoning, escalated)
                )
                await db.commit()
                logger.info(f"[NervousSystem] DB Audit -> {actor} su {target}: {old_value} -> {new_value} ({action})")
        except Exception as e:
            logger.error(f"Errore durante la registrazione dell'evento su {target}: {e}")
            raise

    async def record_signals(self, signals: list[dict]) -> int:
        """
        Salva nella tabella `signals` i segnali numerici (`receptor_id`/`sensor_id`, `agent_owner`, `value`, `unit`);
        quelli non numerici (es. "ON") restano solo nello stato del grafo. Restituisce quanti ne ha scritti.
        Un errore del database viene registrato e rilanciato: chi chiama decide se fermarsi.
        """
        righe = []
        for s in signals:
            try:
                valore = float(s["value"])
            except (KeyError, TypeError, ValueError):
                continue
            righe.append((str(s.get("receptor_id") or s.get("sensor_id")), str(s.get("agent_owner", "")), valore, str(s.get("unit", ""))))
        if not righe:
            return 0
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.executemany("INSERT INTO signals (receptor_id, agent_owner, value, unit) VALUES (?, ?, ?, ?)", righe)
                await db.commit()
            return len(righe)
        except Exception as e:
            logger.error(f"Errore durante la registrazione dei segnali: {e}")
            raise

    async def get_recent_signals(self, hours: float = 4.0, receptor_id: str | None = None) -> list[dict]:
        """Segnali registrati nelle ultime `hours` ore (più recenti per primi), opzionalmente di un solo recettore."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        query, params = "SELECT * FROM signals WHERE timestamp >= ?", [cutoff]
        if receptor_id:
            query, params = query + " AND receptor_id = ?", params + [receptor_id]
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(query + " ORDER BY timestamp DESC, signal_id DESC", params) as cursor:
                    return [dict(row) for row in await cursor.fetchall()]
        except Exception as e:
            logger.error(f"Errore durante il recupero dei segnali: {e}")
            raise

    async def record_node_run(self, run_id: str, node: str, started_at: str, duration_ms: float, outcome: str,
                              detail: str = "", thread_id: str = "") -> None:
        """
        Traccia l'esecuzione di un nodo del grafo (`outcome`: ok, waiting, error, ...). Serve alle statistiche
        (`GET /graph/stats`): un errore qui viene registrato e NON rilanciato, la traccia non deve fermare il grafo.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO node_runs (run_id, thread_id, node, started_at, duration_ms, outcome, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, thread_id, node, started_at, round(duration_ms, 2), outcome[:100], detail[:500]))
                await db.commit()
        except Exception as e:
            logger.warning(f"Traccia dell'esecuzione del nodo '{node}' non registrata: {e}")

    async def get_node_runs(self, hours: float = 24.0, run_id: str | None = None) -> list[dict]:
        """Le esecuzioni dei nodi delle run avviate nelle ultime `hours` ore (o di una sola run), in ordine di esecuzione."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                if run_id:
                    query, params = "SELECT * FROM node_runs WHERE run_id = ? ORDER BY run_no", (run_id,)
                else:
                    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
                    query = ("SELECT * FROM node_runs WHERE run_id IN (SELECT run_id FROM node_runs GROUP BY run_id "
                             "HAVING MIN(started_at) >= ?) ORDER BY run_no")
                    params = (cutoff,)
                async with db.execute(query, params) as cursor:
                    return [dict(row) for row in await cursor.fetchall()]
        except Exception as e:
            logger.error(f"Errore durante il recupero delle esecuzioni dei nodi: {e}")
            raise

    async def mark_resolved(self, target: str) -> int:
        """
        Marca come risolte tutte le escalation ancora aperte del target (qualunque nome abbia l'azione: un agente
        personalizzato può registrarla come vuole, es. CRITICAL_ARHYTHMIA_ESCALATION). Senza questo il Brain
        le rivedrebbe come "non riconciliate" a ogni ciclo. L'UPDATE imposta escalated = 0 e antepone RESOLVED_
        all'azione, così i controlli di conflitto le ignorano. Restituisce quante escalation ha chiuso.
        Un errore del database viene registrato e rilanciato (come in `log_event`): chi chiama decide se fermarsi.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                result = await db.execute(
                    """UPDATE events
                       SET action = 'RESOLVED_' || action, escalated = 0
                       WHERE target = ? AND escalated = 1
                         AND action NOT LIKE 'RESOLVED\\_%' ESCAPE '\\' AND action NOT LIKE 'RECONCILED\\_%' ESCAPE '\\'""",
                    (target,)
                )
                await db.commit()
                if result.rowcount > 0:
                    logger.info(f"[NervousSystem] Marcate {result.rowcount} escalation come RESOLVED per '{target}'.")
                return max(result.rowcount, 0)
        except Exception as e:
            logger.error(f"Errore durante il reset degli eventi di escalation per {target}: {e}")
            raise

    async def unblock_target(self, target: str, reasoning: str, actor: str = "event_producer") -> None:
        """
        Event-Driven Unblock — Sblocca un dispositivo precedentemente in stato di blocco (es. REJECTED).
        Registra l'evento UNBLOCKED nel DB per azzerare lo stato di blocco. Un errore del database viene registrato e
        rilanciato: chi chiama non deve sbloccare il dispositivo se lo sblocco non è stato registrato.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO events (actor, action, target, old_value, new_value, reasoning, escalated)
                       VALUES (?, 'UNBLOCKED', ?, 'REJECTED', 'OFF', ?, 0)""",
                    (actor, target, reasoning)
                )
                # Invalida anche vecchi eventi di blocco pendenti
                await db.execute(
                    """UPDATE events
                       SET action = 'RESOLVED_' || action, escalated = 0
                       WHERE target = ? AND (action LIKE 'REJECTED%' OR action LIKE 'FORCE_SHUTDOWN%')""",
                    (target,)
                )
                await db.commit()
                logger.info(f"[NervousSystem] Target '{target}' sbloccato con successo: {reasoning}")
        except Exception as e:
            logger.error(f"Errore durante lo sblocco del target {target}: {e}")
            raise

    async def expire_old_control_flags(self, ttl_minutes: int = 60) -> int:
        """
        TTL (Time-To-Live) — Marca come 'EXPIRED' tutti gli eventi di blocco/conflitto antecedenti a ttl_minutes.
        Ritorna il numero di eventi scaduti. Un errore del database viene registrato e rilanciato.
        """
        cutoff_time = (datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            async with aiosqlite.connect(self.db_path) as db:
                result = await db.execute(
                    """UPDATE events
                       SET action = 'EXPIRED_' || action, escalated = 0
                       WHERE timestamp < ? AND (escalated = 1 OR action LIKE 'REJECTED%' OR action LIKE 'FORCE_SHUTDOWN%')
                       AND action NOT LIKE 'EXPIRED\\_%' ESCAPE '\\' AND action NOT LIKE 'RESOLVED\\_%' ESCAPE '\\'""",
                    (cutoff_time,)
                )
                await db.commit()
                expired_count = result.rowcount
                if expired_count > 0:
                    logger.info(f"[NervousSystem] TTL: Scaduti {expired_count} vecchi flag di controllo (> {ttl_minutes} min).")
                return expired_count
        except Exception as e:
            logger.error(f"Errore durante l'applicazione del TTL sui flag: {e}")
            raise




# Alias del vecchio nome (retrocompatibilità)
EventLog = NervousSystem
