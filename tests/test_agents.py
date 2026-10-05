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
from knowitall2.agents.base import JsonFile, render_hook_launcher
from knowitall2.agents.claude_code import ClaudeCodeAdapter, hook_launcher_path
from knowitall2.agents import codex, instructions
from knowitall2.agents.codex import BEGIN, CodexAdapter, windows_command
from knowitall2.memory import Memory
from knowitall2.paths import database_path
from knowitall2.store import Store

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

    def test_a_changed_hook_is_replaced_where_it_is(self) -> None:
        # Codex keys its trust in a hook by its position, so the user's hooks after ours must not move.
        hooks = self.codex_home / "hooks.json"
        before = {"type": "command", "command": "before.exe"}
        after = {"type": "command", "command": "after.exe"}
        self.adapter.setup(LAUNCH)
        data = json.loads(hooks.read_text(encoding="utf-8"))
        for event in ("SessionStart", "Stop"):
            data["hooks"][event] = [{"hooks": [before]}, *data["hooks"][event], {"hooks": [after]}]
        hooks.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        moved = ServerLaunch(command=str(Path(LAUNCH.command).with_name("other-python.exe")), args=LAUNCH.args,
                             env=LAUNCH.env)
        self.adapter.setup(moved)
        data = json.loads(hooks.read_text(encoding="utf-8"))
        for event in ("SessionStart", "Stop"):
            groups = data["hooks"][event]
            self.assertEqual(3, len(groups))
            self.assertEqual([before], groups[0]["hooks"])
            self.assertEqual([codex.hook_entry(moved, event)], groups[1]["hooks"])
            self.assertEqual([after], groups[2]["hooks"])
        self.assertEqual(codex.HOOK_MATCHER, data["hooks"]["SessionStart"][1]["matcher"])

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
        # Letters outside ASCII are plain to both shells, so such a path keeps the text it always had.
        self.assertEqual(
            r"C:\Users\José\Python312\python.exe -B C:\Users\José\.knowitall2\hooks\codex_session_start.py",
            windows_command(r"C:\Users\José\Python312\python.exe",
                            Path(r"C:\Users\José\.knowitall2\hooks\codex_session_start.py")),
        )

    def test_no_windows_command_for_a_path_either_shell_would_misread(self) -> None:
        for name in ("O'Brien", "R&D", "a$b", "x%y%", "O’Brien", "(x86)"):
            with self.subTest(name=name):
                python = rf"C:\Users\{name}\Python312\python.exe"
                launcher = rf"C:\Users\{name}\.knowitall2\hooks\codex_session_start.py"
                self.assertIsNone(windows_command(python, launcher, shorten=lambda path: None))
                # Short names keep these characters, so a folder that also has a space is no better.
                spaced = {rf"C:\Users\{name} X\Python312": rf"C:\Users\{name}X~1\Python312",
                          rf"C:\Users\{name} X\.knowitall2\hooks": rf"C:\Users\{name}X~1\.knowitall2\hooks"}
                self.assertIsNone(windows_command(python.replace(name, name + " X"), launcher.replace(name, name + " X"),
                                                  shorten=spaced.get))
                self.assertIsNone(windows_command(python.replace(name, name + " X"), launcher.replace(name, name + " X"),
                                                  shorten=lambda path: None))

    def test_a_python_under_program_files_x86_gets_its_short_name(self) -> None:
        # Review 2026-10-04, C-M2: the parentheses of the long name are not in its short name.
        shortened = {r"C:\Program Files (x86)\Python312": r"C:\PROGRA~2\Python312"}
        self.assertEqual(
            r"C:\PROGRA~2\Python312\python.exe -B C:\Users\alex\.knowitall2\hooks\codex_session_start.py",
            windows_command(r"C:\Program Files (x86)\Python312\python.exe",
                            r"C:\Users\alex\.knowitall2\hooks\codex_session_start.py", shorten=shortened.get),
        )
        # Without short names, PowerShell's call operator cannot be used safely for it either.
        self.assertIsNone(windows_command(r"C:\Program Files (x86)\Python312\python.exe",
                                          r"C:\Users\alex\.knowitall2\hooks\codex_session_start.py",
                                          shorten=lambda path: None))

    def test_hooks_are_skipped_where_no_windows_command_runs_them(self) -> None:
        unsafe = ServerLaunch(command=r"C:\Users\R&D\Python312\python.exe", args=LAUNCH.args, env=LAUNCH.env)
        hooks = self.codex_home / "hooks.json"

        def hook_check(launch):
            return next(check for check in self.adapter.checks(launch) if check.name == "Codex session hook")

        # Registered first as this platform runs them (taken for Windows, a POSIX path would be refused too),
        # then checked and set up again as on Windows, with a path no Windows command runs.
        self.adapter.setup(LAUNCH)
        self.assertTrue(hooks.is_file())
        with mock.patch.object(codex.sys, "platform", "win32"):
            # Hooks registered before cannot run as they are: doctor says so, and setup takes them out.
            self.assertFalse(hook_check(unsafe).ok)
            changes = self.adapter.setup(unsafe)
            check = hook_check(unsafe)
            hint = CodexAdapter(user_home=self.user_home).restart_hint()
            hint_after_setup = self.adapter.restart_hint()
        self.assertTrue(any("did not add the KnowItAll2 session hooks" in change and "R&D" in change
                            for change in changes), changes)
        self.assertIn("knowitall2", tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"])
        self.assertFalse(hooks.exists())
        self.assertFalse(codex.hook_launcher_path().exists())
        self.assertTrue(check.ok)
        self.assertIn("not added", check.detail)
        self.assertIn("R&D", check.detail)
        self.assertIn("/hooks", hint)
        self.assertNotIn("/hooks", hint_after_setup)

    def test_missing_codex_is_reported_and_not_created(self) -> None:
        adapter = CodexAdapter(user_home=self.user_home / "nobody")
        with self.assertRaises(AgentError):
            adapter.setup(LAUNCH)
        self.assertTrue(adapter.checks(LAUNCH)[0].ok)
        self.assertFalse((self.user_home / "nobody").exists())

    def test_uninstall_cleans_agents_md_after_an_override_appeared(self) -> None:
        self.adapter.setup(LAUNCH)
        override = self.codex_home / "AGENTS.override.md"
        override.write_text("# Override\n", encoding="utf-8")
        self.adapter.uninstall()
        self.assertFalse((self.codex_home / "AGENTS.md").exists())
        self.assertEqual("# Override\n", override.read_text(encoding="utf-8"))

    def test_damaged_instructions_stop_setup_before_any_change(self) -> None:
        agents_md = self.codex_home / "AGENTS.md"
        agents_md.write_text(instructions.END + "\n" + instructions.BEGIN + "\n", encoding="utf-8")
        with self.assertRaises(AgentError):
            self.adapter.setup(LAUNCH)
        self.assertEqual(self.ORIGINAL.encode("utf-8"), self.config.read_bytes())
        self.assertFalse((self.codex_home / "hooks.json").exists())
        self.assertFalse((self.codex_home / "skills" / "knowitall2").exists())
        self.assertFalse(codex.hook_launcher_path().exists())

    def test_a_config_edited_while_setup_runs_keeps_the_edit(self) -> None:
        parse = codex._parse
        edits = []

        def check_then_edit(text, path, *, after_edit=False):
            if after_edit and not edits:
                # Codex saves a setting while setup checks its edit, just before writing it.
                edits.append(True)
                self.config.write_text('approval_policy = "never"\n' + self.ORIGINAL, encoding="utf-8")
            return parse(text, path, after_edit=after_edit)

        with mock.patch.object(codex, "_parse", check_then_edit):
            self.adapter.setup(LAUNCH)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("never", parsed["approval_policy"])
        self.assertIn("knowitall2", parsed["mcp_servers"])


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

    def test_the_launcher_runs_every_hook_for_a_project_with_a_non_ascii_name(self) -> None:
        launch = server_launch()  # the real interpreter and this checkout's src
        self.adapter.setup(launch)
        project = make_repository(self.user_home / "alphá")
        store = Store.open(database_path())
        Memory(store, agent="cli").remember("Deploy alpha with make ship.", kind="rule", scope="project", source="user",
                                            project_path=project)
        store.close()
        registered = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]
        # Agents send UTF-8 JSON, while Windows gives a hook's stdin the ANSI code page: the same here everywhere.
        environment = {name: value for name, value in os.environ.items() if name != "PYTHONUTF8"}
        environment.update({"PYTHONIOENCODING": "cp1252", "CLAUDE_CONFIG_DIR": str(self.user_home / ".claude")})
        outputs = {}
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            [hook] = registered[event][0]["hooks"]
            payload = {"hook_event_name": event, "cwd": str(project), "session_id": f"s-{event}",
                       "transcript_path": str(project / "session.jsonl")}
            done = subprocess.run([hook["command"], *hook["args"]], input=json.dumps(payload, ensure_ascii=False).encode(),
                                  capture_output=True, env=environment, timeout=60)
            self.assertEqual(0, done.returncode, f"{event}: {done.stderr}")
            outputs[event] = done.stdout
        context = json.loads(outputs["SessionStart"])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("briefing for project alphá", context)
        self.assertIn("Deploy alpha with make ship.", context)
        # A chat new to KnowItAll2 gets its project's contents with its first message.
        context = json.loads(outputs["UserPromptSubmit"])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("table of contents for project alphá", context)
        self.assertIn("Deploy alpha with make ship.", context)
        # With learning off, the end of a turn and of the session say nothing.
        self.assertEqual((b"", b""), (outputs["Stop"], outputs["SessionEnd"]))

    def test_the_launcher_exits_zero_whatever_the_hook_raises(self) -> None:
        fake = self.user_home / "fake"
        (fake / "knowitall2").mkdir(parents=True)
        (fake / "knowitall2" / "__init__.py").write_text("", encoding="utf-8")
        (fake / "knowitall2" / "hooks.py").write_text(
            "import os\nimport sys\n\n\ndef main(arguments):\n"
            "    sys.stdout.write('the briefing')\n    sys.stdout.flush()\n"
            "    raise {'error': RuntimeError('broken'), 'exit': SystemExit(3), 'interrupt': KeyboardInterrupt()}"
            "[os.environ['FAILURE']]\n", encoding="utf-8")
        launcher = self.user_home / "hook.py"
        launcher.write_text(render_hook_launcher(ServerLaunch(sys.executable, (), {"PYTHONPATH": str(fake)}),
                                                 "claude-code"), encoding="utf-8")
        for failure in ("error", "exit", "interrupt"):
            with self.subTest(failure=failure):
                done = subprocess.run([sys.executable, "-B", str(launcher), "stop"], input=b"{}", capture_output=True,
                                      env=dict(os.environ, FAILURE=failure), timeout=60)
                self.assertEqual((0, b"the briefing"), (done.returncode, done.stdout), done.stderr)

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

    def test_setup_and_uninstall_keep_each_files_formatting(self) -> None:
        # Windows line endings, a 4-space indent and escaped non-ASCII text, as other tools may write them.
        self.config.write_bytes(b'{\r\n    "numStartups": 1,\r\n    "name": "Jos\\u00e9"\r\n}\r\n')
        self.settings.write_bytes(b'{\r\n    "model": "opus",\r\n    "hooks": {}\r\n}\r\n')
        self.adapter.setup(LAUNCH)
        config = self.config.read_bytes()
        self.assertNotIn(b"\n", config.replace(b"\r\n", b""))
        self.assertIn(b'\r\n    "name": "Jos\\u00e9",\r\n    "mcpServers": {\r\n        "knowitall2": {\r\n', config)
        settings = self.settings.read_bytes()
        self.assertNotIn(b"\n", settings.replace(b"\r\n", b""))
        self.assertIn(b'\r\n    "hooks": {\r\n        "SessionStart": [\r\n', settings)
        self.adapter.uninstall()
        # Uninstall cannot know that "mcpServers" was missing and "hooks" was empty before setup;
        # every other byte is as it was.
        self.assertEqual(b'{\r\n    "numStartups": 1,\r\n    "name": "Jos\\u00e9",\r\n    "mcpServers": {}\r\n}\r\n',
                         self.config.read_bytes())
        self.assertEqual(b'{\r\n    "model": "opus"\r\n}\r\n', self.settings.read_bytes())

    def test_damaged_instructions_stop_setup_before_any_change(self) -> None:
        claude_md = self.user_home / ".claude" / "CLAUDE.md"
        claude_md.write_text(instructions.END + "\n" + instructions.BEGIN + "\n", encoding="utf-8")
        before = self.config.read_bytes()
        with self.assertRaises(AgentError):
            self.adapter.setup(LAUNCH)
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse(self.settings.exists())
        self.assertFalse((self.user_home / ".claude" / "skills" / "knowitall2").exists())
        self.assertFalse(hook_launcher_path().exists())
        # An instructions file that is not UTF-8 cannot be edited safely either.
        claude_md.write_bytes(b"\xff\xfe# not UTF-8\n")
        with self.assertRaises(AgentError):
            self.adapter.setup(LAUNCH)
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse(self.settings.exists())


class JsonFileTests(unittest.TestCase):
    """A rewritten settings file keeps the style it was written in."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "settings.json"
        self.file = JsonFile(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def rewrite(self, original: bytes, **added) -> bytes:
        self.path.write_bytes(original)
        text, data = self.file.load()
        self.assertTrue(self.file.write_if_unchanged(text, {**data, **added}))
        return self.path.read_bytes()

    def test_tabs_and_literal_non_ascii_text_are_kept(self) -> None:
        self.assertEqual('{\n\t"name": "José",\n\t"path": "C:\\\\Usuários"\n}\n'.encode("utf-8"),
                         self.rewrite('{\n\t"name": "José"\n}\n'.encode("utf-8"), path="C:\\Usuários"))

    def test_escaped_non_ascii_text_stays_escaped(self) -> None:
        self.assertEqual(b'{\n  "name": "Jos\\u00e9",\n  "path": "C:\\\\Usu\\u00e1rios"\n}',
                         self.rewrite(b'{\n  "name": "Jos\\u00e9"\n}', path="C:\\Usuários"))

    def test_a_one_line_file_stays_on_one_line(self) -> None:
        self.assertEqual(b'{"a": 1, "b": 2}\n', self.rewrite(b'{"a": 1}\n', b=2))
        self.assertEqual(b'{"a":1,"b":2}', self.rewrite(b'{"a":1}', b=2))

    def test_a_new_or_empty_file_gets_two_space_indent(self) -> None:
        self.assertEqual(b'{\n  "a": 1\n}\n', self.rewrite(b"{}\n", a=1))
        self.path.unlink()
        self.assertTrue(self.file.write_if_unchanged("", {"a": 1}))
        self.assertEqual(b'{\n  "a": 1\n}\n', self.path.read_bytes())


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

    def test_a_file_edited_while_the_block_is_added_keeps_the_edit(self) -> None:
        self.path.write_text(self.MINE, encoding="utf-8")
        read = instructions._read
        edits = []

        def read_then_edit(path):
            text = read(path)
            if not edits:
                edits.append(True)
                self.path.write_text(self.MINE + "- Be kind.\n", encoding="utf-8")
            return text

        with mock.patch.object(instructions, "_read", read_then_edit):
            instructions.install(self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(self.MINE + "- Be kind.\n"))
        self.assertEqual("current", instructions.state(self.path))

    def test_codex_uses_its_override_file_when_there_is_one(self) -> None:
        home = Path(self.temporary.name)
        (home / ".codex").mkdir()
        adapter = CodexAdapter(user_home=home)
        self.assertEqual(home / ".codex" / "AGENTS.md", adapter.instructions_path)
        (home / ".codex" / "AGENTS.override.md").write_text("# Override\n", encoding="utf-8")
        self.assertEqual(home / ".codex" / "AGENTS.override.md", adapter.instructions_path)


if __name__ == "__main__":
    unittest.main()
