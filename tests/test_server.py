"""The KnowItAll2 server: change tracking, connections, the exchange, backups, and the web service."""

import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _support import Clock

from knowitall2 import cli, sync
from knowitall2.memory import Memory
from knowitall2.remote import RemoteClient, RemoteError
from knowitall2.server import accounts, backups, exchange, open_store
from knowitall2.server.web import JOIN_FAILURES_PER_ADDRESS, JOIN_WINDOW_SECONDS, KnowItAll2Server, Throttle
from knowitall2.store import SYNCED_TABLES, Store

NOW = "2026-10-01T12:00:00Z"
# Split so repository secret scanners do not flag this file.
FAKE_TOKEN = "gl" + "pat-" + "a1B2c3D4e5F6g7H8i9J0k1L2"


def computer(folder: Path, name: str) -> Store:
    """A computer's own database, tracking its changes as a connected computer does."""

    store = Store.open(folder / f"{name}.db")
    sync.set_tracking(store, True)
    return store


def changed(store: Store) -> list[tuple[str, str, str]]:
    return [tuple(row) for row in store.connection.execute("SELECT tbl, key, op FROM changes ORDER BY seq")]


class TemporaryFolder(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)  # first in, so it runs after every other cleanup
        self.root = Path(self.temporary.name)
        self.plain = self.root / "plain"  # not a repository: memories go to the global scope
        self.plain.mkdir()

    def computer(self, name: str) -> Store:
        store = computer(self.root, name)
        self.addCleanup(store.close)
        return store

    def remember(self, store: Store, text: str, **options) -> str:
        memory = Memory(store, agent="codex", clock=Clock(options.pop("now", NOW)))
        return memory.remember(text, project_path=self.plain, **options).record.id


class ChangeTrackingTests(TemporaryFolder):
    def test_a_local_database_notes_nothing(self) -> None:
        store = Store.open(self.root / "local.db")
        self.addCleanup(store.close)
        self.remember(store, "The build server is build-1.")
        self.assertEqual(changed(store), [])

    def test_saves_are_noted_but_use_counts_are_not(self) -> None:
        store = self.computer("a")
        record_id = self.remember(store, "The build server is build-1.")
        self.assertEqual(changed(store), [("records", record_id, "upsert")])
        Memory(store, agent="codex").recall("build server", project_path=self.plain)
        self.assertEqual(store.connection.execute("SELECT recall_count FROM records").fetchone()[0], 1)
        self.assertEqual(len(changed(store)), 1)
        Memory(store, agent="codex").forget(record_id, reason="moved")
        self.assertEqual(len(changed(store)), 2)

    def test_changes_from_the_server_are_not_noted_as_this_computers(self) -> None:
        source = self.computer("a")
        self.remember(source, "The build server is build-1.")
        operations = sync.pending_operations(source)
        target = self.computer("b")
        rows = [{"table": item["table"], "key": item["key"], "op": item["op"], "row": item["row"]}
                for item in operations]
        sync.apply_pulled(target, rows, cursor=7)
        self.assertEqual(changed(target), [])
        self.assertEqual(target.get_meta(sync.CURSOR_KEY), "7")
        self.assertIn("build-1", Memory(target).recall("build server", project_path=self.plain))

    def test_pending_operations_send_each_row_once_and_forget_what_was_accepted(self) -> None:
        store = self.computer("a")
        record_id = self.remember(store, "The build server is build-1.")
        Memory(store, agent="codex").forget(record_id)
        operations = sync.pending_operations(store)
        self.assertEqual([(item["table"], item["key"]) for item in operations], [("records", record_id)])
        self.assertEqual(operations[0]["row"]["status"], "retired")
        sync.acknowledge(store, operations)
        self.assertEqual(changed(store), [])


class AccountTests(TemporaryFolder):
    def setUp(self) -> None:
        super().setUp()
        self.store = open_store(self.root / "server.db")

    def tearDown(self) -> None:
        self.store.close()
        super().tearDown()

    def test_a_join_code_works_once_and_gives_a_working_key(self) -> None:
        code = accounts.new_join_code(self.store, "Workstation - Codex", now=NOW)
        self.assertRegex(code["code"], r"^[A-Z2-9]{4}(-[A-Z2-9]{4}){3}$")
        key, connection = accounts.redeem(self.store, code["code"].lower().replace("-", " "), agent="codex",
                                          computer="desk", version="0.8.0", now=NOW)
        self.assertEqual(connection["name"], "Workstation - Codex")
        self.assertEqual(accounts.authenticate(self.store, key, now=NOW)["id"], connection["id"])
        with self.assertRaises(accounts.AccountError):
            accounts.redeem(self.store, code["code"], agent="codex", computer="desk", version="0.8.0", now=NOW)

    def test_expired_and_cancelled_codes_do_not_work(self) -> None:
        old = accounts.new_join_code(self.store, "Laptop - Claude Code", now=NOW)
        with self.assertRaises(accounts.AccountError):
            accounts.redeem(self.store, old["code"], agent=None, computer=None, version=None,
                            now="2026-10-01T12:15:00Z")
        cancelled = accounts.new_join_code(self.store, "Laptop - Codex", now=NOW)
        self.assertEqual([item["id"] for item in accounts.waiting_codes(self.store, now=NOW)],
                         [old["id"], cancelled["id"]])
        self.assertTrue(accounts.cancel_code(self.store, cancelled["id"], now=NOW))
        with self.assertRaises(accounts.AccountError):
            accounts.redeem(self.store, cancelled["code"], agent=None, computer=None, version=None, now=NOW)

    def test_a_removed_agents_key_stops_working(self) -> None:
        code = accounts.new_join_code(self.store, "Laptop - Codex", now=NOW)
        key, connection = accounts.redeem(self.store, code["code"], agent="codex", computer=None, version=None,
                                          now=NOW)
        self.assertTrue(accounts.remove(self.store, connection["id"], now=NOW))
        self.assertIsNone(accounts.authenticate(self.store, key, now=NOW))
        self.assertEqual(accounts.connections(self.store), [])

    def test_the_server_keeps_neither_codes_nor_keys(self) -> None:
        code = accounts.new_join_code(self.store, "Laptop - Codex", now=NOW)
        key, _ = accounts.redeem(self.store, code["code"], agent="codex", computer=None, version=None, now=NOW)
        self.store.close()
        raw = (self.root / "server.db").read_bytes()
        for path in self.root.glob("server.db-*"):
            raw += path.read_bytes()
        self.store = open_store(self.root / "server.db")
        self.assertNotIn(key.encode(), raw)
        self.assertNotIn(code["code"].replace("-", "").encode(), raw)

    def test_checking_in_is_written_at_most_once_a_minute(self) -> None:
        code = accounts.new_join_code(self.store, "Laptop - Codex", now=NOW)
        key, _ = accounts.redeem(self.store, code["code"], agent="codex", computer="desk", version="0.8.0", now=NOW)
        self.assertEqual(accounts.authenticate(self.store, key, now="2026-10-01T12:00:30Z")["last_seen_at"], NOW)
        later = accounts.authenticate(self.store, key, now="2026-10-01T12:01:00Z", version="0.8.1")
        self.assertEqual((later["last_seen_at"], later["version"]), ("2026-10-01T12:01:00Z", "0.8.1"))


class ExchangeTests(TemporaryFolder):
    def setUp(self) -> None:
        super().setUp()
        self.server = open_store(self.root / "server.db")
        self.a = computer(self.root, "a")
        self.b = computer(self.root, "b")

    def tearDown(self) -> None:
        for store in (self.server, self.a, self.b):
            store.close()
        super().tearDown()

    def send(self, store: Store, connection: str, operations=None, now: str = NOW) -> list[dict]:
        operations = sync.pending_operations(store) if operations is None else operations
        answers = exchange.apply_operations(self.server, connection, operations, now=now)
        sync.acknowledge(store, [item for item, answer in zip(operations, answers) if answer["result"] != "rejected"])
        return answers

    def test_accepted_changes_reach_another_computer(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.assertEqual([answer["result"] for answer in self.send(self.a, "c-a")], ["applied"])
        found = sync.changes_since(self.server, 0)
        self.assertEqual([(item["table"], item["key"]) for item in found["changes"]], [("records", record_id)])
        self.assertEqual((found["next"], found["more"]), (found["latest"], False))
        sync.apply_pulled(self.b, found["changes"], cursor=found["next"])
        self.assertIn("build-1", Memory(self.b).recall("build server", project_path=self.plain))
        self.assertEqual(sync.changes_since(self.server, found["next"])["changes"], [])

    def test_changes_come_in_pages_with_each_row_once(self) -> None:
        for number in range(5):
            self.remember(self.a, f"Service number {number} listens on port 80{number}.")
        self.send(self.a, "c-a")
        first = sync.changes_since(self.server, 0, limit=3)
        self.assertEqual((len(first["changes"]), first["more"]), (3, True))
        rest = sync.changes_since(self.server, first["next"], limit=3)
        self.assertEqual((len(rest["changes"]), rest["more"]), (2, False))

    def test_the_same_memory_from_two_computers_stays_one_memory(self) -> None:
        first = self.remember(self.a, "The build server is build-1.")
        second = self.remember(self.b, "The build server is build-1.")
        self.send(self.a, "c-a")
        answers = self.send(self.b, "c-b")
        self.assertEqual((answers[0]["result"], answers[0]["kept"]), ("duplicate", first))
        self.assertEqual(answers[0]["row"]["status"], "superseded")
        row = sync.read_row(self.server.connection, sync.table_for("records"), second)
        self.assertEqual((row["status"], row["superseded_by"]), ("superseded", first))

    def test_a_memory_holding_a_secret_is_refused(self) -> None:
        self.remember(self.a, "The build server is build-1.")
        operation = sync.pending_operations(self.a)[0]
        operation["row"]["text"] = f"The deploy token is {FAKE_TOKEN}"
        answer = self.send(self.a, "c-a", [operation])[0]
        self.assertEqual(answer["result"], "rejected")
        self.assertIn("secret", answer["reason"])

    def test_a_memory_kept_from_before_the_screening_caught_it_can_still_be_retired(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        # Saved before the screening knew this shape of secret, so both copies hold it.
        for store in (self.a, self.server):
            store.connection.execute("UPDATE records SET text = ? WHERE id = ?",
                                     (f"The deploy token is {FAKE_TOKEN}", record_id))
        Memory(self.a, agent="codex", clock=Clock("2026-10-01T13:00:00Z")).forget(record_id, reason="holds a secret")
        self.assertEqual([answer["result"] for answer in self.send(self.a, "c-a")], ["applied"])
        row = sync.read_row(self.server.connection, sync.table_for("records"), record_id)
        self.assertEqual((row["status"], row["retired_reason"]), ("retired", "holds a secret"))

    def test_a_known_memory_cannot_be_changed_to_hold_a_secret(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        Memory(self.a, agent="codex", clock=Clock("2026-10-01T13:00:00Z")).forget(record_id, reason="moved")
        [operation] = sync.pending_operations(self.a)
        operation["row"]["text"] = f"The deploy token is {FAKE_TOKEN}"
        answer = self.send(self.a, "c-a", [operation])[0]
        self.assertEqual(answer["result"], "rejected")
        self.assertEqual(answer["row"]["text"], "The build server is build-1.")

    def test_the_newer_version_wins_when_two_agents_change_one_memory(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        base = sync.changes_since(self.server, 0)
        sync.apply_pulled(self.b, base["changes"], cursor=base["next"])
        Memory(self.a, agent="codex", clock=Clock("2026-10-01T13:00:00Z")).forget(record_id, reason="gone")
        Memory(self.b, agent="codex", clock=Clock("2026-10-01T12:30:00Z")).forget(record_id, reason="older")
        self.send(self.a, "c-a")
        operation = sync.pending_operations(self.b)[0]
        operation["base"] = base["next"]
        self.assertEqual(self.send(self.b, "c-b", [operation])[0]["result"], "kept_newer")
        row = sync.read_row(self.server.connection, sync.table_for("records"), record_id)
        self.assertEqual(row["retired_reason"], "gone")
        outcome = self.server.connection.execute("SELECT outcome FROM server_conflicts").fetchall()
        self.assertEqual([tuple(item) for item in outcome], [("existing",)])

    def test_a_stamp_from_a_clock_far_ahead_does_not_win_every_later_disagreement(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        base = sync.changes_since(self.server, 0)
        sync.apply_pulled(self.b, base["changes"], cursor=base["next"])
        Memory(self.a, agent="codex", clock=Clock("9999-12-31T23:59:59Z")).forget(record_id, reason="far ahead")
        self.assertEqual(self.send(self.a, "c-a")[0]["result"], "applied")
        later = "2026-10-01T13:00:00Z"
        Memory(self.b, agent="codex", clock=Clock(later)).forget(record_id, reason="a real later edit")
        operation = sync.pending_operations(self.b)[0]
        operation["base"] = base["next"]
        self.assertEqual(self.send(self.b, "c-b", [operation], now=later)[0]["result"], "applied")
        row = sync.read_row(self.server.connection, sync.table_for("records"), record_id)
        self.assertEqual(row["retired_reason"], "a real later edit")
        outcome = self.server.connection.execute("SELECT outcome FROM server_conflicts").fetchall()
        self.assertEqual([tuple(item) for item in outcome], [("incoming",)])

    def test_a_row_the_server_already_has_is_no_disagreement_and_no_change(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        latest = sync.latest_change(self.server)
        found = sync.changes_since(self.server, 0)
        sync.apply_pulled(self.b, found["changes"], cursor=found["next"])
        with self.b.transaction():  # as when a computer sends its whole memory again on reconnecting
            self.b.connection.execute("DELETE FROM sync_seen")
            sync.note_all(self.b, "records")
        [operation] = sync.pending_operations(self.b)
        self.assertNotIn("base", operation)
        answer = self.send(self.b, "c-b", [operation])[0]
        self.assertEqual((answer["result"], answer["seq"]), ("applied", latest))
        self.assertEqual(self.server.connection.execute("SELECT COUNT(*) FROM server_conflicts").fetchone()[0], 0)
        self.assertEqual(sync.latest_change(self.server), latest)
        self.assertEqual(sync.read_row(self.server.connection, sync.table_for("records"), record_id)["status"],
                         "active")

    def test_an_applied_change_says_its_change_number_on_the_server(self) -> None:
        self.remember(self.a, "The build server is build-1.")
        self.remember(self.a, "The test server is test-1.")
        answers = self.send(self.a, "c-a")
        self.assertEqual([(answer["result"], answer["seq"]) for answer in answers], [("applied", 1), ("applied", 2)])

    def test_an_agents_own_earlier_change_is_no_disagreement(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        Memory(self.a, agent="codex", clock=Clock("2026-10-01T11:00:00Z")).forget(record_id, reason="moved")
        self.assertEqual(self.send(self.a, "c-a")[0]["result"], "applied")
        self.assertEqual(self.server.connection.execute("SELECT COUNT(*) FROM server_conflicts").fetchone()[0], 0)

    def test_an_operation_sent_twice_gets_its_first_answer(self) -> None:
        self.remember(self.a, "The build server is build-1.")
        operations = sync.pending_operations(self.a)
        first = exchange.apply_operations(self.server, "c-a", operations, now=NOW)
        again = exchange.apply_operations(self.server, "c-a", operations, now=NOW)
        self.assertEqual(first, again)
        self.assertEqual(sync.latest_change(self.server), 1)

    def test_a_memory_whose_project_has_not_arrived_waits_for_it(self) -> None:
        self.server.connection.execute("PRAGMA foreign_keys = ON")
        self.a.connection.execute(
            "INSERT INTO projects (id, name, remote, created_at, last_seen_at) VALUES ('prj-1', 'web', NULL, ?, ?)",
            (NOW, NOW),
        )
        record_id = self.remember(self.a, "The web app deploys with make deploy.")
        self.a.connection.execute("UPDATE records SET scope = 'project', project_id = 'prj-1' WHERE id = ?",
                                  (record_id,))
        project, record = sync.pending_operations(self.a)
        answer = self.send(self.a, "c-a", [record])[0]
        self.assertEqual(answer["result"], "rejected")
        self.assertIn("project", answer["reason"])
        self.assertEqual([item["result"] for item in self.send(self.a, "c-a", [project, record])],
                         ["applied", "applied"])

    def test_only_maintenance_reviews_can_be_deleted_and_unknown_columns_are_refused(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        answers = exchange.apply_operations(self.server, "c-a", [
            {"op_id": "x-1", "table": "records", "key": record_id, "op": "delete"},
            {"op_id": "x-2", "table": "records", "key": record_id, "op": "upsert", "row": {"owner": "me"}},
            {"op_id": "x-3", "table": "secrets", "key": "k", "op": "upsert", "row": {}},
            {"op_id": "x-4", "table": "reviews", "key": "f-1", "op": "upsert",
             "row": {"scope_key": "global", "reviewed_at": NOW, "outcome": "reviewed", "failures": 0}},
            {"op_id": "x-5", "table": "reviews", "key": "f-1", "op": "delete"},
        ], now=NOW)
        self.assertEqual([answer["result"] for answer in answers],
                         ["rejected", "rejected", "rejected", "applied", "applied"])
        self.assertIsNotNone(sync.read_row(self.server.connection, sync.table_for("records"), record_id))

    def test_a_whole_computer_can_start_an_empty_server(self) -> None:
        for number in range(3):
            self.remember(self.a, f"Service number {number} listens on port 80{number}.")
        with self.a.transaction():
            self.a.connection.execute("DELETE FROM changes")
            for name in SYNCED_TABLES:  # as when a computer that kept its memory alone connects
                sync.note_all(self.a, name)
        operations = sync.pending_operations(self.a)
        answers = exchange.apply_operations(self.server, "c-a", operations, now=NOW)
        self.assertEqual({answer["result"] for answer in answers}, {"applied"})
        self.assertEqual(exchange.summary(self.server)["memories"], 3)

    def test_usage_counts_once_per_use(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        use = {"op_id": "u-1", "at": NOW, "operation": "recall", "agent": "codex", "query": "build",
               "result_ids": [record_id]}
        self.assertEqual(exchange.add_usage(self.server, "c-a", [use, use], now=NOW), 1)
        self.assertEqual(exchange.add_usage(self.server, "c-a", [use], now=NOW), 0)
        count = self.server.connection.execute("SELECT recall_count FROM records").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(sync.changes_since(self.server, 1)["changes"], [])

    def test_one_agent_at_a_time_holds_the_maintenance_lease(self) -> None:
        self.assertTrue(exchange.take_lease(self.server, "maintenance", "c-a", seconds=600, now=NOW)["granted"])
        taken = exchange.take_lease(self.server, "maintenance", "c-b", seconds=600, now=NOW)
        self.assertEqual((taken["granted"], taken["until"]), (False, "2026-10-01T12:10:00Z"))
        self.assertTrue(exchange.take_lease(self.server, "maintenance", "c-b", seconds=600,
                                            now="2026-10-01T12:10:01Z")["granted"])
        self.assertFalse(exchange.release_lease(self.server, "maintenance", "c-a"))
        self.assertTrue(exchange.release_lease(self.server, "maintenance", "c-b"))

    def test_a_full_copy_holds_the_memory_but_nothing_of_the_servers_own(self) -> None:
        self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        accounts.new_join_code(self.server, "Laptop - Codex", now=NOW)
        latest = exchange.make_copy(self.root / "server.db", self.root / "copy.db")
        self.assertEqual(latest, sync.latest_change(self.server))
        copy = Store.open(self.root / "copy.db")
        try:
            tables = {row[0] for row in copy.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse({name for name in tables if name.startswith("server_")})
            self.assertEqual(copy.connection.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 0)
            self.assertIsNone(copy.get_meta("changes.track"))
            self.assertIn("build-1", Memory(copy).recall("build server", project_path=self.plain))
        finally:
            copy.close()

    def test_a_full_copy_leaves_out_the_uses_and_their_totals_alike(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        for at in ("2025-01-01T00:00:00Z", NOW):
            self.server.log_usage(at=at, operation="recall", agent="codex", project_id=None, query="build",
                                  record_ids=[record_id])
        self.assertEqual(self.server.roll_up_usage(before="2025-10-01T00:00:00Z"), 1)
        exchange.make_copy(self.root / "server.db", self.root / "copy.db")
        copy = Store.open(self.root / "copy.db")
        try:
            # The uses are the server's bookkeeping, newer ones as rows and older ones as totals: neither comes.
            counts = [copy.connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                      for name in ("usage", "usage_totals")]
            self.assertEqual(counts, [0, 0])
            self.assertEqual(copy.record_details(record_id)["uses"], {})
            # Each memory's own "used N times" comes with it.
            self.assertEqual(copy.record_details(record_id)["recall_count"], 2)
        finally:
            copy.close()

    def test_tidying_keeps_only_each_rows_newest_change(self) -> None:
        record_id = self.remember(self.a, "The build server is build-1.")
        self.send(self.a, "c-a")
        Memory(self.a, agent="codex").forget(record_id)
        self.send(self.a, "c-a")
        exchange.tidy(self.server, now=NOW)
        rows = self.server.connection.execute("SELECT tbl, key FROM changes").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("records", record_id)])
        self.assertEqual(sync.changes_since(self.server, 0)["changes"][0]["row"]["status"], "retired")


class BackupTests(TemporaryFolder):
    def test_a_backup_a_day_and_the_last_seven_kept(self) -> None:
        open_store(self.root / "server.db").close()
        folder = self.root / "backups"
        start = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)
        self.assertTrue(backups.due(folder, now=start))
        for day in range(9):
            backups.make(self.root / "server.db", folder, now=start + timedelta(days=day))
        found = backups.backups(folder)
        self.assertEqual(len(found), 7)
        self.assertEqual(backups.made_at(found[0]), start + timedelta(days=8))
        self.assertFalse(backups.due(folder, now=start + timedelta(days=8, hours=23)))
        self.assertTrue(backups.due(folder, now=start + timedelta(days=9)))
        check = sqlite3.connect(str(found[0]))
        try:
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            check.close()

    def test_a_burst_of_backups_does_not_push_out_the_daily_ones(self) -> None:
        open_store(self.root / "server.db").close()
        folder = self.root / "backups"
        start = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)
        for day in range(7):
            backups.make(self.root / "server.db", folder, now=start + timedelta(days=day))
        burst = start + timedelta(days=6, hours=2)
        for hour in range(8):  # such as a runaway computer sending changes for hours
            backups.make(self.root / "server.db", folder, now=burst + timedelta(hours=hour))
        kept = [backups.made_at(path) for path in backups.backups(folder)]
        self.assertEqual(kept[:3], [burst + timedelta(hours=hour) for hour in (7, 6, 5)])
        self.assertEqual(kept[3:], [start + timedelta(days=day) for day in range(5, -1, -1)])
        self.assertEqual(backups.made_at(backups.latest(folder)), burst + timedelta(hours=7))


class WebTests(TemporaryFolder):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "server-home"
        self.server = KnowItAll2Server(self.home, port=0, housekeeping_seconds=3600)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.address = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.thread.join(timeout=5)
        super().tearDown()

    def join_code(self, name: str = "Workstation - Codex") -> str:
        store = self.server.open()
        try:
            return accounts.new_join_code(store, name, now=self.server.clock())["code"]
        finally:
            store.close()

    def client(self, name: str = "Workstation - Codex", agent: str = "codex") -> RemoteClient:
        client = RemoteClient(self.address, agent=agent, computer="desk")
        client.join(self.join_code(name))
        return client

    def test_health_needs_no_key_and_shows_no_memories(self) -> None:
        found = RemoteClient(self.address).health()
        self.assertEqual((found["product"], found["api"]), ("knowitall2", 1))
        with urllib.request.urlopen(self.address + "/", timeout=5) as response:
            self.assertIn(b"<title>KnowItAll2 server</title>", response.read())

    def test_every_other_call_needs_a_key(self) -> None:
        for call in (lambda c: c.hello(), lambda c: c.changes(0), lambda c: c.push([]), lambda c: c.copy()):
            with self.assertRaises(RemoteError) as raised:
                call(RemoteClient(self.address, key="kia_not-a-real-key"))
            self.assertEqual(raised.exception.status, 401)

    def test_two_agents_share_one_memory(self) -> None:
        codex, claude = self.client(), self.client("Workstation - Claude Code", "claude-code")
        hello = claude.hello()
        self.assertEqual((hello["connection"]["agent"], hello["connection"]["computer"]), ("claude-code", "desk"))
        a, b = computer(self.root, "a"), computer(self.root, "b")
        try:
            record_id = self.remember(a, "The build server is build-1.")
            operations = sync.pending_operations(a)
            answer = codex.push(operations)
            self.assertEqual(answer["results"][0]["result"], "applied")
            sync.acknowledge(a, operations)
            found = claude.changes(0)
            sync.apply_pulled(b, found["changes"], cursor=found["next"])
            self.assertIn(record_id, Memory(b).recall("build server", project_path=self.plain))
        finally:
            a.close()
            b.close()

    def test_a_new_computer_downloads_a_full_copy(self) -> None:
        client = self.client()
        a = computer(self.root, "a")
        try:
            self.remember(a, "The build server is build-1.")
            client.push(sync.pending_operations(a))
        finally:
            a.close()
        data, latest = client.copy()
        (self.root / "fresh.db").write_bytes(data)
        fresh = Store.open(self.root / "fresh.db")
        try:
            self.assertEqual(latest, 1)
            self.assertIn("build-1", Memory(fresh).recall("build server", project_path=self.plain))
        finally:
            fresh.close()

    def test_guessing_join_codes_is_slowed_down(self) -> None:
        guesser = RemoteClient(self.address)
        for _ in range(JOIN_FAILURES_PER_ADDRESS):
            with self.assertRaises(RemoteError) as raised:
                guesser.join("AAAA-BBBB-CCCC-DDDD")
            self.assertEqual(raised.exception.status, 400)
        with self.assertRaises(RemoteError) as raised:
            guesser.join(self.join_code())
        self.assertEqual(raised.exception.status, 429)

    def test_a_removed_agent_is_turned_away(self) -> None:
        client = self.client()
        store = self.server.open()
        try:
            accounts.remove(store, client.hello()["connection"]["id"], now=self.server.clock())
        finally:
            store.close()
        with self.assertRaises(RemoteError) as raised:
            client.hello()
        self.assertEqual(raised.exception.status, 401)

    def test_the_lease_and_usage_over_the_web(self) -> None:
        codex, claude = self.client(), self.client("Workstation - Claude Code", "claude-code")
        self.assertTrue(codex.lease("maintenance", seconds=600)["granted"])
        self.assertFalse(claude.lease("maintenance", seconds=600)["granted"])
        self.assertTrue(codex.lease("maintenance", release=True)["released"])
        use = {"op_id": "u-1", "at": NOW, "operation": "briefing", "result_ids": []}
        self.assertEqual(claude.usage([use])["added"], 1)

    def test_an_unreachable_server_is_a_plain_error(self) -> None:
        self.server.stop()
        self.thread.join(timeout=5)
        with self.assertRaises(RemoteError) as raised:
            RemoteClient(self.address, timeout=1).health()
        self.assertIn("cannot reach", str(raised.exception))

    def test_a_burst_of_changes_gets_its_own_backup_an_hour_later(self) -> None:
        from datetime import datetime, timedelta, timezone

        from knowitall2.server.web import BACKUP_AFTER_CHANGES

        start = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)
        self.server.housekeeping(now=start)
        store = self.server.open()
        try:
            with store.transaction():
                for number in range(BACKUP_AFTER_CHANGES):
                    store.connection.execute("INSERT INTO changes (tbl, key, op) VALUES ('records', ?, 'upsert')",
                                             (f"k-{number}",))
        finally:
            store.close()
        self.server.housekeeping(now=start + timedelta(minutes=30))
        self.assertEqual(len(backups.backups(self.home / "backups")), 1)
        self.server.housekeeping(now=start + timedelta(hours=1))
        self.assertEqual(len(backups.backups(self.home / "backups")), 2)
        self.server.housekeeping(now=start + timedelta(hours=3))
        self.assertEqual(len(backups.backups(self.home / "backups")), 2)

    def test_head_requests_get_headers_without_a_body(self) -> None:
        request = urllib.request.Request(self.address + "/api/v1/health", method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual((response.status, response.read()), (200, b""))
            self.assertEqual(response.headers["X-KnowItAll2-API"], "1")

    def test_one_guesser_does_not_lock_out_everyone_else(self) -> None:
        guard = self.server.join_guard
        for number in range(40):
            if guard.begin(f"10.0.0.{number % 8}"):
                guard.end(f"10.0.0.{number % 8}", failed=True)
        self.assertFalse(guard.begin("10.0.0.1"))
        self.assertTrue(guard.begin("10.0.0.99"))

    def test_housekeeping_makes_the_daily_backup(self) -> None:
        self.server.housekeeping()
        self.assertEqual(len(backups.backups(self.home / "backups")), 1)
        self.server.housekeeping()
        self.assertEqual(len(backups.backups(self.home / "backups")), 1)

    def test_housekeeping_folds_uses_older_than_a_year_into_totals(self) -> None:
        store = self.server.open()
        try:
            for at in ("2025-01-01T00:00:00Z", "2026-09-30T00:00:00Z"):
                store.log_usage(at=at, operation="recall", agent="codex", project_id=None, query="router",
                                record_ids=["k-1"])
        finally:
            store.close()
        self.server.housekeeping(now=datetime(2026, 10, 3, tzinfo=timezone.utc))
        store = self.server.open()
        try:
            self.assertEqual(1, store.connection.execute("SELECT COUNT(*) FROM usage").fetchone()[0])
            self.assertEqual([("k-1", "recall", 1)],
                             [tuple(row) for row in store.connection.execute("SELECT * FROM usage_totals")])
        finally:
            store.close()


class ThrottleTests(unittest.TestCase):
    def test_tries_still_running_count_against_the_limit(self) -> None:
        moment = [1000.0]
        guard = Throttle(clock=lambda: moment[0])
        for _ in range(JOIN_FAILURES_PER_ADDRESS):
            self.assertTrue(guard.begin("10.0.0.1"))
        self.assertFalse(guard.begin("10.0.0.1"))  # none has finished, yet a sixth at once is refused
        self.assertTrue(guard.begin("10.0.0.2"))
        guard.end("10.0.0.1", failed=False)
        self.assertTrue(guard.begin("10.0.0.1"))  # a try that worked gives its place back
        for _ in range(JOIN_FAILURES_PER_ADDRESS):
            guard.end("10.0.0.1", failed=True)
        self.assertFalse(guard.begin("10.0.0.1"))
        moment[0] += JOIN_WINDOW_SECONDS + 1
        self.assertTrue(guard.begin("10.0.0.1"))

    def test_an_ipv6_caller_counts_as_its_whole_64_bit_network(self) -> None:
        guard = Throttle()
        for number in range(1, JOIN_FAILURES_PER_ADDRESS + 1):
            self.assertTrue(guard.begin(f"2001:db8::{number}"))
            guard.end(f"2001:db8::{number}", failed=True)
        self.assertFalse(guard.begin("2001:db8::ffff:1"))
        self.assertTrue(guard.begin("2001:db8:0:1::1"))
        self.assertTrue(guard.begin("10.0.0.1"))


class TrustedProxyTests(TemporaryFolder):
    def test_a_proxy_can_be_named_by_address_or_network(self) -> None:
        server = KnowItAll2Server(self.root / "home", port=0, trusted_proxy="172.18.0.0/16, 10.0.0.7, nonsense")
        try:
            self.assertTrue(server.trusts("172.18.4.9"))
            self.assertTrue(server.trusts("10.0.0.7"))
            self.assertFalse(server.trusts("10.0.0.8"))
            self.assertFalse(server.trusts("not an address"))
        finally:
            server.server_close()
        nobody = KnowItAll2Server(self.root / "home", port=0)
        try:
            self.assertFalse(nobody.trusts("127.0.0.1"))
        finally:
            nobody.server_close()


class CommandTests(TemporaryFolder):
    def test_a_join_code_from_the_command_line(self) -> None:
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "home")}):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["server-join-code", "Laptop - Codex"]), 0)
        self.assertRegex(output.getvalue(), r"Join code for Laptop - Codex: [A-Z2-9]{4}(-[A-Z2-9]{4}){3}")


if __name__ == "__main__":
    unittest.main()
