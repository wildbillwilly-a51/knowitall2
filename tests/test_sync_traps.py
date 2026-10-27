"""Sync traps closed without a protocol change (review 2026-10-24, R3: F-11b, F-12, F-13, F-14)."""

import json
import os
from datetime import datetime, timezone
from unittest import mock

from _support import Clock

from test_connected import NOW, TwoComputers

from knowitall2 import connected, doctor, question_schema, sync
from knowitall2.app import api
from knowitall2.memory import Memory
from knowitall2.remote import RemoteClient, RemoteError
from knowitall2.store import SYNCED_TABLES

LATER = "2026-10-01T13:00:00Z"
# Split so repository secret scanners do not flag this file.
FAKE_TOKEN = "gl" + "pat-" + "a1B2c3D4e5F6g7H8i9J0k1L2"


class ConnectedPair(TwoComputers):
    """Two connected computers, and a look at the server's own rows."""

    def setUp(self) -> None:
        super().setUp()
        self.connect("a")
        self.connect("b", upload=False)

    def server_row(self, table: str, key: str) -> dict | None:
        store = self.server.open()
        try:
            return sync.read_row(store.connection, sync.table_for(table), key)
        finally:
            store.close()

    def server_latest(self) -> int:
        store = self.server.open()
        try:
            return sync.latest_change(store)
        finally:
            store.close()

    def on_server_store(self, sql: str, parameters: tuple) -> None:
        store = self.server.open()
        try:
            with store.transaction():
                store.connection.execute(sql, parameters)
        finally:
            store.close()

class SyncTrapTests(ConnectedPair):
    def test_a_lost_answer_and_another_computers_edit_still_converge(self) -> None:
        # F-11b: a's send is applied, but its answer is lost; b then changes the row; a's resend gets the cached
        # answer, without b's version, and used to keep its own version for good.
        with self.on("a") as store:
            record_id = self.remember(store, "The build server is build-1.")
        self.sync("a")
        self.sync("b")
        with self.on("a") as store:
            Memory(store, agent="codex", clock=Clock("2026-10-01T12:30:00Z")).forget(record_id, reason="first")
        push = RemoteClient.push

        def lost(client, operations):
            push(client, operations)  # the server applies it
            raise RemoteError("timed out", status=None)

        with mock.patch.object(RemoteClient, "push", lost):
            self.assertIn("timed out", self.sync("a").problem)
        self.sync("b")
        with self.on("b") as store:
            self.assertEqual("first", store.get(record_id).retired_reason)
            store.connection.execute("UPDATE records SET retired_reason = 'second', updated_at = ? WHERE id = ?",
                                     (LATER, record_id))
        self.assertIsNone(self.sync("b").problem)
        self.assertEqual("second", self.server_row("records", record_id)["retired_reason"])
        for _ in range(2):  # no further edits anywhere
            self.assertIsNone(self.sync("a").problem)
        with self.on("a") as store:
            self.assertEqual("second", store.get(record_id).retired_reason)
            self.assertFalse(sync.has_pending(store))

    def test_a_row_this_version_cannot_use_is_set_aside_and_used_after_an_upgrade(self) -> None:
        # F-12: one question of a kind only a newer version knows used to fail every page, push included.
        self.on_server_store(
            "INSERT INTO questions (id, kind, prompt, record_ids, options, status, created_at) "
            "VALUES ('q-new', 'ask_later', 'Later?', '[\"k-1\"]', '[{\"key\": \"later\", \"label\": \"Later\"}]', "
            "'open', ?)", (NOW,))
        self.on_server_store(
            "INSERT INTO question_stages (question_id, stage, labels, findings, updated_at) "
            "VALUES ('q-new', 'asked', '{}', '[]', ?)", (NOW,))
        with self.on("a") as store:
            record_id = self.remember(store, "The build server is build-1.")
        self.sync("a")
        report = self.sync("b")
        self.assertIsNone(report.problem)
        with self.on("b") as store:
            self.assertEqual("active", store.get(record_id).status)  # the rest still arrives
            self.assertIsNone(store.question("q-new"))
            aside = {(entry["change"]["table"], entry["change"]["key"]) for entry in sync.set_aside(store)}
            self.assertEqual({("questions", "q-new"), ("question_stages", "q-new")}, aside)
            self.assertEqual(self.server_latest(), sync.cursor(store))
            self.remember(store, "The test server is test-1.")
        self.assertEqual(1, self.sync("b").sent)  # and what b saves still goes
        with mock.patch.dict(question_schema.QUESTION_CHOICES, {"ask_later": (1, {"later"})}):  # an upgrade
            self.assertIsNone(self.sync("b").problem)
            with self.on("b") as store:
                self.assertEqual("ask_later", store.question("q-new")["kind"])
                self.assertEqual("asked", store.question_stage("q-new")["stage"])
                self.assertEqual([], sync.set_aside(store))

    def test_a_row_set_aside_is_dropped_when_a_newer_version_arrives(self) -> None:
        self.on_server_store(
            "INSERT INTO questions (id, kind, prompt, record_ids, options, status, created_at) "
            "VALUES ('q-new', 'ask_later', 'Later?', '[\"k-1\"]', '[]', 'open', ?)", (NOW,))
        self.sync("b")
        with self.on("b") as store:
            self.assertEqual(1, len(sync.set_aside(store)))
        # Corrected on the server: a kind this version knows.
        options = json.dumps([{"key": "keep", "label": "Keep"}, {"key": "forget", "label": "Forget"}])
        self.on_server_store("UPDATE questions SET kind = 'still_true', options = ? WHERE id = 'q-new'", (options,))
        self.sync("b")
        with self.on("b") as store:
            self.assertEqual("still_true", store.question("q-new")["kind"])
            self.assertEqual([], sync.set_aside(store))

    def test_a_child_of_a_refused_parent_is_set_aside_and_goes_again_with_it(self) -> None:
        # F-13: the parent was refused for good, the child was refused "for now", the parent noted again, both
        # sent and refused again: a problem line and a sync after every tool call, for ever.
        with self.on("a") as store:
            record_id = self.remember(store, "The NAS is nas01.")
            # Saved before this computer's screening knew the shape; the server's knows it.
            store.connection.execute("UPDATE records SET text = ? WHERE id = ?",
                                     (f"The NAS key is {FAKE_TOKEN}", record_id))
            store.upsert_system(system_id="s-nas", name="NAS", area="storage", kind="host", aliases=[], now=NOW)
            store.set_note(record_id, headline="NAS key", system_id="s-nas", facet="access", written_by="cataloguer",
                           now=NOW)
        first = self.sync("a")
        self.assertIsNone(first.problem)
        self.assertIsNone(self.server_row("records", record_id))
        self.assertIsNone(self.server_row("record_notes", record_id))  # never accepted without its memory
        self.assertIsNotNone(self.server_row("systems", "s-nas"))
        with self.on("a") as store:
            self.assertFalse(sync.has_pending(store))
            kept = {item["table"]: item for item in sync.rejections(store)}
            self.assertEqual({"records", "record_notes"}, set(kept))
            self.assertEqual(["records", record_id], kept["record_notes"]["waits_for"])
        for _ in range(2):
            report = self.sync("a")
            self.assertEqual((0, 0), (report.sent, report.refused))
            with self.on("a") as store:
                self.assertFalse(sync.has_pending(store))
        with self.on("a") as store:  # the memory is corrected, and sent again: its note goes with it
            store.connection.execute("UPDATE records SET text = 'The NAS key is in Vaultwarden.' WHERE id = ?",
                                     (record_id,))
            parent = next(item for item in sync.rejections(store) if item["table"] == "records")
            sync.resolve_rejection(store, parent["id"], retry=True)
            self.assertEqual([], sync.rejections(store))
        self.assertIsNone(self.sync("a").problem)
        self.assertEqual("NAS key", self.server_row("record_notes", record_id)["headline"])

    def test_every_link_between_shared_rows_is_declared(self) -> None:
        # F-14: only a memory's project was a link; a note's system and a stage's question were not.
        declared = {name: dict(table.parents) for name, table in SYNCED_TABLES.items()}
        self.assertEqual({"project_id": "projects"}, declared["records"])
        self.assertEqual({"record_id": "records", "system_id": "systems"}, declared["record_notes"])
        self.assertEqual({"question_id": "questions"}, declared["question_stages"])
        self.assertEqual({"question_id": "questions", "system_id": "systems", "project_id": "projects"},
                         declared["agent_tasks"])
        with self.on("a") as store:
            for name, table in SYNCED_TABLES.items():
                columns = {row[1] for row in store.connection.execute(f"PRAGMA table_info({name})")}
                for column, parent in table.parents:
                    self.assertIn(column, columns)
                    self.assertIn(parent, SYNCED_TABLES)

    def test_a_note_whose_system_comes_later_waits_for_it_on_both_sides(self) -> None:
        # F-14: the server accepted a note whose system it did not have; a computer saved it without its system.
        with self.on("a") as store:
            record_id = self.remember(store, "The NAS is nas01.")
            store.upsert_system(system_id="s-nas", name="NAS", area="storage", kind="host", aliases=[], now=NOW)
            store.set_note(record_id, headline="NAS", system_id="s-nas", facet="access", written_by="cataloguer",
                           now=NOW)
            store.connection.execute("DELETE FROM changes WHERE tbl = 'systems'")  # the system was never sent
        self.sync("a")
        self.assertIsNone(self.server_row("record_notes", record_id))  # the server would not take it alone
        self.sync("a")
        self.assertIsNotNone(self.server_row("systems", "s-nas"))  # noted again, and sent, when the note waited
        self.assertEqual("s-nas", self.server_row("record_notes", record_id)["system_id"])
        with self.on("a") as store:
            self.assertFalse(sync.has_pending(store))

    def test_a_fetched_note_without_its_system_is_set_aside_until_the_system_arrives(self) -> None:
        with self.on("a") as store:
            record_id = self.remember(store, "The NAS is nas01.")
        self.sync("a")
        # An orphan an older computer left on the server.
        self.on_server_store("INSERT INTO record_notes (record_id, headline, system_id, facet, written_by, written_at) "
                             "VALUES (?, 'NAS', 's-late', 'access', 'cataloguer', ?)", (record_id, NOW))
        self.assertIsNone(self.sync("b").problem)
        with self.on("b") as store:
            self.assertEqual("active", store.get(record_id).status)
            self.assertEqual({}, store.notes_for([record_id]))
            [entry] = sync.set_aside(store)
            self.assertIn("systems s-late", entry["reason"])
        self.on_server_store("INSERT INTO systems (id, name, area, kind, created_at, updated_at) "
                             "VALUES ('s-late', 'NAS', 'storage', 'host', ?, ?)", (LATER, LATER))
        self.assertIsNone(self.sync("b").problem)
        with self.on("b") as store:
            self.assertEqual("s-late", store.notes_for([record_id])[record_id]["system_id"])
            self.assertEqual([], sync.set_aside(store))


class SecretSyncTests(ConnectedPair):
    """Secrets that reached a store before the screening (Codex's review of step A)."""

    def test_a_secret_in_a_finding_on_the_server_is_redacted_here_and_sent_back_clean(self) -> None:
        # Written by an older computer, or kept from before the server screened findings.
        self.on_server_store(
            "INSERT INTO questions (id, kind, prompt, record_ids, options, status, created_at) VALUES ('q-1', "
            "'still_true', 'Still true?', '[\"k-1\"]', ?, 'open', ?)",
            (json.dumps([{"key": "keep", "label": "Keep"}, {"key": "forget", "label": "Forget"}]), NOW))
        self.on_server_store("INSERT INTO question_stages (question_id, stage, labels, findings, updated_at) "
                             "VALUES ('q-1', 'asked', '{}', ?, ?)", (json.dumps([f"saw token={FAKE_TOKEN}"]), NOW))
        self.assertIsNone(self.sync("b").problem)
        with self.on("b") as store:
            self.assertEqual(["saw token=[REDACTED GitLab token]"], store.question_stage("q-1")["findings"])
        self.assertIsNone(self.sync("b").problem)  # the cleaned row goes back
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.server_row("question_stages", "q-1")))
        with self.on("b") as store:
            self.assertFalse(sync.has_pending(store))

    def test_taking_a_tag_off_a_memory_with_an_old_secret_tag_reaches_the_server(self) -> None:
        with self.on("a") as store:
            record_id = self.remember(store, "The NAS is nas01.")
        self.sync("a")
        tags = json.dumps(["disputed", FAKE_TOKEN])
        self.on_server_store("UPDATE records SET tags = ? WHERE id = ?", (tags, record_id))
        self.sync("a")
        with self.on("a") as store:
            self.assertEqual(("disputed", FAKE_TOKEN), store.get(record_id).tags)
            store.set_tags(record_id, [FAKE_TOKEN], now=LATER)  # as closing its question does
        report = self.sync("a")
        self.assertEqual((1, 0), (report.sent, report.refused))
        self.assertEqual([FAKE_TOKEN], json.loads(self.server_row("records", record_id)["tags"]))


class HonestHealthTests(TwoComputers):
    """R3 group fix 4: a server that answers is not a sync that works."""

    def setUp(self) -> None:
        super().setUp()
        self.connect("a")

    def status(self, **values) -> None:
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "a")}):
            path = connected.folder() / "status.json"
            path.write_text(json.dumps({"last_attempt": "2026-10-27T11:59:00Z", **values}), encoding="utf-8")

    def health(self) -> tuple[bool, str, dict, doctor.Check]:
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "a")}):
            healthy, lines = connected.describe_status()
            board = {"level": "ok", "headline": "", "items": []}
            api._add_sharing_health(board, api._sharing())
            return healthy, " ".join(lines), board, doctor._sharing_check()

    def test_a_sync_failing_for_an_hour_is_unhealthy_though_the_server_answers(self) -> None:
        problem = "the server sent something this computer could not use: unsupported question kind"
        self.status(problem=problem, problem_kind="data", last_success="2026-10-27T10:00:00Z")
        with mock.patch.object(connected.time, "time", return_value=_epoch("2026-10-27T12:00:00Z")):
            healthy, text, board, check = self.health()
        self.assertFalse(healthy)
        self.assertIn("The server answers", text)  # reachable...
        self.assertIn(problem, text)  # ...and still the recorded problem is shown
        self.assertIn("No sync has worked since 2026-10-27T10:00:00Z", text)
        self.assertFalse(check.ok)
        self.assertIn("sync", check.fix)
        [item] = board["items"]
        self.assertEqual("problem", item["level"])
        self.assertNotIn("could not be reached", item["text"])  # the app no longer calls every problem that
        self.assertIn("unsupported question kind", item["text"])

    def test_a_recent_failure_is_shown_but_not_yet_unhealthy(self) -> None:
        self.status(problem="the memory store on this computer failed during the sync: database is locked",
                    problem_kind="store", last_success="2026-10-27T11:50:00Z")
        with mock.patch.object(connected.time, "time", return_value=_epoch("2026-10-27T12:00:00Z")):
            healthy, text, board, check = self.health()
        self.assertTrue(healthy)
        self.assertIn("database is locked", text)
        self.assertTrue(check.ok)
        self.assertEqual("attention", board["items"][0]["level"])
        self.assertIn("did not work: the memory store", board["items"][0]["text"])

    def test_rows_set_aside_are_reported(self) -> None:
        self.status(set_aside=2, last_success="2026-10-27T11:59:00Z")
        healthy, text, board, _ = self.health()
        self.assertTrue(healthy)
        self.assertIn("2 row(s) from the server are set aside", text)
        self.assertIn("set aside", board["items"][0]["text"])


def _epoch(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


if __name__ == "__main__":
    import unittest

    unittest.main()
