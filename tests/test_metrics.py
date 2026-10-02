import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository

from knowitall2.cli import main
from knowitall2.memory import Memory
from knowitall2.store import SCHEMA_VERSION, Store, StoreError


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = make_repository(Path(self.temporary.name) / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.clock = Clock("2026-09-28T12:00:00Z")
        self.store = Store.in_memory()
        self.memory = Memory(self.store, agent="claude-code", clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def remember(self, text: str, **options):
        options.setdefault("project_path", self.project)
        return self.memory.remember(text, **options)

    def test_recalls_and_briefings_are_counted(self) -> None:
        router = self.remember("The homelab router runs OpenWrt.")
        self.remember("We chose SQLite for storage.", kind="decision")
        self.memory.recall("openwrt router", project_path=self.project)
        self.memory.recall("kubernetes cluster", project_path=self.project)
        self.memory.briefing(project_path=self.project)
        summary = self.store.usage_summary(since="2026-01-01T00:00:00Z")
        self.assertEqual({"count": 2, "with_results": 1}, summary["operations"]["recall"])
        self.assertEqual({"count": 1, "with_results": 1}, summary["operations"]["briefing"])
        self.assertEqual(router.record.id, summary["top"][0]["id"])
        self.assertEqual(0, summary["never_used"])
        self.assertEqual({"claude-code": 2}, summary["sources"])

    def test_logged_queries_are_redacted(self) -> None:
        self.memory.recall("router password=hunter22", project_path=self.project)
        rows = self.store._connection.execute("SELECT query FROM usage").fetchall()
        self.assertNotIn("hunter22", rows[0][0])

    def test_a_metrics_failure_never_breaks_recall(self) -> None:
        self.remember("The homelab router runs OpenWrt.")
        with mock.patch.object(self.store, "log_usage", side_effect=sqlite3.OperationalError("locked")):
            self.assertIn("OpenWrt", self.memory.recall("router", project_path=self.project))


class FreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = make_repository(Path(self.temporary.name) / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.clock = Clock("2025-01-01T00:00:00Z")
        self.store = Store.in_memory()
        self.memory = Memory(self.store, agent="learner", clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_recent_memories_outrank_stale_ones_but_the_users_words_do_not_decay(self) -> None:
        stale = self.memory.remember("The backup target is nas01.", project_path=self.project)
        stated = self.memory.remember("The backup schedule is nightly.", source="user", project_path=self.project)
        self.clock.value = "2026-09-28T00:00:00Z"
        fresh = self.memory.remember("The backup target is nas02.", project_path=self.project)
        ranked = self.memory.recall("backup target", project_path=self.project)
        self.assertLess(ranked.index(fresh.record.id), ranked.index(stale.record.id))
        from knowitall2.memory import _freshness, _parse_time
        now = _parse_time(self.clock.value)
        self.assertEqual(1.0, _freshness(self.store.get(stated.record.id), now))
        self.assertLess(_freshness(self.store.get(stale.record.id), now), 0.8)


class MigrationTests(unittest.TestCase):
    def test_a_version_1_database_is_upgraded_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "knowitall2.db"
            store = Store.open(path)
            Memory(store, agent="cli", clock=Clock()).remember("Kept across the upgrade.")
            store.close()
            raw = sqlite3.connect(path)
            raw.executescript(
                "DROP TRIGGER changes_records_insert; DROP TRIGGER changes_records_update; "
                "DROP TRIGGER changes_records_delete; DELETE FROM meta WHERE key = 'changes.triggers';"
                "ALTER TABLE records DROP COLUMN recall_count; ALTER TABLE records DROP COLUMN last_used_at;"
                "ALTER TABLE records DROP COLUMN source_computer;"
                "DROP TABLE usage; UPDATE meta SET value = '1' WHERE key = 'schema_version';"
            )
            raw.close()
            store = Store.open(path)
            try:
                version = store._connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
                self.assertEqual(str(SCHEMA_VERSION), version)
                backup = sqlite3.connect(path.with_name("knowitall2.schema1-backup.db"))
                try:
                    self.assertEqual("1", backup.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
                    self.assertEqual(1, backup.execute("SELECT COUNT(*) FROM records").fetchone()[0])
                finally:
                    backup.close()
                columns = {row[1] for row in store._connection.execute("PRAGMA table_info(records)")}
                self.assertTrue({"recall_count", "last_used_at"} <= columns)
                self.assertEqual(1, store.stats()["active"])
                store.log_usage(at="2026-09-28T00:00:00Z", operation="recall", agent="cli", project_id=None,
                                query="kept", record_ids=[])
            finally:
                store.close()

    def test_a_newer_database_explains_that_a_restart_loads_the_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "knowitall2.db"
            Store.open(path).close()
            raw = sqlite3.connect(path)
            raw.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
            raw.commit()
            raw.close()
            with self.assertRaises(StoreError) as caught:
                Store.open(path)
            self.assertIn("restart the session or the agent app", str(caught.exception))
            self.assertIn("the memories are safe", str(caught.exception))


class StatsCommandTests(unittest.TestCase):
    def test_stats_reports_usefulness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = make_repository(Path(temporary) / "alpha", "https://gitlab.example.com/team/alpha.git")
            with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(Path(temporary) / "data")}):
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["remember", "The homelab router runs OpenWrt.", "--project-path", str(project)])
                    main(["recall", "openwrt", "--project-path", str(project)])
                    main(["recall", "kubernetes", "--project-path", str(project)])
                    output.truncate(0)
                    output.seek(0)
                    main(["stats"])
        text = output.getvalue()
        self.assertIn("recalls: 2, found something: 1 (50%)", text)
        self.assertIn("never returned yet: 0 of 1 memories", text)
        self.assertIn("By source: user 1", text)
        self.assertIn("used 1x:", text)


if __name__ == "__main__":
    unittest.main()
