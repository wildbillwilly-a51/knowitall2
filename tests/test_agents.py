import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from _support import make_repository

from knowitall2.agents import AgentError, ServerLaunch, server_launch
from knowitall2.agents.claude_code import ClaudeCodeAdapter, hook_launcher_path
from knowitall2.agents import codex, instructions
from knowitall2.agents.codex import BEGIN, CodexAdapter, windows_command

LAUNCH = ServerLaunch(command=sys.executable, args=("-B", "-m", "knowitall2", "serve"), env={"PYTHONPATH": "C:\\src dir"})


class CodexAdapterTests(unittest.TestCase):
    ORIGINAL = 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other.exe"\nargs = ["--flag"]\n'

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.user_home = Path(self.temporary.name)
        self.codex_home = self.user_home / ".codex"
        self.codex_home.mkdir()
        self.config = self.codex_home / "config.toml"
        self.config.write_bytes(self.ORIGINAL.encode("utf-8"))
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.user_home / "data")})
        self.environment.start()
        self.adapter = CodexAdapter(user_home=self.user_home)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_setup_adds_a_valid_block_keeps_the_rest_and_is_idempotent(self) -> None:
        self.assertEqual(4, len(self.adapter.setup(LAUNCH)))
        self.assertIn("## KnowItAll2 memory", (self.codex_home / "AGENTS.md").read_text(encoding="utf-8"))
        text = self.config.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(self.ORIGINAL))
        servers = tomllib.loads(text)["mcp_servers"]
        self.assertEqual(["--flag"], servers["other"]["args"])
        self.assertEqual(LAUNCH.command, servers["knowitall2"]["command"])
        self.assertEqual({"PYTHONPATH": "C:\\src dir"}, servers["knowitall2"]["env"])
        self.assertTrue((self.codex_home / "skills" / "knowitall2" / "SKILL.md").is_file())
        self.assertEqual([], self.adapter.setup(LAUNCH))
        self.assertTrue(all(check.ok for check in self.adapter.checks(LAUNCH)))

    def test_a_changed_launch_is_detected_and_replaced_in_place(self) -> None:
        self.adapter.setup(LAUNCH)
        moved = ServerLaunch(command=LAUNCH.command, args=LAUNCH.args, env={})
        self.assertFalse(self.adapter.checks(moved)[0].ok)
        self.adapter.setup(moved)
        text = self.config.read_text(encoding="utf-8")
        self.assertEqual(1, text.count(BEGIN))
        self.assertNotIn("PYTHONPATH", text)

    def test_uninstall_restores_the_original_bytes(self) -> None:
        self.adapter.setup(LAUNCH)
        self.assertEqual(4, len(self.adapter.uninstall()))
        self.assertEqual(self.ORIGINAL.encode("utf-8"), self.config.read_bytes())
        self.assertFalse((self.codex_home / "AGENTS.md").exists())
        self.assertFalse((self.codex_home / "skills" / "knowitall2").exists())
        self.assertFalse((self.codex_home / "hooks.json").exists())
        self.assertFalse(codex.hook_launcher_path().exists())
        self.assertEqual([], self.adapter.uninstall())

    def test_windows_line_endings_are_kept(self) -> None:
        crlf = self.ORIGINAL.replace("\n", "\r\n").encode("utf-8")
        self.config.write_bytes(crlf)
        self.adapter.setup(LAUNCH)
        self.assertNotIn(b"\n", self.config.read_bytes().replace(b"\r\n", b""))
        self.adapter.uninstall()
        self.assertEqual(crlf, self.config.read_bytes())

    def test_refuses_an_entry_it_did_not_create(self) -> None:
        foreign = self.ORIGINAL + '\n[mcp_servers.knowitall2]\ncommand = "mine.exe"\n'
        self.config.write_text(foreign, encoding="utf-8")
        with self.assertRaises(AgentError):
            self.adapter.setup(LAUNCH)
        self.assertEqual(foreign, self.config.read_text(encoding="utf-8"))

    def test_refuses_invalid_toml_without_changing_it(self) -> None:
        self.config.write_text("model = \n", encoding="utf-8")
        with self.assertRaises(AgentError):
            self.adapter.setup(LAUNCH)
        self.assertEqual("model = \n", self.config.read_text(encoding="utf-8"))

    def test_a_foreign_skill_folder_blocks_setup_before_any_change(self) -> None:
        foreign = self.codex_home / "skills" / "knowitall2"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("mine", encoding="utf-8")
        with self.assertRaises(AgentError):
            self.adapter.setup(LAUNCH)
        self.assertEqual(self.ORIGINAL.encode("utf-8"), self.config.read_bytes())
        self.adapter.uninstall()
        self.assertEqual("mine", (foreign / "SKILL.md").read_text(encoding="utf-8"))

    def test_a_config_created_by_setup_is_removed_by_uninstall(self) -> None:
        self.config.unlink()
        self.adapter.setup(LAUNCH)
        self.assertTrue(self.config.is_file())
        self.adapter.uninstall()
        self.assertFalse(self.config.exists())

    def test_the_session_hook_keeps_other_hooks_and_is_checked(self) -> None:
        hooks = self.codex_home / "hooks.json"
        other = {"type": "command", "command": "echo hi"}
        original = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [other]}], "Stop": [{"hooks": [other]}]}}
        hooks.write_text(json.dumps(original), encoding="utf-8")
        self.adapter.setup(LAUNCH)
        groups = json.loads(hooks.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
        self.assertEqual([other], groups[0]["hooks"])
        self.assertEqual("startup|resume|clear|compact", groups[1]["matcher"])
        [entry] = groups[1]["hooks"]
        self.assertIn("codex_session_start.py", entry["command"])
        if sys.platform == "win32":
            self.assertIn("codex_session_start.py", entry["commandWindows"])
        else:
            self.assertNotIn("commandWindows", entry)
        stop = json.loads(hooks.read_text(encoding="utf-8"))["hooks"]["Stop"]
        self.assertEqual([other], stop[0]["hooks"])
        [ours] = stop[1]["hooks"]
        self.assertIn("codex_session_start.py", ours["command"])
        self.assertTrue(ours["command"].endswith(" stop"))
        self.assertNotIn("statusMessage", ours)
        launcher = codex.hook_launcher_path()
        self.assertIn('main(["codex", sys.argv[1] if len(sys.argv) > 1 else "session-start"])',
                      launcher.read_text(encoding="utf-8"))

        def hook_check():
            return [check for check in self.adapter.checks(LAUNCH) if check.name == "Codex session hook"][0]

        self.assertTrue(hook_check().ok, hook_check().detail)
        self.assertIn("/hooks", hook_check().detail)
        launcher.unlink()
        self.assertFalse(hook_check().ok)
        self.adapter.uninstall()
        self.assertEqual(original, json.loads(hooks.read_text(encoding="utf-8")))

    def test_settings_codex_adds_inside_the_block_are_kept(self) -> None:
        self.adapter.setup(LAUNCH)
        key = str(self.codex_home / "hooks.json") + ":session_start:0:0"
        trust = f"[hooks.state.'{key}']\ntrusted_hash = \"sha256:abc\"\n"
        text = self.config.read_text(encoding="utf-8")
        self.config.write_text(text.replace("# END knowitall2", "\n" + trust + "# END knowitall2"), encoding="utf-8")
        hook_check = [check for check in self.adapter.checks(LAUNCH) if check.name == "Codex session hook"][0]
        # Until the end-of-turn and message hooks are trusted too, doctor says so.
        self.assertIn("runs the end-of-turn and message hooks once you trust them", hook_check.detail)
        for name in ("stop", "user_prompt_submit"):
            other_key = str(self.codex_home / "hooks.json") + f":{name}:0:0"
            trust += f"[hooks.state.'{other_key}']\ntrusted_hash = \"sha256:def\"\n"
        # Codex appends new tables before the file's last comment, which is our end marker.
        self.config.write_text(text.replace("# END knowitall2", "\n" + trust + "# END knowitall2"), encoding="utf-8")
        hook_check = [check for check in self.adapter.checks(LAUNCH) if check.name == "Codex session hook"][0]
        self.assertIn("trusted in Codex", hook_check.detail)
        moved = ServerLaunch(command=LAUNCH.command, args=LAUNCH.args, env={})
        self.adapter.setup(moved)
        after = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("sha256:abc", after["hooks"]["state"][key]["trusted_hash"])
        self.assertNotIn("env", after["mcp_servers"]["knowitall2"])
        block_end = self.config.read_text(encoding="utf-8").index("# END knowitall2")
        self.assertGreater(self.config.read_text(encoding="utf-8").index("[hooks.state"), block_end)
        self.adapter.uninstall()
        remaining = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("sha256:abc", remaining["hooks"]["state"][key]["trusted_hash"])
        self.assertNotIn("knowitall2", remaining["mcp_servers"])

    def test_the_windows_command_needs_no_quotes(self) -> None:
        python = r"C:\Users\Jane Doe\Python312\python.exe"
        launcher = Path(r"C:\Users\Jane Doe\.knowitall2\hooks\codex_session_start.py")
        short = {
            r"C:\Users\Jane Doe\Python312": r"C:\Users\JANEDO~1\Python312",
            r"C:\Users\Jane Doe\.knowitall2\hooks": r"C:\Users\JANEDO~1\.knowitall2\hooks",
        }
        self.assertEqual(
            r"C:\Users\JANEDO~1\Python312\python.exe -B C:\Users\JANEDO~1\.knowitall2\hooks\codex_session_start.py",
            windows_command(python, launcher, shorten=short.get),
        )
        self.assertEqual(
            r"& 'C:\Users\Jane Doe\Python312\python.exe' -B 'C:\Users\Jane Doe\.knowitall2\hooks\codex_session_start.py'",
            windows_command(python, launcher, shorten=lambda path: None),
        )
        self.assertEqual(
            r"C:\Python\python.exe -B C:\kia\hook.py", windows_command(r"C:\Python\python.exe", Path(r"C:\kia\hook.py")),
        )

    def test_missing_codex_is_reported_and_not_created(self) -> None:
        adapter = CodexAdapter(user_home=self.user_home / "nobody")
        with self.assertRaises(AgentError):
            adapter.setup(LAUNCH)
        self.assertTrue(adapter.checks(LAUNCH)[0].ok)
        self.assertFalse((self.user_home / "nobody").exists())


class ClaudeCodeAdapterTests(unittest.TestCase):
    ORIGINAL = {
        "numStartups": 3,
        "projects": {"C:/work": {"allowedTools": []}},
        "mcpServers": {"other": {"type": "stdio", "command": "other.exe", "args": []}},
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.user_home = Path(self.temporary.name)
        (self.user_home / ".claude").mkdir()
        self.config = self.user_home / ".claude.json"
        self.settings = self.user_home / ".claude" / "settings.json"
        self.config.write_text(json.dumps(self.ORIGINAL, indent=2) + "\n", encoding="utf-8")
        # The hook launcher lives in the data home; keep it out of the real one.
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.user_home / "data")})
        self.environment.start()
        self.adapter = ClaudeCodeAdapter(user_home=self.user_home)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def read(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def test_setup_adds_only_its_entry_and_is_idempotent(self) -> None:
        self.assertEqual(4, len(self.adapter.setup(LAUNCH)))
        self.assertIn("## KnowItAll2 memory", (self.user_home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"))
        data = self.read()
        self.assertEqual(self.ORIGINAL["projects"], data["projects"])
        self.assertEqual(self.ORIGINAL["mcpServers"]["other"], data["mcpServers"]["other"])
        self.assertEqual(
            {"type": "stdio", "command": LAUNCH.command, "args": list(LAUNCH.args), "env": LAUNCH.env},
            data["mcpServers"]["knowitall2"],
        )
        self.assertTrue((self.user_home / ".claude" / "skills" / "knowitall2" / "SKILL.md").is_file())
        self.assertEqual([], self.adapter.setup(LAUNCH))
        self.assertTrue(all(check.ok for check in self.adapter.checks(LAUNCH)))

    def test_uninstall_removes_only_its_entry(self) -> None:
        self.adapter.setup(LAUNCH)
        self.assertEqual(4, len(self.adapter.uninstall()))
        self.assertEqual(self.ORIGINAL, self.read())
        self.assertFalse((self.user_home / ".claude" / "CLAUDE.md").exists())
        self.assertFalse((self.user_home / ".claude" / "skills" / "knowitall2").exists())
        self.assertFalse(self.settings.exists())
        self.assertFalse(hook_launcher_path().exists())

    def test_the_session_hook_is_exec_form_and_keeps_other_hooks(self) -> None:
        other_start = {"matcher": "startup", "hooks": [{"type": "command", "command": "other.exe"}]}
        guard = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.exe"}]}]
        original = {"switchModelsOnFlag": True, "hooks": {"SessionStart": [other_start], "PreToolUse": guard}}
        self.settings.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
        self.adapter.setup(LAUNCH)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(other_start, data["hooks"]["SessionStart"][0])
        ours = data["hooks"]["SessionStart"][1]["hooks"][0]
        self.assertEqual((LAUNCH.command, "-B"), (ours["command"], ours["args"][0]))
        self.assertEqual(str(hook_launcher_path()), ours["args"][1])
        self.assertNotIn("shell", ours)
        # The end of each turn and the end of the session run the same launcher with their event.
        self.assertEqual([str(hook_launcher_path()), "stop"], data["hooks"]["Stop"][0]["hooks"][0]["args"][1:])
        self.assertEqual([str(hook_launcher_path()), "session-end"],
                         data["hooks"]["SessionEnd"][0]["hooks"][0]["args"][1:])
        self.assertEqual([str(hook_launcher_path()), "prompt-submit"],
                         data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["args"][1:])
        self.assertEqual(guard, data["hooks"]["PreToolUse"])
        self.assertTrue(data["switchModelsOnFlag"])
        self.assertIn(repr("C:\\src dir"), hook_launcher_path().read_text(encoding="utf-8"))
        self.adapter.uninstall()
        self.assertEqual(original, json.loads(self.settings.read_text(encoding="utf-8")))

    def test_a_removed_hook_is_detected(self) -> None:
        self.adapter.setup(LAUNCH)
        self.settings.unlink()
        hook = next(check for check in self.adapter.checks(LAUNCH) if check.name == "Claude Code session hook")
        self.assertFalse(hook.ok)
        self.assertIn("knowitall2 setup claude-code", hook.fix)

    def test_the_launcher_runs_the_hook(self) -> None:
        launch = server_launch()  # the real interpreter and this checkout's src
        self.adapter.setup(launch)
        project = make_repository(self.user_home / "alpha", "https://gitlab.example.com/team/alpha.git")
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": str(project), "session_id": "s1"})
        done = subprocess.run(
            [launch.command, "-B", str(hook_launcher_path())], input=payload.encode(), capture_output=True, timeout=60,
        )
        self.assertEqual(0, done.returncode, done.stderr)
        context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("briefing for project alpha", context)

    def test_a_registration_lost_to_an_overwrite_is_detected(self) -> None:
        self.adapter.setup(LAUNCH)
        self.config.write_text(json.dumps(self.ORIGINAL, indent=2) + "\n", encoding="utf-8")
        registration = self.adapter.checks(LAUNCH)[0]
        self.assertFalse(registration.ok)
        self.assertIn("knowitall2 setup claude-code", registration.fix)

    def test_refuses_a_foreign_entry_and_invalid_json(self) -> None:
        foreign = dict(self.ORIGINAL, mcpServers={"knowitall2": {"type": "stdio", "command": "x.exe", "args": []}})
        self.config.write_text(json.dumps(foreign), encoding="utf-8")
        with self.assertRaises(AgentError):
            self.adapter.setup(LAUNCH)
        self.config.write_text("{not json", encoding="utf-8")
        with self.assertRaises(AgentError):
            self.adapter.setup(LAUNCH)
        self.assertEqual("{not json", self.config.read_text(encoding="utf-8"))

    def test_needs_claude_code_to_have_run_once(self) -> None:
        self.config.unlink()
        with self.assertRaises(AgentError) as caught:
            self.adapter.setup(LAUNCH)
        self.assertIn("Open Claude Code once", str(caught.exception))
        self.assertFalse(self.config.exists())


class InstructionsTests(unittest.TestCase):
    """KnowItAll2's block in an agent's global instructions changes nothing else in the file."""

    MINE = "# My rules\n\n- Be brief.\n"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "AGENTS.md"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_the_block_follows_the_users_text_and_leaves_it_on_removal(self) -> None:
        self.path.write_bytes(self.MINE.encode("utf-8"))
        self.assertIn("added", instructions.install(self.path))
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(self.MINE + "\n" + instructions.BEGIN))
        self.assertEqual("current", instructions.state(self.path))
        self.assertIsNone(instructions.install(self.path))
        self.assertIn("removed", instructions.remove(self.path))
        self.assertEqual(self.MINE.encode("utf-8"), self.path.read_bytes())
        self.assertIsNone(instructions.remove(self.path))

    def test_line_endings_and_a_byte_order_mark_are_kept(self) -> None:
        original = ("\ufeff" + self.MINE.replace("\n", "\r\n")).encode("utf-8")
        self.path.write_bytes(original)
        instructions.install(self.path)
        written = self.path.read_bytes()
        self.assertTrue(written.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", written.replace(b"\r\n", b""))
        instructions.remove(self.path)
        self.assertEqual(original, self.path.read_bytes())

    def test_an_outdated_block_is_replaced_in_place(self) -> None:
        self.path.write_text(self.MINE + "\n" + instructions.BEGIN + "\nold words\n" + instructions.END + "\n\n## Later\n",
                             encoding="utf-8")
        self.assertEqual("outdated", instructions.state(self.path))
        self.assertIn("updated", instructions.install(self.path))
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("old words", text)
        self.assertTrue(text.endswith(instructions.END + "\n\n## Later\n"))

    def test_a_file_that_held_only_the_block_is_removed(self) -> None:
        instructions.install(self.path)
        self.assertEqual(instructions.render() + "\n", self.path.read_text(encoding="utf-8"))
        instructions.remove(self.path)
        self.assertFalse(self.path.exists())

    def test_damaged_markers_are_left_for_the_user(self) -> None:
        damaged = self.MINE + instructions.BEGIN + "\n"
        self.path.write_text(damaged, encoding="utf-8")
        with self.assertRaises(AgentError):
            instructions.install(self.path)
        self.assertEqual(damaged, self.path.read_text(encoding="utf-8"))
        self.assertEqual("damaged", instructions.state(self.path))
        self.assertFalse(instructions.check("Codex", self.path, "Run: knowitall2 setup codex").ok)

    def test_codex_uses_its_override_file_when_there_is_one(self) -> None:
        home = Path(self.temporary.name)
        (home / ".codex").mkdir()
        adapter = CodexAdapter(user_home=home)
        self.assertEqual(home / ".codex" / "AGENTS.md", adapter.instructions_path)
        (home / ".codex" / "AGENTS.override.md").write_text("# Override\n", encoding="utf-8")
        self.assertEqual(home / ".codex" / "AGENTS.override.md", adapter.instructions_path)


if __name__ == "__main__":
    unittest.main()
