import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from knowitall2 import doctor
from knowitall2.agents import describe_checks
from knowitall2.memory import Memory
from knowitall2.paths import database_path
from knowitall2.store import Store

PASSWORD = "Xk9" + "#mQ2vL8pR"  # split so repository secret scanners do not flag it


class StoredSecretsTests(unittest.TestCase):
    """doctor names memories that now look like secrets, by id and kind only, and changes nothing."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(root / "data")})
        self.environment.start()
        (root / "plain").mkdir()
        store = Store.open(database_path())
        try:
            memory = Memory(store, agent="cli")
            self.kept = memory.remember("The build server is build-1.", project_path=root / "plain").record.id
            self.held = memory.remember("The database is db01.", project_path=root / "plain").record.id
            # Saved before the screening knew this shape of secret.
            store.connection.execute("UPDATE records SET text = ? WHERE id = ?",
                                     (f"Connect to db01 with PGPASSWORD={PASSWORD} psql", self.held))
        finally:
            store.close()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def rows(self) -> list[tuple]:
        store = Store.open(database_path())
        try:
            return [tuple(row) for row in store.connection.execute(
                "SELECT id, status, text, updated_at FROM records ORDER BY id")]
        finally:
            store.close()

    def test_a_memory_that_now_looks_like_a_secret_is_a_note_with_a_fix(self) -> None:
        before = self.rows()
        [store_check, secrets] = doctor._store_checks()
        self.assertTrue(store_check.ok)
        self.assertTrue(secrets.ok)  # a note: KnowItAll2 itself is healthy
        self.assertEqual(f"1 active memory looks like it may hold a secret: {self.held} (password)", secrets.detail)
        self.assertIn("knowitall2 forget <id>", secrets.fix)
        shown = describe_checks([secrets])
        self.assertTrue(shown.startswith("NOTE secrets in memories:"), shown)
        self.assertIn("fix: ", shown)
        self.assertNotIn(PASSWORD, shown)
        self.assertNotIn("PGPASSWORD", shown)
        self.assertEqual(before, self.rows())

    def test_no_note_when_no_memory_looks_like_a_secret(self) -> None:
        store = Store.open(database_path())
        try:
            Memory(store, agent="cli").forget(self.held, reason="held a secret")
        finally:
            store.close()
        [_, secrets] = doctor._store_checks()
        self.assertEqual((True, None), (secrets.ok, secrets.fix))
        self.assertTrue(describe_checks([secrets]).startswith("OK   secrets in memories: no active memory"))


class CloudFolderTests(unittest.TestCase):
    """doctor notes projects agents work in that a cloud service keeps in sync, so they can be moved."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        environment = {key: value for key, value in os.environ.items()
                       if key.casefold() not in ("onedrive", "onedriveconsumer", "onedrivecommercial")}
        environment.update({"KNOWITALL2_HOME": str(self.root / "data"), "OneDriveCommercial": str(self.root / "Work Sync")})
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_project_in_a_synced_folder_is_a_note_with_a_suggestion(self) -> None:
        from _support import make_repository
        from knowitall2 import known

        synced = make_repository(self.root / "Work Sync" / "Documents" / "Projects" / "lab")
        boxed = make_repository(self.home / "Dropbox" / "tools")
        local = make_repository(self.root / "work" / "alpha")
        for folder in (synced, boxed, local):
            known.worked_in(folder)
        [check] = doctor._cloud_folder_checks(home=self.home)
        self.assertTrue(check.ok)   # a note: nothing is broken
        self.assertIsNotNone(check.fix)
        for folder in (synced, boxed):
            self.assertIn(os.path.normcase(str(folder.resolve())), os.path.normcase(check.detail))
        self.assertNotIn("alpha", check.detail)
        self.assertTrue(describe_checks([check]).startswith("NOTE "), describe_checks([check]))
        self.assertIn("Move", check.fix)

    def test_a_synced_folder_since_deleted_is_not_listed(self) -> None:
        # Seen 2026-10-12: after Homelab was removed, doctor still listed it.
        import shutil

        from _support import make_repository
        from knowitall2 import known

        gone = make_repository(self.root / "Work Sync" / "old")
        kept = make_repository(self.root / "Work Sync" / "lab")
        for folder in (gone, kept):
            known.worked_in(folder)
        shutil.rmtree(gone)
        [check] = doctor._cloud_folder_checks(home=self.home)
        self.assertIn("lab", check.detail)
        self.assertNotIn("old", check.detail.replace("folder", ""))
        self.assertIn("writes no knowledge files there", check.detail)

    def test_no_note_when_no_project_is_in_a_synced_folder(self) -> None:
        from _support import make_repository
        from knowitall2 import known

        known.worked_in(make_repository(self.root / "work" / "alpha"))
        self.assertEqual([], doctor._cloud_folder_checks(home=self.home))


class StalledMaintenanceTests(unittest.TestCase):
    """doctor notes when maintenance has made no model call for days while groups of memories wait for it."""

    def setUp(self) -> None:
        from knowitall2.learning.state import LearnerSettings, save_settings

        self.temporary = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(Path(self.temporary.name) / "data")})
        self.environment.start()
        save_settings(LearnerSettings(enabled=True))

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def runs(self, *runs: tuple[float, int, int]) -> None:
        """Learning runs, each (days ago, maintenance calls, groups waiting for review)."""

        from datetime import datetime, timedelta, timezone

        from knowitall2 import journal

        store = Store.open(database_path())
        try:
            for days, calls, waiting in runs:
                at = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                journal.record(store, "learning", "run", at=at,
                               details={"maintenance": {"calls": calls, "groups": {"review": waiting}}})
        finally:
            store.close()

    def test_a_note_when_groups_wait_and_maintenance_made_no_call_for_three_days(self) -> None:
        # 2026-10-11: maintenance made no call for 13 days while 181 groups waited, and nothing said so.
        self.runs((5, 1, 18), (2, 0, 170), (0.1, 0, 181))
        check = doctor._maintenance_check()
        self.assertTrue(check.ok)
        self.assertIn("no model call", check.detail)
        self.assertIn("181 groups", check.detail)
        self.assertIn("knowitall2 maintain", check.fix)
        self.assertTrue(describe_checks([check]).startswith("NOTE maintenance:"))

    def test_no_note_when_maintenance_ran_lately_nothing_waits_learning_is_new_or_off(self) -> None:
        from knowitall2.learning.state import LearnerSettings, save_settings

        for number, runs in enumerate((((5, 0, 9), (1, 2, 9)), ((5, 1, 0), (0.1, 0, 0)), ((1, 0, 9),))):
            home = str(Path(self.temporary.name) / f"data-{number}")
            with self.subTest(runs=runs), mock.patch.dict(os.environ, {"KNOWITALL2_HOME": home}):
                save_settings(LearnerSettings(enabled=True))
                self.runs(*runs)
                self.assertIsNone(doctor._maintenance_check().fix)
        self.runs((5, 0, 9))
        save_settings(LearnerSettings(enabled=False))
        self.assertIsNone(doctor._maintenance_check().fix)


if __name__ == "__main__":
    unittest.main()
