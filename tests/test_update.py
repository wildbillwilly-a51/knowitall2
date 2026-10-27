import io
import json
import os
import re
import subprocess
import sys
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
        self.assertEqual(["-P", "-m", "knowitall2", "update", "--finish", "--previous", __version__], finish[2:9])
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

    def test_a_new_version_that_does_not_start_is_undone(self) -> None:
        # Review 2026-10-04, U-M4.
        class Broken(FakeGit):
            def __call__(self, command, **options):
                if command[0] != "git" and "--version" in command:
                    self.commands.append((command, options))
                    return subprocess.CompletedProcess(command, 1, "", "SyntaxError: invalid syntax")
                return super().__call__(command, **options)

        git = Broken()
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"KNOWITALL2_HOME": home}):
            code, output = self.run_update(git)
        self.assertEqual(1, code)
        self.assertIn("went back to the version you had", output)
        self.assertIn(["reset", "--hard", "--quiet", "aaa"],
                      [command[3:] for command, _ in git.commands if command[0] == "git"])
        self.assertFalse(any("--finish" in command for command, _ in git.commands))

    def test_one_update_at_a_time(self) -> None:
        from knowitall2.learning.state import RunLock

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"KNOWITALL2_HOME": home}):
            with RunLock(Path(home) / update.UPDATE_LOCK):
                code, output = self.run_update(FakeGit())
        self.assertEqual(1, code)
        self.assertIn("Another KnowItAll2 update is running", output)

    def test_a_copy_that_is_not_a_clone_says_how_to_reinstall(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(update, "checkout_root", return_value=None):
            self.assertEqual(1, update.run(runner=FakeGit()))
        self.assertIn("install-for-agents.md", output.getvalue())


class InterruptedUpdateTests(unittest.TestCase):
    """Review 2026-10-24, F-32: ``git pull`` killed mid-checkout left a half-written folder that every later update
    refused ("has local changes"), with no way out given. Real Git, in a clone of a local repository."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.origin, self.clone = root / "origin", root / "clone"
        self.git(root, "init", "--quiet", "-b", "main", str(self.origin))
        (self.origin / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
        self.commit(self.origin, "one")
        self.git(root, "clone", "--quiet", str(self.origin), str(self.clone))
        (self.origin / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
        (self.origin / "new.py").write_text("NEW = True\n", encoding="utf-8")
        self.newer = self.commit(self.origin, "two")
        self.home = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(root / "home")})
        self.home.start()
        self.addCleanup(self.home.stop)

    def git(self, folder: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@example.com",
                               *arguments], capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, folder: Path, message: str) -> str:
        self.git(folder, "add", "-A")
        self.git(folder, "commit", "--quiet", "-m", message)
        return self.git(folder, "rev-parse", "HEAD")

    def update(self, *, cut_short: str | None = None, broken: bool = False) -> tuple[int, str]:
        """Run the update; ``cut_short`` names the Git verb killed partway (a reset first writes one file);
        ``broken``: the new version does not start, and going back is cut short too."""

        def runner(command, **options):
            if command[0] != "git":
                if broken and "--version" in command:
                    cut.append("reset")  # the next reset, going back, is the one cut short
                    return subprocess.CompletedProcess(command, 1, "", "SyntaxError: invalid syntax")
                return subprocess.CompletedProcess(command, 0, "", "")  # the new code starts, and finishes
            if cut and command[3] == "reset":
                raise subprocess.TimeoutExpired(command, 180)
            if command[3] == cut_short:
                if cut_short == "reset":
                    (self.clone / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
                raise subprocess.TimeoutExpired(command, 180)
            return subprocess.run(command, **options)

        cut: list[str] = []
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(update, "checkout_root", return_value=self.clone):
            code = update.run(runner=runner)
        return code, output.getvalue()

    def test_going_back_cut_short_still_says_how_to_finish(self) -> None:
        # Codex's review of step A: the mark was gone before the new version was tried, and a failed way back
        # was reported as done.
        before = self.git(self.clone, "rev-parse", "HEAD")
        code, output = self.update(broken=True)
        self.assertEqual(1, code)
        self.assertNotIn("went back to the version you had", output)
        self.assertIn(f'git -C "{self.clone}" reset --hard {before}', output)
        self.assertIn(before, (Path(os.environ["KNOWITALL2_HOME"]) / update.UPDATE_MARK).read_text(encoding="utf-8"))

    def test_a_download_cut_short_leaves_the_folder_as_it_was(self) -> None:
        before = self.git(self.clone, "rev-parse", "HEAD")
        self.assertEqual(1, self.update(cut_short="fetch")[0])
        self.assertEqual(before, self.git(self.clone, "rev-parse", "HEAD"))
        self.assertEqual("", self.git(self.clone, "status", "--porcelain"))
        self.assertEqual((0, self.newer), (self.update()[0], self.git(self.clone, "rev-parse", "HEAD")))
        self.assertTrue((self.clone / "new.py").is_file())

    def test_files_replaced_partway_say_how_to_finish_and_that_works(self) -> None:
        code, output = self.update(cut_short="reset")
        self.assertEqual(1, code)
        finish = f'git -C "{self.clone}" reset --hard {self.newer}'
        self.assertIn(finish, output)
        self.assertNotEqual("", self.git(self.clone, "status", "--porcelain"))  # half of each version
        code, output = self.update()  # the next update says the same, not just "local changes"
        self.assertEqual(1, code)
        self.assertIn("cut short", output)
        self.assertIn(finish, output)
        self.git(self.clone, "reset", "--hard", self.newer)  # the command it gave
        self.assertEqual(0, self.update()[0])
        self.assertEqual("VERSION = 2\n", (self.clone / "app.py").read_text(encoding="utf-8"))
        self.assertFalse((Path(os.environ["KNOWITALL2_HOME"]) / update.UPDATE_MARK).exists())

    def test_the_users_own_changes_are_named_with_the_way_back(self) -> None:
        (self.clone / "app.py").write_text("VERSION = 'mine'\n", encoding="utf-8")
        code, output = self.update()
        self.assertEqual(1, code)
        self.assertIn("has local changes (app.py)", output)
        self.assertIn(f'git -C "{self.clone}" reset --hard HEAD', output)
        self.assertEqual("VERSION = 'mine'\n", (self.clone / "app.py").read_text(encoding="utf-8"))

    def test_a_version_that_does_not_follow_this_one_is_not_put_in(self) -> None:
        self.git(self.origin, "reset", "--quiet", "--hard", "HEAD~1")
        (self.origin / "app.py").write_text("VERSION = 'other'\n", encoding="utf-8")
        self.commit(self.origin, "rewritten")
        self.git(self.clone, "pull", "--quiet", "--ff-only")  # as if the rewrite came after this clone
        self.git(self.origin, "reset", "--quiet", "--hard", "HEAD~1")
        (self.origin / "app.py").write_text("VERSION = 'third'\n", encoding="utf-8")
        self.commit(self.origin, "another")
        before = self.git(self.clone, "rev-parse", "HEAD")
        code, output = self.update()
        self.assertEqual(1, code)
        self.assertIn("does not follow", output)
        self.assertEqual(before, self.git(self.clone, "rev-parse", "HEAD"))


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

    def test_hook_launchers_are_refreshed_while_codex_is_open_with_nothing_to_trust_again(self) -> None:
        from knowitall2.agents import claude_code, codex

        launchers = [claude_code.hook_launcher_path(), codex.hook_launcher_path()]
        for launcher in launchers:
            launcher.write_text("# an older launcher\n", encoding="utf-8")
        settings = [self.root / "codex" / "config.toml", self.root / "codex" / "hooks.json",
                    self.root / "claude" / "settings.json"]
        before = [path.read_bytes() for path in settings]
        text = self.finish(codex_running=True)
        self.assertNotIn("# an older launcher", "".join(path.read_text(encoding="utf-8") for path in launchers))
        self.assertTrue(all(check.ok for name in ("codex", "claude-code")
                            for check in adapter_for(name).checks(server_launch())))
        # The launchers are KnowItAll2's own files: the agents' hook commands stay as they were.
        self.assertEqual(before, [path.read_bytes() for path in settings])
        self.assertIn("Codex: updated (refreshed the KnowItAll2 session hook launcher", text)
        self.assertNotIn("Close Codex", text)
        self.assertNotIn(update.TRUST_HOOKS, text)
        self.assertNotIn("so it keeps the new settings", text)

    def test_claude_code_hooks_registered_the_earlier_way_are_registered_again(self) -> None:
        # Up to 0.10.5 the launcher was Python's script, and a missing launcher blocked every prompt (H-H2).
        from knowitall2.agents import claude_code

        settings = self.root / "claude" / "settings.json"
        data = json.loads(settings.read_text(encoding="utf-8"))
        words = {"SessionStart": [], "Stop": ["stop"], "SessionEnd": ["session-end"], "UserPromptSubmit": ["prompt-submit"],
                 "PostToolUse": ["command"], "PostToolUseFailure": ["command"]}
        for event, groups in data["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    hook["args"] = ["-B", str(claude_code.hook_launcher_path()), *words[event]]
        settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        text = self.finish(codex_running=True)
        registered = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
        for event in claude_code.HOOK_EVENTS:
            [hook] = [hook for group in registered[event] for hook in group["hooks"]]
            self.assertEqual(claude_code.hook_entry(server_launch(), event), hook)
            self.assertEqual("-c", hook["args"][2])
        self.assertIn("Quit and reopen Claude Code so it keeps the new settings.", text)

    def test_changed_hooks_are_trusted_again_in_codex(self) -> None:
        (self.root / "codex" / "hooks.json").unlink()
        text = self.finish(codex_running=False)
        self.assertIn("added the KnowItAll2 session hooks", text)
        self.assertIn(update.TRUST_HOOKS, text)

    def test_hooks_no_windows_command_can_run_are_taken_out_with_nothing_to_trust(self) -> None:
        from knowitall2.agents import codex

        hooks = self.root / "codex" / "hooks.json"
        reason = r"C:\Users\R&D\Python312\python.exe contains a character (such as ', &, $ or %)"
        self.assertIn("codex_session_start", hooks.read_text(encoding="utf-8"))
        with mock.patch.object(codex, "hooks_unavailable", return_value=reason):
            # Hooks registered by an earlier version cannot run as they are: the update takes them out.
            text = self.finish(codex_running=False)
            self.assertNotIn("codex_session_start", hooks.read_text(encoding="utf-8") if hooks.exists() else "")
            self.assertIn(f"did not add the KnowItAll2 session hooks: {reason}", text)
            self.assertNotIn(update.TRUST_HOOKS, text)
            # With none registered, a later update that sets Codex up again registers none either.
            self.outdate_skill(self.root / "codex")
            text = self.finish(codex_running=False)
            self.assertIn(f"did not add the KnowItAll2 session hooks: {reason}", text)
            self.assertNotIn(update.TRUST_HOOKS, text)
            self.assertFalse(hooks.exists() and "codex_session_start" in hooks.read_text(encoding="utf-8"))

    def test_the_update_script_runs_this_installation(self) -> None:
        text = update.render_launcher()
        self.assertIn('main(["update"])', text)
        compile(text, "update.py", "exec")

    def run_script(self, *, setup_python: Path, running: str) -> tuple[mock.Mock, mock.Mock, str]:
        """Run the generated update script in this process, as if started by ``running``."""

        with mock.patch("knowitall2.agents.base.console_python", return_value=str(setup_python)):
            update.write_launcher()
        script = update.launcher_path()
        output = io.StringIO()
        path = list(sys.path)
        with redirect_stdout(output), mock.patch.object(sys, "executable", running), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 3)) as relaunch, \
                mock.patch("knowitall2.cli.main", return_value=0) as main, \
                self.assertRaises(SystemExit) as exited:
            exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"),
                 {"__name__": "__main__", "__file__": str(script)})
        sys.path[:] = path
        self.assertIn(exited.exception.code, (0, 3))
        return relaunch, main, output.getvalue()

    def test_the_update_script_runs_with_the_python_it_was_set_up_with(self) -> None:
        setup_python = self.root / "Python 312" / "python.exe"
        setup_python.parent.mkdir()
        setup_python.write_text("", encoding="utf-8")
        relaunch, main, output = self.run_script(setup_python=setup_python, running=str(self.root / "conda.exe"))
        self.assertEqual([str(setup_python), str(update.launcher_path())], relaunch.call_args.args[0][:2])
        main.assert_not_called()
        self.assertIn(f"with {setup_python}, the Python KnowItAll2 was set up with", output)
        # Started by that Python, it runs the update itself.
        relaunch, main, output = self.run_script(setup_python=setup_python, running=str(setup_python))
        relaunch.assert_not_called()
        main.assert_called_once_with(["update"])
        self.assertEqual("", output)

    def test_the_update_script_carries_on_when_that_python_is_gone(self) -> None:
        gone = self.root / "gone" / "python.exe"
        relaunch, main, output = self.run_script(setup_python=gone, running=str(self.root / "conda.exe"))
        relaunch.assert_not_called()
        main.assert_called_once_with(["update"])
        self.assertIn(f"({gone}) is no longer there, so this update uses {self.root / 'conda.exe'}", output)

    def test_the_update_command_names_the_python_it_was_set_up_with(self) -> None:
        with mock.patch("knowitall2.agents.base.console_python", return_value="C:\\Py 312\\python.exe"):
            self.assertIn("Py 312", update.update_command())
            self.assertIn(str(update.launcher_path()), update.update_command())

    def test_whats_new_comes_from_the_changelog(self) -> None:
        notes = update.changelog_since("0.2.0")
        self.assertTrue(notes and notes[0].startswith(f"{__version__}: "), notes)
        self.assertEqual([], update.changelog_since(__version__))

    def test_whats_new_reads_a_heading_wrapped_across_lines(self) -> None:
        # 0.11.0: "Your rules, your preferences, and how your projects work are told apart." wraps onto a second
        # line in CHANGELOG.md, so the update's "What's new" left it out.
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "CHANGELOG.md").write_text(
                "# Changelog\n\n## 9.1.0 (2026-10-14)\n\nIntro.\n\n"
                "- **Learning happens twice.** During a chat, it learns\n  at each commit.\n"
                "- **Your rules, your preferences, and how your projects work are told\n  apart.** A rule needs your "
                "own words.\n- **Less that goes out of date.** Status goes after a week.\n\n"
                "## 9.0.0 (2026-10-12)\n\n- **Older.** Text.\n", encoding="utf-8")
            with mock.patch.object(update, "checkout_root", return_value=Path(folder)):
                self.assertEqual(["9.1.0: Learning happens twice",
                                  "9.1.0: Your rules, your preferences, and how your projects work are told apart",
                                  "9.1.0: Less that goes out of date"], update.changelog_since("9.0.0"))

    def test_an_unreleased_section_is_not_yet_whats_new(self) -> None:
        # Between releases the changelog has an undated "## Unreleased" section; "What's new" lists releases.
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "CHANGELOG.md").write_text(
                "# Changelog\n\n## Unreleased\n\n- **Coming.** Text.\n\n## 9.0.0 (2026-10-12)\n\n- **Older.** Text.\n",
                encoding="utf-8")
            with mock.patch.object(update, "checkout_root", return_value=Path(folder)):
                self.assertEqual(["9.0.0: Older"], update.changelog_since("8.0.0"))
                self.assertEqual([], update.changelog_since(None))

    def test_every_heading_of_the_real_changelog_is_read_whole(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
        released = re.sub(r"^## Unreleased\n.*?(?=^## )", "", text, flags=re.MULTILINE | re.DOTALL)
        headings = re.findall(r"^- \*\*", released, re.MULTILINE)
        notes = update.changelog_since("0.0.0", limit=10_000)
        self.assertEqual(len(headings), len(notes))
        self.assertIn("0.11.0: Your rules, your preferences, and how your projects work are told apart", notes)


if __name__ == "__main__":
    unittest.main()
