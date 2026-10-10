"""
Segnali (ex letture): finestra temporale nel reducer e in lettura, tabella `signals` scritta dal readout del Brain.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import aiosqlite

from app.db.database import Database
from app.graph.state import FINESTRA_SEGNALI_ORE, filtra_segnali, finestra_segnali_ore, reduce_readings
from app.tools.nervous_system import NervousSystem


def _signal(hours_ago: float | None, sensor_id: str = "temp") -> dict:
    s = {"sensor_id": sensor_id, "agent_owner": "x", "value": "21.5", "unit": "C"}
    if hours_ago is not None:
        s["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return s


class SignalWindowTest(unittest.TestCase):
    def test_reducer_drops_signals_older_than_default_window(self):
        merged = reduce_readings([_signal(FINESTRA_SEGNALI_ORE + 1), _signal(1)], [_signal(0)])
        self.assertEqual(len(merged), 2)

    def test_signals_without_timestamp_are_kept_and_empty_update_resets(self):
        self.assertEqual(len(reduce_readings([_signal(None)], [_signal(None)])), 2)
        self.assertEqual(reduce_readings([_signal(0)], []), [])

    def test_per_run_window_and_legacy_key(self):
        signals = [_signal(0.5), _signal(3)]
        self.assertEqual(len(filtra_segnali(signals, finestra_segnali_ore({"signals_window_hours": 1}))), 1)
        self.assertEqual(len(filtra_segnali(signals, finestra_segnali_ore({"readings_window_hours": 1}))), 1)
        self.assertEqual(finestra_segnali_ore({"signals_window_hours": "boh"}), FINESTRA_SEGNALI_ORE)
        self.assertEqual(finestra_segnali_ore(None), FINESTRA_SEGNALI_ORE)


class SignalTableTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.mkdtemp(prefix="signals_win_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db_path = os.path.join(self.folder, "signals.db")
        await Database(db_path=self.db_path).init_db()
        self.system = NervousSystem(db_path=self.db_path)

    async def test_numeric_signals_are_stored_and_text_ones_skipped(self):
        written = await self.system.record_signals([_signal(0), {**_signal(0, "ac"), "value": "ON"}])
        self.assertEqual(written, 1)
        rows = await self.system.get_recent_signals()
        self.assertEqual([(r["receptor_id"], r["value"]) for r in rows], [("temp", 21.5)])
        self.assertEqual(await self.system.get_recent_signals(receptor_id="ac"), [])

    async def test_window_excludes_old_rows(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO signals (receptor_id, agent_owner, value, unit, timestamp) "
                             "VALUES ('temp', 'x', 1, 'C', '2000-01-01 00:00:00')")
            await db.commit()
        await self.system.record_signals([_signal(0)])
        self.assertEqual(len(await self.system.get_recent_signals(hours=1)), 1)
        self.assertEqual(await self.system.record_signals([]), 0)
