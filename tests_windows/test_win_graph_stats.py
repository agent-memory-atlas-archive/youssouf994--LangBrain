"""
Statistiche per i grafici del core: traccia delle esecuzioni dei nodi (`GET /graph/stats`, `/graph/runs`) e segnali
nel tempo (`GET /signals`, `/signals/stats`), con parametri generici (finestra, intervallo, recettore, proprietario).
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient
from langgraph.errors import GraphInterrupt

from app.core.configurazione import costruisci_configurazione, imposta_configurazione
from app.db.database import Database
from app.graph.builder import traccia_nodo
from app.tools import nervous_system
from app.tools.nervous_system import NervousSystem
from app.tools.statistiche_grafo import FINE, INIZIO, riassumi_esecuzioni, riassumi_segnali

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def _run(run_id, node, minutes_ago, outcome="ok", ms=10.0, detail=""):
    return {"run_id": run_id, "thread_id": "t", "node": node, "duration_ms": ms, "outcome": outcome, "detail": detail,
            "started_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")}


def _signal(receptor, minutes_ago, value, owner="agent_x", unit="C"):
    return {"receptor_id": receptor, "agent_owner": owner, "value": value, "unit": unit,
            "timestamp": (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")}


class SummaryFunctionsTest(unittest.TestCase):
    def test_nodes_edges_waits_and_runs_in_progress(self):
        rows = [
            _run("a", "brain", 50, ms=100), _run("a", "agent_x", 49, ms=20), _run("a", "brain", 48, ms=40),
            _run("b", "brain", 180, "waiting"), _run("b", "brain", 60, ms=30),     # waited two hours
            _run("c", "brain", 5, "waiting"),                                       # still waiting
        ]
        out = riassumi_esecuzioni(rows, 24, NOW)
        self.assertEqual((out["runs"], out["waiting"]), (3, 1))
        brain = next(n for n in out["nodes"] if n["node"] == "brain")
        self.assertEqual((brain["runs"], brain["outcomes"]), (5, {"ok": 3, "waiting": 2}))
        self.assertEqual(brain["median_ms"], 40.0)  # the waits are not counted as time
        edges = {(e["from"], e["to"]): e["count"] for e in out["edges"]}
        self.assertEqual(edges[(INIZIO, "brain")], 3)
        self.assertEqual(edges[("brain", "agent_x")], 1)
        self.assertEqual(edges[("agent_x", "brain")], 1)
        self.assertEqual(edges[("brain", FINE)], 2)  # a and b ended; c did not
        self.assertEqual(out["wait_hours_median"], 2.0)

    def test_no_rows_is_an_empty_summary(self):
        out = riassumi_esecuzioni([], 24, NOW)
        self.assertEqual((out["runs"], out["nodes"], out["edges"], out["wait_hours_median"]), (0, [], [], None))

    def test_signal_series_keep_empty_intervals_and_any_receptor(self):
        rows = [_signal("temp", 5, 20.0), _signal("temp", 20, 22.0), _signal("temp", 21, 24.0), _signal("hr", 3, 70, "organ_heart", "bpm"),
                {**_signal("temp", 1, 0), "value": "ON"}, _signal("temp", 500, 99.0)]  # text and out of the window: ignored
        out = riassumi_segnali(rows, 1, 10, NOW)
        self.assertEqual(out["bucket_minutes"], 10.0)
        self.assertEqual(out["signals"], 4)
        temp = next(r for r in out["receptors"] if r["receptor_id"] == "temp")
        self.assertEqual((temp["count"], temp["min"], temp["max"], temp["mean"]), (3, 20.0, 24.0, 22.0))
        self.assertEqual(temp["last"]["value"], 20.0)
        self.assertEqual(len(temp["series"]), 6)
        self.assertEqual([b["count"] for b in temp["series"]], [0, 0, 0, 1, 1, 1])
        self.assertIsNone(temp["series"][0]["mean"])  # empty: no invented value
        self.assertEqual(next(r for r in out["receptors"] if r["receptor_id"] == "hr")["owners"], ["organ_heart"])

    def test_default_interval_splits_the_window_in_48(self):
        self.assertEqual(riassumi_segnali([], 24, None, NOW)["bucket_minutes"], 30.0)


class TraceAndApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.mkdtemp(prefix="graph_stats_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = os.path.join(self.folder, "stats.db")
        await Database(db_path=self.path).init_db()
        self.real = NervousSystem
        self.system = NervousSystem(db_path=self.path)
        patcher = mock.patch.object(nervous_system, "NervousSystem", lambda *a, **k: self.real(db_path=self.path))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_the_trace_records_ok_escalated_waiting_and_error_without_changing_the_result(self):
        async def ok(state):
            return {"next_agent": "END"}

        async def escalating(state):
            return {"pending_escalations": [{"resolved": False}], "next_agent": "brain"}

        async def pausing(state):
            raise GraphInterrupt()

        async def broken(state):
            raise RuntimeError("boom")

        config = {"configurable": {"thread_id": "th"}}
        state = {"config": {"run_id": "run1"}}
        self.assertEqual(await traccia_nodo("n1", ok)(state, config), {"next_agent": "END"})
        await traccia_nodo("n2", escalating)(state, config)
        with self.assertRaises(GraphInterrupt):
            await traccia_nodo("n3", pausing)(state, config)
        with self.assertRaises(RuntimeError):
            await traccia_nodo("n4", broken)({"config": {}}, config)  # no run_id: the thread is the run
        runs = await self.system.get_node_runs(1)
        self.assertEqual([(r["run_id"], r["node"], r["outcome"], r["detail"]) for r in runs],
                         [("run1", "n1", "ok", "END"), ("run1", "n2", "escalated", "brain"), ("run1", "n3", "waiting", ""),
                          ("th", "n4", "error", "RuntimeError")])
        self.assertEqual(len(await self.system.get_node_runs(1, run_id="run1")), 3)

    async def test_a_failing_trace_never_stops_the_node(self):
        async def ok(state):
            return {"x": 1}

        with mock.patch.object(nervous_system, "NervousSystem", side_effect=RuntimeError("no db")):
            self.assertEqual(await traccia_nodo("n", ok)({}, {}), {"x": 1})

    async def test_the_endpoints_answer_with_generic_parameters_and_viewer_role(self):
        from app.api import main

        await self.system.record_signals([{"receptor_id": "temp_living", "agent_owner": "agent_climate", "value": 21.5, "unit": "C"},
                                          {"receptor_id": "power_main", "agent_owner": "agent_energy", "value": 1200, "unit": "W"}])
        await self.system.record_node_run("r1", "brain", datetime.now(timezone.utc).isoformat(timespec="seconds"), 12.0, "ok", "END", "th")
        env = {"API_KEY_PRIMARIO": "adm", "API_KEY_TIROCINANTE": "viewer", "API_KEY_MEDICO_DI_GUARDIA": "", "API_KEY": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(main, "EventLog", lambda *a, **k: self.real(db_path=self.path)):
            client = TestClient(main.app)
            viewer = {"X-API-Key": "viewer"}
            self.assertEqual(client.get("/signals/stats").status_code, 401)
            stats = client.get("/signals/stats", params={"window_hours": 2, "bucket_minutes": 30}, headers=viewer).json()
            self.assertEqual({r["receptor_id"] for r in stats["receptors"]}, {"temp_living", "power_main"})
            self.assertEqual(len(stats["receptors"][0]["series"]), 4)
            owner = client.get("/signals/stats", params={"agent_owner": "agent_energy"}, headers=viewer).json()
            self.assertEqual([r["receptor_id"] for r in owner["receptors"]], ["power_main"])
            raw = client.get("/signals", params={"receptor_id": "temp_living"}, headers=viewer).json()
            self.assertEqual((raw["count"], raw["signals"][0]["value"]), (1, 21.5))
            graph = client.get("/graph/stats", headers=viewer).json()
            self.assertEqual((graph["runs"], graph["nodes"][0]["node"]), (1, "brain"))
            self.assertIn("topology", graph)
            runs = client.get("/graph/runs", headers=viewer).json()
            self.assertEqual(runs["runs"][0]["steps"][0]["node"], "brain")
            self.assertEqual(client.get("/signals/stats", params={"window_hours": 0}, headers=viewer).status_code, 422)

    async def test_the_signal_window_comes_from_the_configuration(self):
        imposta_configurazione(costruisci_configurazione({"signals": {"window_hours": 6}}))
        self.addCleanup(imposta_configurazione, None)
        from app.core.configurazione import get_configurazione
        self.assertEqual(get_configurazione().finestra_segnali_ore, 6.0)
        from app.core.configurazione import ErroreConfigurazione
        for bad in (0, -1, "x", True, 99999):
            with self.assertRaises(ErroreConfigurazione):
                costruisci_configurazione({"segnali": {"finestra_ore": bad}})


if __name__ == "__main__":
    unittest.main()
