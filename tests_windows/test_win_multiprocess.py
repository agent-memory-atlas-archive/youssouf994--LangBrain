"""
Più processi: stato condiviso, contratti dei compiti in background, blocchi tra processi, registro degli accessi senza rumore,
avvio con più worker e riavvio del supervisore. Qui i «processi» sono simulati cambiando proprietario del contratto.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import multiprocesso as mp
from app.core import registro_accessi, riavvio
from app.db.database import Database


class SharedStateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.mkdtemp(prefix="multiprocess_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = os.path.join(self.folder, "shared.db")
        patcher = mock.patch.dict(os.environ, {"LANGBRAIN_WORKERS": "2"})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_with_one_process_nothing_is_shared(self):
        with mock.patch.dict(os.environ, {"LANGBRAIN_WORKERS": "1"}):
            self.assertFalse(mp.condiviso())
            ran = []

            async def task():
                ran.append(1)

            await mp.compito_leader("x", task, percorso=self.path)  # direct, no database
            async with mp.blocco("x", percorso=self.path):
                pass
            self.assertEqual(ran, [1])
            self.assertFalse(os.path.exists(self.path))

    async def test_values_and_versions(self):
        self.assertTrue(mp.condiviso())
        self.assertEqual((await mp.versioni("a", "b", percorso=self.path)), {"a": 0, "b": 0})
        self.assertIsNone(await mp.leggi("a", percorso=self.path))
        self.assertEqual(await mp.scrivi("a", {"x": [1, 2]}, percorso=self.path), 1)
        self.assertEqual(await mp.scrivi("a", "due", percorso=self.path), 2)
        self.assertEqual(await mp.leggi("a", percorso=self.path), "due")
        self.assertEqual((await mp.versioni("a", "b", percorso=self.path)), {"a": 2, "b": 0})

    async def test_a_lease_is_taken_by_one_owner_and_expires(self):
        self.assertTrue(await mp._acquisisci("n", "A", 60, self.path))
        self.assertFalse(await mp._acquisisci("n", "B", 60, self.path))
        self.assertTrue(await mp._acquisisci("n", "A", 60, self.path))  # the owner renews
        await mp._rilascia("n", "A", self.path)
        self.assertTrue(await mp._acquisisci("n", "B", 60, self.path))
        self.assertTrue(await mp._acquisisci("m", "A", 0.05, self.path))
        await asyncio.sleep(0.1)
        self.assertTrue(await mp._acquisisci("m", "B", 60, self.path))  # expired: somebody else takes over

    async def test_the_lock_excludes_between_processes_and_times_out(self):
        order = []

        async def worker(owner, hold):
            with mock.patch.object(mp, "_PROCESSO", owner):
                async with mp.blocco("inv", attesa=5, percorso=self.path):
                    order.append(f"{owner}+")
                    await asyncio.sleep(hold)
                    order.append(f"{owner}-")

        await asyncio.gather(worker("A", 0.2), worker("B", 0.0))
        self.assertIn(order, (["A+", "A-", "B+", "B-"], ["B+", "B-", "A+", "A-"]))  # never interleaved
        await mp._acquisisci("blocco:busy", "other", 60, self.path)
        with self.assertRaises(TimeoutError):
            async with mp.blocco("busy", attesa=0.2, percorso=self.path):
                pass

    async def test_only_the_leader_runs_the_task_and_another_takes_over(self):
        runs = []

        async def task():
            runs.append(os.getpid())
            await asyncio.sleep(3600)

        leader = asyncio.create_task(mp.compito_leader("job", task, durata=0.3, percorso=self.path, proprietario="A"))
        await asyncio.sleep(0.3)
        follower = asyncio.create_task(mp.compito_leader("job", task, durata=0.3, percorso=self.path, proprietario="B"))
        await asyncio.sleep(0.5)
        self.assertEqual(len(runs), 1)  # the second one waits
        leader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await leader
        await asyncio.sleep(0.6)
        self.assertEqual(len(runs), 2)  # the lease was released: the follower is the leader now
        follower.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await follower

    async def test_a_task_that_returns_is_not_started_again_and_one_that_fails_is(self):
        calls = []

        async def returns():
            calls.append("r")

        async def fails():
            calls.append("f")
            raise RuntimeError("boom")

        for task, expected in ((returns, 1), (fails, 2)):
            calls.clear()
            leader = asyncio.create_task(mp.compito_leader(task.__name__, task, durata=0.3, percorso=self.path))
            await asyncio.sleep(0.45 if task is returns else 0.7)
            leader.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await leader
            self.assertEqual(len(calls) if task is returns else min(len(calls), 2), expected, task.__name__)


class SharedRuntimeStateTest(unittest.IsolatedAsyncioTestCase):
    """HITL configuration and simulated effectors seen by every process."""

    async def asyncSetUp(self):
        self.folder = tempfile.mkdtemp(prefix="multiprocess_state_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = os.path.join(self.folder, "state.db")
        await Database(db_path=self.path).init_db()
        for patcher in (mock.patch.dict(os.environ, {"LANGBRAIN_WORKERS": "2"}), mock.patch.object(mp, "DB_PATH", self.path)):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_the_hitl_configuration_changed_by_one_process_reaches_the_other(self):
        from app.graph.hitl_config import HitlConfigManager
        mine, other = HitlConfigManager(), object.__new__(HitlConfigManager)
        other.config = type(mine.config)()
        other._versione_vista = 0
        original = mine.config.model_copy()
        self.addCleanup(setattr, mine, "config", original)
        mine.update_config(hitl_nodes=["brain"], hitl_targets=["door"])
        await mine.salva_condivisa()
        self.assertEqual(other.config.hitl_nodes, [])
        await other.sincronizza()
        self.assertEqual((other.config.hitl_nodes, other.config.hitl_targets), (["brain"], ["door"]))

    async def test_a_simulated_effector_keeps_its_value_in_the_shared_state(self):
        from app.tools.effectors import SimulatedEffector
        a, b = SimulatedEffector("lamp_shared", "OFF"), SimulatedEffector("lamp_shared", "OFF")
        await a.set_tool_value("ON")
        self.assertEqual(await b.get_tool_value(), "ON")  # another process object: same value
        with mock.patch.dict(os.environ, {"LANGBRAIN_WORKERS": "1"}):
            self.assertEqual(await SimulatedEffector("lamp_alone", "OFF").get_tool_value(), "OFF")

    async def test_the_graph_is_recompiled_when_another_process_changed_the_agents(self):
        from app.api import main
        compiled = []

        async def fake_build(propaga=True):
            compiled.append(propaga)
            main._versione_grafo_vista = (await mp.versioni("grafo"))["grafo"]

        with mock.patch.object(main, "_recompile_system_graph", fake_build):
            await main.sincronizza_stato()
            self.assertEqual(compiled, [])  # nothing changed
            await mp.scrivi("grafo", 1)  # another process created an agent
            await main.sincronizza_stato()
            await main.sincronizza_stato()
            self.assertEqual(compiled, [False])  # recompiled once, without telling the others again


class AccessLogTest(unittest.TestCase):
    def _record(self, status):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:1", "GET", "/events/stats?window_hours=24", "1.1", status), None)

    def test_successful_requests_are_silent_and_problems_are_not(self):
        f = registro_accessi.SoloAccessiConProblemi()
        with mock.patch.dict(os.environ, {"LANGBRAIN_ACCESS_LOG": ""}):
            self.assertFalse(f.filter(self._record(200)))
            self.assertFalse(f.filter(self._record(304)))
            for status in (400, 401, 403, 404, 422, 500):
                self.assertTrue(f.filter(self._record(status)), status)
            self.assertTrue(f.filter(logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "free text", None, None)))
        with mock.patch.dict(os.environ, {"LANGBRAIN_ACCESS_LOG": "1"}):
            self.assertTrue(f.filter(self._record(200)))  # the full log on request

    def test_installing_twice_adds_one_filter(self):
        logger = logging.getLogger(registro_accessi.NOME_LOGGER)
        before = len([x for x in logger.filters if isinstance(x, registro_accessi.SoloAccessiConProblemi)])
        registro_accessi.installa_filtro_accessi()
        registro_accessi.installa_filtro_accessi()
        after = len([x for x in logger.filters if isinstance(x, registro_accessi.SoloAccessiConProblemi)])
        self.assertEqual(after, max(before, 1))


class ServeTest(unittest.TestCase):
    def test_how_many_processes(self):
        from app import serve
        with mock.patch.dict(os.environ, {"LANGBRAIN_WORKERS": ""}):
            self.assertEqual(serve.processi_richiesti(3), 3)
            self.assertEqual(serve.processi_richiesti("0"), 1)
            self.assertTrue(1 <= serve.processi_richiesti("auto") <= serve.MAX_PROCESSI_AUTOMATICI)
            self.assertTrue(1 <= serve.processi_richiesti(None) <= serve.MAX_PROCESSI_AUTOMATICI)
        with mock.patch.dict(os.environ, {"LANGBRAIN_WORKERS": "2"}):
            self.assertEqual(serve.processi_richiesti(None), 2)
        with self.assertRaises(SystemExit):
            serve.processi_richiesti("molti")

    def test_the_supervisor_stops_and_restarts_when_a_process_leaves_the_file(self):
        from app import serve
        folder = Path(tempfile.mkdtemp(prefix="serve_flag_"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        file, stopped, outcome = folder / "x.restart", [], {}
        import threading
        thread = threading.Thread(target=serve._sorveglia_richiesta_di_riavvio, args=(file, lambda: stopped.append(1), outcome), daemon=True)
        thread.start()
        file.write_text("restart")
        thread.join(5)
        self.assertEqual((stopped, outcome.get("riavvia"), file.exists()), ([1], True, False))

    def test_a_process_asks_the_supervisor_instead_of_stopping_itself(self):
        folder = Path(tempfile.mkdtemp(prefix="restart_flag_"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        file = folder / "r.restart"

        async def run():
            with mock.patch.object(riavvio, "RITARDO_SECONDI", 0), mock.patch.dict(os.environ, {riavvio.VARIABILE_FILE: str(file)}), \
                    mock.patch.object(riavvio.signal, "raise_signal") as stop, mock.patch.object(riavvio.subprocess, "Popen") as popen:
                self.assertEqual(riavvio.programma_riavvio(), "supervisor")
                await riavvio._compito
                return stop, popen

        stop, popen = asyncio.run(run())
        self.assertTrue(file.exists())
        stop.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
