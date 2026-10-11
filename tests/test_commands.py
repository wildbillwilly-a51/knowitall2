"""What KnowItAll2 knows about a command, given to the agent when that command fails."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from _support import SOURCE_ROOT, Clock, make_repository

from knowitall2 import commands, known
from knowitall2.agents.base import ServerLaunch, render_hook_launcher
from knowitall2.learning import command_tags
from knowitall2.learning.state import LearnerState
from knowitall2.memory import Memory
from knowitall2.store import Store

QUOTING = ("Python heredocs run through the Bash tool on Windows mangle backslashes in string literals; "
           "write the script with the Write tool instead.")
SLEEP = "The Claude Code Bash tool blocks `sleep N` followed by another command; wait with an until-loop instead."
RMM = "On the RMM host, `git status` in /rmm fails with 'dubious ownership'; use git -c safe.directory=/rmm."
TESTS = "Alpha's go tests need -count=1, or the cached result hides a broken build."
# What each failure printed: the hook gives a lesson only when the error plainly points to its command.
EOF_ERROR = "Exit code 2\n/usr/bin/bash: -c: line 3: unexpected EOF while looking for matching `''"
BLOCKED = "<tool_use_error>Blocked: sleep 240 followed by: ls. To wait for a condition, use Monitor.</tool_use_error>"
DUBIOUS = "Exit code 128\nfatal: detected dubious ownership in repository at '/rmm'"
STALE = "Exit code 1\nalpha/build_test.go:12:3: undefined: Thing\nok  alpha 0.1s (cached)"


class KeyTests(unittest.TestCase):
    def test_a_command_names_its_programs_and_subcommands(self) -> None:
        for command, expected in (
            ("git archive HEAD:sub | tar -x", {"git", "git archive", "tar"}),
            ("cd /x && python - <<'EOF'\nimport os\nprint(os.sep)\nEOF\ngit status", {"cd", "python", "python heredoc",
                                                                                     "git", "git status"}),
            ("sleep 240; ssh host 'uptime'", {"sleep", "ssh", "ssh host"}),
            ("sudo git -C /rmm status", {"git", "git status"}),
            ("ssh -o BatchMode=yes -i key saltbox 'df'", {"ssh", "ssh saltbox"}),
            ("timeout 600 python rmm.py command abc --timeout 150", {"rmm.py", "rmm.py command"}),
            ("PYTHONPATH=src python -m unittest test_x", {"python", "python unittest"}),
            ("sudo -n docker compose up -d", {"docker", "docker compose"}),
            ("& 'C:\\lab\\labctl.ps1' down --name x", {"labctl.ps1", "labctl.ps1 down"}),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, commands.keys(command))

    def test_only_specific_commands_are_keys(self) -> None:
        self.assertEqual("git archive", commands.valid_key(" Git  Archive "))
        self.assertEqual("python heredoc", commands.valid_key("python heredoc"))
        for general in ("git", "python", "docker", "read", "C:/x", "a b c", "", None):
            with self.subTest(key=general):
                self.assertIsNone(commands.valid_key(general))
        self.assertEqual([("git status", "/rmm"), ("sleep", "")], commands.tagged_keys(
            ["nas", commands.tag("git status", "/rmm"), commands.tag("sleep"), "command:git"]))


class CommandTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.home), known.ENVIRONMENT_SWITCH: "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.alpha = make_repository(self.root / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.beta = make_repository(self.root / "beta", "https://gitlab.example.com/team/beta.git")
        self.store = Store.open(self.home / "knowitall2.db")
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store, agent="codex", clock=Clock())

    def lesson(self, text: str, key: str | None = None, where: str = "", **options) -> str:
        options.setdefault("project_path", self.alpha)
        options.setdefault("scope", "global")
        tags = [commands.tag(key, where)] if key else []
        return self.memory.remember(text, kind="lesson", tags=tags, **options).record.id

    def failed(self, command: str, error: str, *, cwd: Path | None = None, session: str = "chat-1",
               event: str = "PostToolUseFailure") -> str:
        payload = {"hook_event_name": event, "session_id": session, "cwd": str(cwd or self.alpha),
                   "tool_name": "Bash", "tool_input": {"command": command}, "error": error}
        return commands.hint(payload, str(self.home))


class HintTests(CommandTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.quoting = self.lesson(QUOTING, "python heredoc")
        self.sleep = self.lesson(SLEEP, "sleep")
        self.rmm = self.lesson(RMM, "git status", where="/rmm")
        self.tests = self.lesson(TESTS, "go test", scope="project")
        self.lesson("The NAS answers on its management address.")   # no command: never a reminder
        known.refresh(self.store._connection)

    def test_a_failed_command_brings_what_is_known_about_it_once_per_chat(self) -> None:
        context = self.failed("python - <<'EOF'\nprint('C:\\new')\nEOF", EOF_ERROR)
        self.assertIn("that `python heredoc` command failed", context)
        self.assertIn(f"[{self.quoting}]", context)
        self.assertNotIn(self.sleep, context)
        self.assertEqual("", self.failed("python - <<'EOF'\nprint(1)\nEOF", EOF_ERROR))   # told already in this chat
        self.assertIn(self.quoting, self.failed("python - <<'EOF'\nx\nEOF", EOF_ERROR, session="chat-2"))
        logged = [json.loads(line) for line in (self.home / commands.HINT_LOG).read_text().splitlines()]
        self.assertEqual([("python heredoc", [self.quoting])] * 2, [(entry["key"], entry["ids"]) for entry in logged])

    def test_only_a_failure_brings_it(self) -> None:
        self.assertEqual("", self.failed("sleep 240; ls", BLOCKED, event="PreToolUse"))
        self.assertEqual("", self.failed("sleep 240; ls", BLOCKED, event="PostToolUse"))
        self.assertIn(self.sleep, self.failed("sleep 240; ls", BLOCKED))

    def test_a_lesson_applies_only_where_it_says(self) -> None:
        self.assertEqual("", self.failed("git status --short", DUBIOUS))           # not about /rmm
        self.assertIn(self.rmm, self.failed("sudo git -C /rmm status", DUBIOUS))
        self.assertEqual("", self.failed("go test ./...", STALE, cwd=self.beta))  # alpha's own lesson
        self.assertIn(self.tests, self.failed("go test ./...", STALE, cwd=self.alpha / "src"))

    def test_the_error_decides_which_part_of_a_command_it_is_about(self) -> None:
        heredoc_then_status = "python - <<'EOF'\nprint(1)\nEOF\nsudo git -C /rmm status"
        self.assertIn(self.rmm, self.failed(heredoc_then_status, DUBIOUS))
        context = self.failed(heredoc_then_status, EOF_ERROR, session="chat-2")
        self.assertIn(self.quoting, context)
        self.assertNotIn(self.rmm, context)
        # A failure its lessons say nothing about brings nothing; nor does a heredoc run on another host.
        self.assertEqual("", self.failed("sleep 240; ls", "Exit code 2\nls: cannot access 'x'", session="chat-3"))
        self.assertEqual("", self.failed("ssh host 'python3 - <<EOF\nprint(1)\nEOF'", EOF_ERROR, session="chat-4"))

    def test_the_hook_prints_the_context_for_the_agent(self) -> None:
        payload = {"hook_event_name": "PostToolUseFailure", "session_id": "s", "cwd": str(self.alpha),
                   "tool_input": {"command": "sleep 90 && curl x"}, "error": BLOCKED}
        output = json.loads(commands.main("claude-code", str(self.home), json.dumps(payload).encode("utf-8")))
        self.assertEqual("PostToolUseFailure", output["hookSpecificOutput"]["hookEventName"])
        self.assertIn(self.sleep, output["hookSpecificOutput"]["additionalContext"])
        for broken in (b"", b"not json", b"[]", json.dumps({**payload, "tool_input": "x"}).encode("utf-8")):
            with self.subTest(stdin=broken[:20]):
                self.assertEqual("", commands.main("claude-code", str(self.home), broken))

    def test_the_launcher_runs_the_light_path_and_never_fails(self) -> None:
        launcher = self.root / "launcher.py"
        launcher.write_text(render_hook_launcher(
            ServerLaunch(sys.executable, (), {"PYTHONPATH": str(SOURCE_ROOT), "KNOWITALL2_HOME": str(self.home)}),
            "claude-code"), encoding="utf-8")
        payload = json.dumps({"hook_event_name": "PostToolUseFailure", "session_id": "s", "cwd": str(self.alpha),
                              "tool_input": {"command": "sleep 90; date"}, "error": BLOCKED})
        done = subprocess.run([sys.executable, "-B", str(launcher), "command"], input=payload.encode("utf-8"),
                              capture_output=True, timeout=60)
        self.assertEqual(0, done.returncode)
        self.assertIn(self.sleep, json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"])
        done = subprocess.run([sys.executable, "-B", str(launcher), "command"], input=b"{broken",
                              capture_output=True, timeout=60)
        self.assertEqual((0, b""), (done.returncode, done.stdout))

    def test_retired_lessons_leave_the_index_and_off_removes_it(self) -> None:
        self.memory.forget(self.sleep, reason="the tool no longer blocks sleeps")
        self.assertTrue(known.due(self.store._connection))
        known.refresh(self.store._connection)
        self.assertNotIn("sleep", json.loads((self.home / commands.INDEX).read_text(encoding="utf-8")))
        known.remove_all(self.store._connection)
        self.assertFalse((self.home / commands.INDEX).exists())

    def test_a_new_tag_makes_a_refresh_due(self) -> None:
        record = self.store.get(self.lesson("Some lesson about the nightly backups.", None))
        known.refresh(self.store._connection)
        self.store.set_tags(record.id, [commands.tag("rsync")], now=record.created_at)
        self.assertTrue(known.due(self.store._connection))


class FakeEngine:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.inputs: list[str] = []

    def run(self, text, *, schema, system_prompt):
        self.inputs.append(text)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class TaggingTests(CommandTestCase):
    def test_the_model_says_which_command_a_lesson_is_about(self) -> None:
        quoting, rmm = self.lesson(QUOTING), self.lesson(RMM)
        fact = self.memory.remember("The NAS answers on its management address.", project_path=self.alpha).record.id
        plain = self.lesson("Alpha's release notes are in CHANGELOG.md.")
        unanswered = self.lesson(SLEEP)
        engine = FakeEngine({"lessons": [
            {"id": quoting, "command": "Python heredoc", "where": ""},
            {"id": rmm, "command": "git status", "where": "/rmm"},
            {"id": plain, "command": "git", "where": ""},            # too general: looked at, not tagged
        ]})
        state = LearnerState(self.home / "learner" / "state.json")
        report = command_tags.tag_commands(self.store, engine, budget=3, now="2026-10-11T00:00:00Z", state=state)
        self.assertEqual((1, 3, 2, {"python heredoc": 1, "git status": 1}),
                         (report.calls, report.looked_at, report.tagged, report.keys))
        self.assertNotIn(fact, engine.inputs[0])   # a fact that says nothing about how a command fails
        self.assertEqual([commands.tag("git status", "/rmm")], list(self.store.get(rmm).tags))
        self.assertEqual([], list(self.store.get(plain).tags))
        self.assertEqual(1, state.calls_since(datetime(2000, 1, 1, tzinfo=timezone.utc)))
        # Looked at once: only the memory the model did not answer for is asked about again.
        self.assertEqual([unanswered], [record.id for record in command_tags.waiting(self.store)])

    def test_the_budget_and_a_failing_engine_leave_the_rest_for_later(self) -> None:
        for number in range(3):
            self.lesson(f"Lesson {number}: the deploy script needs --force here, or it stops halfway.")
        with mock.patch.object(command_tags, "BATCH", 1):
            report = command_tags.tag_commands(self.store, FakeEngine({"lessons": []}), budget=1, now="now")
            self.assertEqual((1, 2), (report.calls, report.deferred))
            report = command_tags.tag_commands(self.store, FakeEngine(RuntimeError("engine down")), budget=5,
                                               now="now")
        self.assertEqual((0, "engine down"), (report.calls, report.blocked))
        self.assertEqual(3, len(command_tags.waiting(self.store)))


class UpkeepShareTests(CommandTestCase):
    def test_catching_up_leaves_calls_for_maintenance_the_catalog_and_commands(self) -> None:
        # Waiting sessions took every call of every run, and maintenance made none in a week (2026-10-11).
        from contextlib import contextmanager
        from types import SimpleNamespace

        from knowitall2.learning import command
        from knowitall2.learning.learner import LearnReport
        from knowitall2.learning.state import LearnerSettings

        seen = {}

        def learn(**options):
            seen["learning"] = options["settings"].max_calls_per_run
            return LearnReport(calls=options["settings"].max_calls_per_run)

        def step(name):
            def run(*args, **options):
                seen[name] = options["budget"]
                return SimpleNamespace(calls=options["budget"], blocked=None, describe=lambda **_: name)
            return run

        @contextmanager
        def ours():
            yield True

        settings = LearnerSettings(enabled=True, max_calls_per_run=10, max_calls_per_day=60)
        with mock.patch.multiple(command, learn=learn, maintain=step("maintenance"), run_catalog=step("catalog"),
                                 tag_waiting_commands=step("commands"), maintenance_turn=ours,
                                 renew_maintenance_turn=lambda: True, share_quietly=lambda store: None,
                                 record_learning_run=lambda *a, **k: None, session_logs=lambda: [],
                                 learn_requests=lambda *a: SimpleNamespace(blocked=None, saved=[])), \
                mock.patch("sys.stdout"):
            command._learn_once(settings, object(), sweep=True)
        self.assertEqual({"learning": 6, "maintenance": 2, "catalog": 1, "commands": 1}, seen)


class BackgroundTests(CommandTestCase):
    def test_learning_runs_note_new_lessons_within_the_budget(self) -> None:
        from knowitall2.learning.command import tag_waiting_commands

        lesson = self.lesson(SLEEP)
        state = LearnerState(self.home / "learner" / "state.json")
        engine = FakeEngine({"lessons": [{"id": lesson, "command": "sleep", "where": ""}]})
        with mock.patch("sys.stdout"):
            self.assertEqual(0, tag_waiting_commands(self.store, engine, state=state, run="r-1", budget=0).calls)
            self.assertEqual([], engine.inputs)
            report = tag_waiting_commands(self.store, engine, state=state, run="r-1", budget=1)
        self.assertEqual((1, 1), (report.calls, report.tagged))
        [event] = self.store.events(kinds=["catalog"])
        self.assertEqual(({"sleep": 1}, "r-1"), (event["details"]["commands"], event["run_id"]))


if __name__ == "__main__":
    unittest.main()
