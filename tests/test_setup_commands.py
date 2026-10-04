import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from knowitall2 import doctor
from knowitall2.agents import instructions
from knowitall2.cli import main
from knowitall2.paths import database_path
from knowitall2.store import Store, StoreError


class SetupCommandTests(unittest.TestCase):
    """End to end through the CLI, against a disposable user home and data home."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.user_home = root / "user"
        (self.user_home / ".codex").mkdir(parents=True)
        (self.user_home / ".claude").mkdir()
        (self.user_home / ".claude.json").write_text(json.dumps({"numStartups": 1}) + "\n", encoding="utf-8")
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(root / "data")})
        self.environment.start()
        # Where this checkout lives is not under test: a clone inside an app's package storage fails doctor.
        self.location = mock.patch.object(doctor, "in_package_storage", return_value=False)
        self.location.start()

    def tearDown(self) -> None:
        self.location.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([*arguments, "--user-home", str(self.user_home)])
        return code, output.getvalue(), errors.getvalue()

    def test_setup_doctor_and_uninstall_round_trip(self) -> None:
        code, output, _ = self.run_cli("setup", "codex")
        self.assertEqual(0, code)
        self.assertIn("registered the knowitall2 MCP server", output)
        self.assertIn("trust the KnowItAll2 SessionStart, Stop, and UserPromptSubmit hooks", output)
        code, output, _ = self.run_cli("setup", "claude-code")
        self.assertEqual(0, code)
        self.assertIn("Quit and reopen Claude Code", output)

        code, output, _ = self.run_cli("doctor")
        self.assertEqual(0, code, output)
        self.assertIn("OK   server starts: MCP handshake completed", output)
        self.assertIn("KnowItAll2 is healthy.", output)

        code, output, _ = self.run_cli("uninstall", "codex")
        self.assertEqual(0, code)
        self.assertIn("removed the knowitall2 MCP server", output)
        self.assertIn("Your memories were kept", output)

        code, output, _ = self.run_cli("doctor")
        self.assertEqual(0, code, output)
        self.assertIn("OK   Codex: not set up; skipped", output)

        # Asked about by name, an agent is checked in full.
        code, output, _ = self.run_cli("doctor", "--agent", "codex")
        self.assertEqual(1, code)
        self.assertIn("FAIL Codex registration: KnowItAll2 is not registered", output)
        # A clone has no knowitall2 program on PATH: the fix is the command that runs this installation.
        fix = next(line for line in output.splitlines() if line.strip().startswith("fix:"))
        self.assertIn("-P -m knowitall2 setup codex", fix)
        self.assertIn("PYTHONPATH", fix)

    def test_doctor_after_setting_up_one_agent_skips_the_other(self) -> None:
        code, _, _ = self.run_cli("setup", "codex")
        self.assertEqual(0, code)
        code, output, _ = self.run_cli("doctor")
        self.assertEqual(0, code, output)
        self.assertIn("OK   Claude Code: not set up; skipped", output)
        self.assertNotIn("Claude Code registration", output)
        self.assertIn("OK   Codex registration", output)

    def test_the_location_check_fails_inside_package_storage(self) -> None:
        self.assertTrue(doctor._location_check().ok)
        with mock.patch.object(doctor, "in_package_storage", return_value=True):
            check = doctor._location_check()
        self.assertFalse(check.ok)
        self.assertIn("private package storage", check.detail)

    def test_setup_errors_are_reported_without_a_traceback(self) -> None:
        (self.user_home / ".claude.json").write_text("{broken", encoding="utf-8")
        code, output, errors = self.run_cli("setup", "claude-code")
        self.assertEqual(1, code)
        self.assertEqual("", output)
        self.assertIn("is not valid JSON", errors)

    def test_setup_that_cannot_finish_spends_no_join_code(self) -> None:
        claude_md = self.user_home / ".claude" / "CLAUDE.md"
        claude_md.write_text(instructions.END + "\n" + instructions.BEGIN + "\n", encoding="utf-8")
        with mock.patch("knowitall2.cli._connect_agent") as connect:
            code, output, errors = self.run_cli("setup", "claude-code", "--server", "http://127.0.0.1:9",
                                                "--join-code", "ABCD-EFGH-JKLM-NPQR")
        self.assertEqual(1, code)
        connect.assert_not_called()
        self.assertIn("markers", errors)
        self.assertEqual(json.dumps({"numStartups": 1}) + "\n",
                         (self.user_home / ".claude.json").read_text(encoding="utf-8"))


class DoctorStoreTests(unittest.TestCase):
    """Doctor looks at the memory store without changing it, and suggests the fix for what is wrong."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(Path(self.temporary.name) / "data")})
        self.environment.start()
        self.path = database_path()
        Store.open(self.path).close()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def edit(self, script: str) -> None:
        raw = sqlite3.connect(self.path)
        try:
            raw.executescript(script)
        finally:
            raw.close()

    def test_a_newer_store_is_not_moved_aside(self) -> None:
        self.edit("UPDATE meta SET value = '999' WHERE key = 'schema_version';")
        check = doctor._store_checks()[0]
        self.assertFalse(check.ok)
        self.assertIn("schema 999", check.detail)
        self.assertNotIn("Move", check.fix)
        self.assertIn("update", check.fix.lower())
        with self.assertRaises(StoreError) as caught:
            Store.open(self.path)
        self.assertEqual("newer_schema", caught.exception.reason)

    def test_an_older_store_is_left_exactly_as_it_is(self) -> None:
        # A schema 2 store, from before the change triggers and the source computer.
        self.edit("DROP TRIGGER changes_records_insert; DROP TRIGGER changes_records_update; "
                  "DROP TRIGGER changes_records_delete; DELETE FROM meta WHERE key = 'changes.triggers'; "
                  "ALTER TABLE records DROP COLUMN source_computer; "
                  "UPDATE meta SET value = '2' WHERE key = 'schema_version';")
        before = self.path.read_bytes()
        check = doctor._store_checks()[0]
        self.assertTrue(check.ok, check.detail)
        self.assertIn("0 active memories", check.detail)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.path.with_name("knowitall2.schema2-backup.db").exists())
        raw = sqlite3.connect(self.path)
        try:
            self.assertEqual("2", raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
        finally:
            raw.close()

    def test_only_an_unreadable_store_is_moved_aside(self) -> None:
        self.path.write_bytes(b"this is not a database" * 200)
        check = doctor._store_checks()[0]
        self.assertFalse(check.ok)
        self.assertIn("Move", check.fix)
        self.assertIn("server", check.fix)
        self.assertIn("backup", check.fix)
        with self.assertRaises(StoreError) as caught:
            Store.open(self.path)
        self.assertEqual("unreadable", caught.exception.reason)

    def test_each_reason_has_its_own_fix(self) -> None:
        fixes = {reason: doctor._store_fix(reason, self.path)
                 for reason in ("newer_schema", "no_fts5", "busy", "unreadable", "io")}
        self.assertEqual(["unreadable"], [reason for reason, fix in fixes.items() if "Move" in fix])
        self.assertEqual(5, len(set(fixes.values())))

    def test_sqlite_errors_are_sorted_into_reasons(self) -> None:
        from knowitall2.store import error_reason

        def failure(kind, message, code):
            exc = kind(message)
            exc.sqlite_errorcode = code
            return exc

        self.assertEqual("busy", error_reason(failure(sqlite3.OperationalError, "database is locked", sqlite3.SQLITE_BUSY)))
        self.assertEqual("unreadable", error_reason(failure(sqlite3.DatabaseError, "file is not a database",
                                                            sqlite3.SQLITE_NOTADB)))
        self.assertEqual("no_fts5", error_reason(sqlite3.OperationalError("no such module: fts5")))
        self.assertEqual("io", error_reason(failure(sqlite3.OperationalError, "disk I/O error", sqlite3.SQLITE_IOERR)))


if __name__ == "__main__":
    unittest.main()
