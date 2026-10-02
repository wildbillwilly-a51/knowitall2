"""Computers connected to a KnowItAll2 server: connecting, syncing, working offline, and taking turns."""

import json
import os
import socket
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository

from knowitall2 import connected, sync
from knowitall2.memory import Memory
from knowitall2.paths import database_path
from knowitall2.server import accounts
from knowitall2.server.web import KnowItAll2Server
from knowitall2.store import Store

NOW = "2026-10-01T12:00:00Z"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TwoComputers(unittest.TestCase):
    """A real server, and two computers, each with its own data home."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plain = self.root / "plain"  # not a repository: memories go to the global scope
        self.plain.mkdir()
        self.port = free_port()
        self.address = f"http://127.0.0.1:{self.port}"
        self.start_server()

    def start_server(self) -> None:
        self.server = KnowItAll2Server(self.root / "server", port=self.port)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.stop()
        self.thread.join(timeout=5)

    def code(self, name: str) -> str:
        store = self.server.open()
        try:
            return accounts.new_join_code(store, name, now=self.server.clock())["code"]
        finally:
            store.close()

    @contextmanager
    def on(self, computer: str):
        """Act as one computer: its data home, and its database open."""

        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / computer)}):
            store = Store.open(database_path())
            try:
                yield store
            finally:
                store.close()

    def remember(self, store: Store, text: str, *, now: str = NOW) -> str:
        return Memory(store, agent="codex", clock=Clock(now)).remember(text, project_path=self.plain).record.id

    def recall(self, store: Store, query: str) -> str:
        return Memory(store, agent="codex").recall(query, project_path=self.plain)

    def connect(self, computer: str, agent: str = "codex", *, upload: bool = True) -> dict:
        with self.on(computer) as store:
            return connected.connect(store, self.address, agent, self.code(f"{computer} - {agent}"), upload=upload)

    def sync(self, computer: str) -> connected.SyncReport:
        with self.on(computer) as store:
            return connected.sync_now(store)


class ConnectingTests(TwoComputers):
    def test_the_first_computer_starts_the_server_and_the_second_gets_its_memory(self) -> None:
        with self.on("a") as store:
            self.remember(store, "The build server is build-1.")
        first = self.connect("a")
        self.assertEqual((first["first"], first["uploaded"]), (True, 1))
        second = self.connect("b", upload=False)
        self.assertEqual(second["received"], 1)
        with self.on("b") as store:
            self.assertIn("build-1", self.recall(store, "build server"))

    def test_a_second_agent_on_a_computer_gets_its_own_key(self) -> None:
        self.connect("a", "codex")
        report = self.connect("a", "claude-code")
        self.assertFalse(report["first"])
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "a")}):
            found = connected.load()
            self.assertEqual(sorted(found.keys), ["claude-code", "codex"])
            self.assertNotEqual(found.keys["codex"], found.keys["claude-code"])
        store = self.server.open()
        try:
            self.assertEqual(len(accounts.connections(store)), 2)
        finally:
            store.close()

    def test_a_computer_cannot_join_a_second_server_without_disconnecting(self) -> None:
        self.connect("a")
        code = self.code("a - other")
        with self.on("a") as store:
            with self.assertRaises(connected.ConnectError):
                connected.connect(store, "http://127.0.0.1:1", "claude-code", code, upload=False)
        store = self.server.open()
        try:
            self.assertEqual(len(accounts.waiting_codes(store, now=self.server.clock())), 1)  # the code was not spent
        finally:
            store.close()

    def test_a_bad_address_or_code_is_a_plain_error(self) -> None:
        with self.on("a") as store:
            with self.assertRaises(connected.ConnectError):
                connected.connect(store, "kia.example.lan", "codex", "AAAA", upload=False)
            with self.assertRaises(connected.ConnectError) as raised:
                connected.connect(store, self.address, "codex", "AAAA-BBBB-CCCC-DDDD", upload=False)
        self.assertIn("join code", str(raised.exception))
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "a")}):
            self.assertFalse(connected.is_connected())

    def test_disconnecting_keeps_the_memory_and_stops_noting_changes(self) -> None:
        with self.on("a") as store:
            self.remember(store, "The build server is build-1.")
        self.connect("a")
        with self.on("a") as store:
            connected.disconnect(store)
            self.remember(store, "The test server is test-1.")
            self.assertFalse(sync.has_pending(store))
            self.assertIn("build-1", self.recall(store, "build server"))
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "a")}):
            self.assertIsNone(connected.load())


class ReplacingTests(TwoComputers):
    def test_after_replacing_a_memory_saved_in_a_project_this_computer_had_still_goes(self) -> None:
        self.connect("a")
        project = make_repository(self.root / "web", "https://git.example.com/team/web.git")
        with self.on("b") as store:
            Memory(store, agent="codex", clock=Clock()).remember("The web app builds with make.", scope="project", project_path=project)
        with self.on("b") as store:
            report = connected.connect(store, self.address, "codex", self.code("b - codex"), upload=False, replace=True)
        with self.on("b") as store:
            record_id = Memory(store, agent="codex", clock=Clock()).remember(
                "The web app deploys with make deploy.", scope="project", project_path=project).record.id
            found = connected.sync_now(store)
            self.assertEqual((found.problem, found.refused), (None, 0))
            self.assertFalse(sync.has_pending(store))
        self.assertTrue(report["first"])
        server = self.server.open()
        try:
            row = server.connection.execute("SELECT scope, project_id FROM records WHERE id = ?", (record_id,)).fetchone()
            self.assertEqual(row[0], "project")
            self.assertIsNotNone(server.connection.execute("SELECT 1 FROM projects WHERE id = ?", (row[1],)).fetchone())
        finally:
            server.close()


class SyncingTests(TwoComputers):
    def setUp(self) -> None:
        super().setUp()
        self.connect("a")
        self.connect("b", upload=False)

    def test_a_memory_saved_on_one_computer_reaches_the_other(self) -> None:
        with self.on("a") as store:
            record_id = self.remember(store, "The build server is build-1.")
            self.assertEqual(store.get(record_id).source_computer is not None, True)
        self.assertEqual(self.sync("a").sent, 1)
        self.assertEqual(self.sync("b").received, 1)
        with self.on("b") as store:
            self.assertIn(record_id, self.recall(store, "build server"))

    def test_changes_wait_while_the_server_is_down(self) -> None:
        self.stop_server()
        with self.on("a") as store:
            self.remember(store, "The build server is build-1.")
            report = connected.sync_now(store)
            self.assertIn("cannot reach", report.problem)
            self.assertTrue(sync.has_pending(store))
            self.assertIn("build-1", self.recall(store, "build server"))
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "a")}):
            status = connected.read_status()
        self.assertTrue(status["unsent"])
        self.assertIn("cannot reach", status["problem"])
        self.start_server()
        self.assertEqual(self.sync("a").sent, 1)
        self.assertEqual(self.sync("b").received, 1)

    def test_fetching_leaves_this_computers_unsent_change_alone(self) -> None:
        with self.on("a") as store:
            record_id = self.remember(store, "The build server is build-1.")
        self.sync("a")
        self.sync("b")
        with self.on("b") as store:
            Memory(store, agent="codex", clock=Clock("2026-10-01T13:00:00Z")).forget(record_id, reason="moved")
            connected.pull(store, connected.client_for(connected.load(), None))
            self.assertEqual(store.get(record_id).status, "retired")
        self.sync("b")
        self.sync("a")
        with self.on("a") as store:
            self.assertEqual(store.get(record_id).status, "retired")

    def test_when_two_computers_change_one_memory_the_newer_change_wins_on_both(self) -> None:
        with self.on("a") as store:
            record_id = self.remember(store, "The build server is build-1.")
        self.sync("a")
        self.sync("b")
        with self.on("a") as store:
            Memory(store, agent="codex", clock=Clock("2026-10-01T14:00:00Z")).forget(record_id, reason="newer")
        with self.on("b") as store:
            Memory(store, agent="codex", clock=Clock("2026-10-01T13:00:00Z")).forget(record_id, reason="older")
        self.sync("a")
        self.sync("b")
        for computer in ("a", "b"):
            with self.on(computer) as store:
                self.assertEqual(store.connection.execute(
                    "SELECT retired_reason FROM records WHERE id = ?", (record_id,)).fetchone()[0], "newer", computer)
                self.assertFalse(sync.has_pending(store))

    def test_the_same_memory_learned_on_both_computers_stays_one(self) -> None:
        with self.on("a") as store:
            first = self.remember(store, "The build server is build-1.")
        with self.on("b") as store:
            second = self.remember(store, "The build server is build-1.")
        self.sync("a")
        self.sync("b")
        with self.on("b") as store:
            row = store.get(second)
            self.assertEqual((row.status, row.superseded_by), ("superseded", first))
            self.assertEqual(self.recall(store, "build server").count("build-1"), 1)

    def test_use_on_a_computer_counts_on_the_server(self) -> None:
        with self.on("a") as store:
            record_id = self.remember(store, "The build server is build-1.")
        self.sync("a")
        with self.on("b") as store:
            self.sync("b")
            self.recall(store, "build server")
        self.sync("b")
        store = self.server.open()
        try:
            count = store.connection.execute("SELECT recall_count FROM records WHERE id = ?", (record_id,)).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(count, 1)

    def test_a_removed_agent_is_told_so(self) -> None:
        store = self.server.open()
        try:
            for connection in accounts.connections(store):
                if connection["computer"] is not None and connection["name"].startswith("a "):
                    accounts.remove(store, connection["id"], now=self.server.clock())
        finally:
            store.close()
        report = self.sync("a")
        self.assertEqual(report.status, 401)

    def test_only_one_computer_at_a_time_tidies_up(self) -> None:
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "a")}):
            with connected.maintenance_turn() as mine:
                self.assertTrue(mine)
                with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "b")}):
                    with connected.maintenance_turn() as theirs:
                        self.assertFalse(theirs)
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "b")}):
            with connected.maintenance_turn() as theirs:
                self.assertTrue(theirs)
        self.stop_server()
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "b")}):
            with connected.maintenance_turn() as offline:
                self.assertFalse(offline)
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "local")}):
            with connected.maintenance_turn() as alone:
                self.assertTrue(alone)

    def test_a_sync_while_another_runs_asks_it_to_go_round_again(self) -> None:
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "a")}):
            with connected.SyncLock():
                store = Store.open(database_path())
                try:
                    self.assertTrue(connected.sync_now(store).skipped)
                finally:
                    store.close()
                self.assertTrue((connected.folder() / "sync.again").exists())


class NudgeTests(TwoComputers):
    def test_nothing_starts_on_a_computer_that_is_not_connected(self) -> None:
        launcher = mock.Mock()
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "a")}):
            self.assertFalse(connected.nudge("codex", launcher=launcher))
        launcher.assert_not_called()

    def test_a_sync_starts_when_due_and_not_again_right_away(self) -> None:
        self.connect("a")
        launcher = mock.Mock()
        with self.on("a") as store:
            self.assertTrue(connected.nudge("codex", store=store, launcher=launcher, now=10_000_000_000))
            command = launcher.call_args[0][0]
            self.assertEqual(command[-4:], ["sync", "--quiet", "--agent", "codex"])
            self.assertFalse(connected.nudge("codex", store=store, launcher=launcher))
            self.remember(store, "The build server is build-1.")
            self.assertTrue(connected.nudge("codex", store=store, launcher=launcher, pull=False))
        self.assertEqual(launcher.call_count, 2)


class NewerServerTests(unittest.TestCase):
    def test_a_kind_of_data_this_version_does_not_know_is_skipped(self) -> None:
        store = Store.in_memory()
        try:
            changes = [{"table": "gadgets", "key": "g-1", "op": "upsert", "row": {"id": "g-1"}, "seq": 3},
                       {"table": "projects", "key": "prj-1", "op": "upsert", "seq": 4,
                        "row": {"id": "prj-1", "name": "web", "remote": None, "created_at": NOW, "last_seen_at": NOW}}]
            self.assertEqual(sync.apply_pulled(store, changes, cursor=4), 1)
            self.assertEqual(store.get_meta(sync.CURSOR_KEY), "4")
        finally:
            store.close()

    def test_columns_this_version_does_not_know_are_left_out(self) -> None:
        store = Store.in_memory()
        try:
            row = {"id": "prj-1", "name": "web", "remote": None, "created_at": NOW, "last_seen_at": NOW,
                   "colour": "blue"}
            sync.apply_pulled(store, [{"table": "projects", "key": "prj-1", "op": "upsert", "row": row, "seq": 4}])
            self.assertEqual(store.connection.execute("SELECT name FROM projects").fetchone()[0], "web")
            self.assertEqual(store.connection.execute("SELECT seq FROM sync_seen").fetchone()[0], 4)
        finally:
            store.close()

    def test_changes_to_a_newer_schema_are_sent_without_what_an_older_server_lacks(self) -> None:
        operations = [{"op_id": "o-1", "table": "records", "key": "k-1", "op": "upsert",
                       "row": {"id": "k-1", "source_computer": "desk", "text": "x"}},
                      {"op_id": "o-2", "table": "gadgets", "key": "g-1", "op": "upsert", "row": {}}]
        fitted = connected._fit(operations, {"records": ["id", "text"]})
        self.assertEqual(fitted, [{"op_id": "o-1", "table": "records", "key": "k-1", "op": "upsert",
                                   "row": {"id": "k-1", "text": "x"}}])


if __name__ == "__main__":
    unittest.main()
