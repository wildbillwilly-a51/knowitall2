import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from knowitall2 import __version__, update
from knowitall2.agents import adapter_for, instructions, server_launch


class FakeGit:
    """Answers the update's Git commands from a script."""

    def __init__(self, *, dirty="", pull=(0, "", ""), heads=("aaa", "bbb")) -> None:
        self.dirty, self.pull, self.heads = dirty, pull, list(heads)
        self.commands = []

    def __call__(self, command, **options):
        self.commands.append((command, options))
        if command[0] != "git":
            return subprocess.CompletedProcess(command, 0, "", "")
        verb = command[3]
        if verb == "status":
            return subprocess.CompletedProcess(command, 0, self.dirty, "")
        if verb == "rev-parse":
            return subprocess.CompletedProcess(command, 0, self.heads.pop(0) + "\n", "")
        return subprocess.CompletedProcess(command, self.pull[0], self.pull[1], self.pull[2])


class DownloadTests(unittest.TestCase):
    def run_update(self, git) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(update, "checkout_root", return_value=Path("C:/kia2")):
            code = update.run(runner=git)
        return code, output.getvalue()

    def test_a_download_finishes_with_the_new_code(self) -> None:
        git = FakeGit()
        self.assertEqual(0, self.run_update(git)[0])
        finish, options = git.commands[-1]
        self.assertEqual(["-m", "knowitall2", "update", "--finish", "--previous", __version__], finish[2:8])
        self.assertNotIn("--unchanged", finish)
        self.assertTrue(options["env"]["PYTHONPATH"].startswith(str(Path("C:/kia2") / "src")))
        git = FakeGit(heads=("aaa", "aaa"))
        self.run_update(git)
        self.assertIn("--unchanged", git.commands[-1][0])

    def test_local_changes_or_a_failed_download_change_nothing(self) -> None:
        code, output = self.run_update(FakeGit(dirty=" M src/knowitall2/cli.py\n"))
        self.assertEqual(1, code)
        self.assertIn("has local changes", output)
        code, output = self.run_update(FakeGit(pull=(1, "", "fatal: could not read Username for 'https://x'")))
        self.assertEqual(1, code)
        self.assertIn("could not sign in", output)
        self.assertIn("Nothing was changed.", output)

    def test_a_copy_that_is_not_a_clone_says_how_to_reinstall(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(update, "checkout_root", return_value=None):
            self.assertEqual(1, update.run(runner=FakeGit()))
        self.assertIn("install-for-agents.md", output.getvalue())


class FinishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "codex").mkdir()
        (root / "claude").mkdir()
        (root / "claude" / ".claude.json").write_text(json.dumps({"numStartups": 1}) + "\n", encoding="utf-8")
        self.environment = mock.patch.dict(os.environ, {
            "KNOWITALL2_HOME": str(root / "data"), "CODEX_HOME": str(root / "codex"),
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
        })
        self.environment.start()
        self.root = root
        for name in ("codex", "claude-code"):
            adapter_for(name).setup(server_launch())
        self.patches = [mock.patch("knowitall2.app.shortcut.install", return_value=[]),
                        mock.patch("knowitall2.doctor.run_checks", return_value=[])]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def finish(self, *, codex_running: bool, unchanged: bool = False) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, update.finish(previous="0.2.0", unchanged=unchanged,
                                              codex_running=lambda: codex_running))
        return output.getvalue()

    def outdate_skill(self, folder: Path) -> Path:
        skill = folder / "skills" / "knowitall2" / "SKILL.md"
        skill.write_text("old instructions", encoding="utf-8")
        return skill

    def test_an_up_to_date_install_changes_nothing(self) -> None:
        text = self.finish(codex_running=False, unchanged=True)
        self.assertIn(f"KnowItAll2 is up to date ({__version__})", text)
        self.assertIn("Codex: already up to date", text)
        self.assertIn("Health check: everything is working.", text)
        self.assertNotIn("What to do now", text)
        self.assertTrue(update.launcher_path().is_file())

    def test_instructions_are_refreshed_while_codex_is_open_without_touching_its_settings(self) -> None:
        skill = self.outdate_skill(self.root / "codex")
        config = self.root / "codex" / "config.toml"
        before = config.read_bytes()
        text = self.finish(codex_running=True)
        self.assertNotEqual("old instructions", skill.read_text(encoding="utf-8"))
        self.assertEqual(before, config.read_bytes())
        self.assertIn("Codex: its KnowItAll2 instructions were updated.", text)
        self.assertIn(f"KnowItAll2 was updated from 0.2.0 to {__version__}", text)
        self.assertIn("Quit and reopen Claude Code and Codex once", text)

    def test_the_global_instructions_block_is_refreshed_while_codex_is_open(self) -> None:
        agents_file = self.root / "codex" / "AGENTS.md"
        agents_file.write_text("# Mine\n\n" + instructions.BEGIN + "\nold\n" + instructions.END + "\n", encoding="utf-8")
        claude_file = self.root / "claude" / "CLAUDE.md"
        claude_file.unlink()
        config = self.root / "codex" / "config.toml"
        before = config.read_bytes()
        text = self.finish(codex_running=True)
        self.assertEqual("current", instructions.state(agents_file))
        self.assertTrue(agents_file.read_text(encoding="utf-8").startswith("# Mine\n\n"))
        self.assertEqual(before, config.read_bytes())
        self.assertIn("Codex: its KnowItAll2 instructions were updated.", text)
        self.assertEqual("current", instructions.state(claude_file))
        self.assertNotIn("Quit and reopen Claude Code so it keeps the new settings", text)

    def test_whats_new_is_known_even_when_the_download_came_first(self) -> None:
        text = self.finish(codex_running=False, unchanged=True)
        self.assertIn("up to date", text)
        (update.data_home() / update.VERSION_FILE).write_text("0.2.0\n", encoding="utf-8")
        text = self.finish(codex_running=False, unchanged=True)
        self.assertIn(f"KnowItAll2 was updated from 0.2.0 to {__version__}", text)
        self.assertIn("What's new:", text)
        (update.data_home() / update.VERSION_FILE).unlink()
        output = io.StringIO()
        with redirect_stdout(output):
            update.finish(previous=__version__, unchanged=True, codex_running=lambda: False)
        self.assertIn(f"KnowItAll2 is now version {__version__}.", output.getvalue())
        self.assertEqual(__version__, update.seen_version())

    def test_codex_settings_wait_until_codex_is_closed(self) -> None:
        config = self.root / "codex" / "config.toml"
        before = config.read_text(encoding="utf-8")
        config.write_text(before.replace("knowitall2", "knowitall2-old", 1), encoding="utf-8")
        changed = config.read_text(encoding="utf-8")
        self.outdate_skill(self.root / "codex")
        text = self.finish(codex_running=True)
        self.assertEqual(changed, config.read_text(encoding="utf-8"))
        self.assertIn("its KnowItAll2 instructions were updated", text)
        self.assertIn("Close Codex, then run the update again", text)
        self.assertIn(str(update.launcher_path()), text)

    def test_the_update_script_runs_this_installation(self) -> None:
        text = update.render_launcher()
        self.assertIn('main(["update"])', text)
        compile(text, "update.py", "exec")

    def test_whats_new_comes_from_the_changelog(self) -> None:
        notes = update.changelog_since("0.2.0")
        self.assertTrue(notes and notes[0].startswith(f"{__version__}: "), notes)
        self.assertEqual([], update.changelog_since(__version__))


if __name__ == "__main__":
    unittest.main()
