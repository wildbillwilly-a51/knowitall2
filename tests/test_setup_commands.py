import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from knowitall2 import doctor
from knowitall2.agents import adapter_for, claude_code, codex, instructions, server_launch
from knowitall2.agents.base import cli_command, render_hook_launcher
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


class UninstallTests(unittest.TestCase):
    """Uninstall changes only the agent it names, and leaves the data home alone when testing against another home."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.claude, self.codex, self.data = root / "claude", root / "codex", root / "data"
        self.test_home = root / "test home"
        for folder in (self.claude, self.codex, self.test_home / ".claude"):
            folder.mkdir(parents=True)
        for config in (self.claude / ".claude.json", self.test_home / ".claude.json"):
            config.write_text(json.dumps({"numStartups": 1}) + "\n", encoding="utf-8")
        self.environment = mock.patch.dict(os.environ, {
            "KNOWITALL2_HOME": str(self.data), "CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(list(arguments))
        return code, output.getvalue() + errors.getvalue()

    def run_hook(self, launcher: Path, event: str) -> subprocess.CompletedProcess:
        payload = json.dumps({"session_id": "s1", "cwd": str(self.test_home), "prompt": "hello"}).encode()
        return subprocess.run([sys.executable, "-B", str(launcher), event], input=payload, capture_output=True,
                              timeout=60)

    def test_uninstall_against_another_home_leaves_the_data_home_alone(self) -> None:
        # Review 2026-10-11, A-M4: the real agent's launcher, its server key and the connection stay.
        self.assertEqual(0, self.run_cli("setup", "claude-code", "--no-app-shortcut")[0])
        self.assertEqual(0, self.run_cli("setup", "claude-code", "--user-home", str(self.test_home))[0])
        keys = self.data / "server" / "keys"
        keys.mkdir(parents=True)
        connection = self.data / "server" / "connection.json"
        connection.write_text(json.dumps({"address": "http://127.0.0.1:9"}), encoding="utf-8")
        for agent in ("claude-code", "codex"):
            (keys / f"{agent}.key").write_text("test-key\n", encoding="utf-8")
        launcher = claude_code.hook_launcher_path()
        before = launcher.read_bytes()
        for _ in range(2):  # a test home with KnowItAll2's settings, then one without
            code, output = self.run_cli("uninstall", "claude-code", "--user-home", str(self.test_home))
            self.assertEqual(0, code, output)
            self.assertEqual(before, launcher.read_bytes())
            self.assertEqual(["claude-code.key", "codex.key"], sorted(path.name for path in keys.iterdir()))
            self.assertTrue(connection.is_file())
            self.assertNotIn("for the KnowItAll2 server", output)
        self.assertTrue(all(check.ok for check in adapter_for("claude-code").checks(server_launch())))

    def test_codex_leaves_a_launcher_that_does_nothing_and_claude_code_keeps_its_own(self) -> None:
        for agent in ("codex", "claude-code"):
            self.assertEqual(0, self.run_cli("setup", agent, "--no-app-shortcut")[0])
        code, output = self.run_cli("uninstall", "codex")
        self.assertEqual(0, code, output)
        # A Codex chat open since before keeps its hooks until Codex reloads them, and its Windows command
        # cannot test for a missing file: the launcher stays, and does nothing.
        stub = codex.hook_launcher_path()
        self.assertNotIn("knowitall2", stub.read_text(encoding="utf-8").split('"""')[-1])
        for event in ("session-start", "stop", "prompt-submit"):
            done = self.run_hook(stub, event)
            self.assertEqual((0, b"", b""), (done.returncode, done.stdout, done.stderr))
        self.assertEqual(render_hook_launcher(server_launch(), "claude-code"),
                         claude_code.hook_launcher_path().read_text(encoding="utf-8"))
        self.assertIn(b"KnowItAll2 briefing", self.run_hook(claude_code.hook_launcher_path(), "session-start").stdout)
        code, output = self.run_cli("uninstall", "claude-code")
        self.assertEqual(0, code, output)
        self.assertFalse(claude_code.hook_launcher_path().exists())
        # Set up again, Codex gets its launcher back, and uninstalling Claude Code leaves it alone.
        for agent in ("codex", "claude-code"):
            self.assertEqual(0, self.run_cli("setup", agent, "--no-app-shortcut")[0])
        self.assertEqual(render_hook_launcher(server_launch(), "codex"), stub.read_text(encoding="utf-8"))
        self.assertEqual(0, self.run_cli("uninstall", "claude-code")[0])
        self.assertEqual(render_hook_launcher(server_launch(), "codex"), stub.read_text(encoding="utf-8"))

    def test_the_folder_is_offered_for_deletion_only_when_no_agent_uses_it(self) -> None:
        for agent in ("codex", "claude-code"):
            self.assertEqual(0, self.run_cli("setup", agent, "--no-app-shortcut")[0])
        code, output = self.run_cli("uninstall", "codex")
        self.assertEqual(0, code, output)
        self.assertNotIn("delete that folder", output)
        self.assertIn("Claude Code still uses it", output)
        self.assertIn("Close and reopen Codex", output)
        # A clone has no knowitall2 program on PATH: the advice is the command that runs this installation.
        self.assertIn(cli_command("app", "--remove-shortcut"), output)
        code, output = self.run_cli("uninstall", "claude-code")
        self.assertEqual(0, code, output)
        self.assertIn(f"Your memories were kept in {self.data}; delete that folder to remove them.", output)
        self.assertIn("Close and reopen Claude Code", output)
        # Testing against another home, the agents set up here count too.
        self.assertEqual(0, self.run_cli("setup", "codex", "--no-app-shortcut")[0])
        self.assertEqual(0, self.run_cli("setup", "claude-code", "--user-home", str(self.test_home))[0])
        code, output = self.run_cli("uninstall", "claude-code", "--user-home", str(self.test_home))
        self.assertEqual(0, code, output)
        self.assertNotIn("delete that folder", output)
        self.assertIn("Codex still uses it", output)


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
