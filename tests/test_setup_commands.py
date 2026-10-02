import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from knowitall2.cli import main


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

    def tearDown(self) -> None:
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

        code, output, _ = self.run_cli("doctor", "--agent", "codex")
        self.assertEqual(1, code)
        self.assertIn("FAIL Codex registration: KnowItAll2 is not registered", output)
        self.assertIn("fix: Run: knowitall2 setup codex", output)

    def test_setup_errors_are_reported_without_a_traceback(self) -> None:
        (self.user_home / ".claude.json").write_text("{broken", encoding="utf-8")
        code, output, errors = self.run_cli("setup", "claude-code")
        self.assertEqual(1, code)
        self.assertEqual("", output)
        self.assertIn("is not valid JSON", errors)


if __name__ == "__main__":
    unittest.main()
