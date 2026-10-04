import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from _support import make_repository

from knowitall2 import hooks, journal
from knowitall2.learning import moments
from knowitall2.learning.state import LearnerSettings, RunLock, save_settings
from knowitall2.memory import Memory
from knowitall2.paths import database_path
from knowitall2.store import Store


class HookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.home = root / "data"
        self.project = make_repository(root / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.home)})
        self.environment.start()
        journal._last_problem.clear()  # each test's problems reach its own journal

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def payload(self) -> str:
        return json.dumps({"session_id": "s1", "cwd": str(self.project), "hook_event_name": "SessionStart",
                           "source": "startup"})


class SessionStartTests(HookTestCase):
    def test_adds_the_briefing_as_session_context_and_starts_the_learner(self) -> None:
        store = Store.open(database_path())
        Memory(store, agent="cli").remember("Store credentials in Vaultwarden.", kind="rule", source="user",
                                            project_path=self.project)
        store.close()
        started = []
        output = json.loads(hooks.session_start(self.payload(), start_learner=lambda: started.append(True)))
        context = output["hookSpecificOutput"]
        self.assertEqual("SessionStart", context["hookEventName"])
        self.assertIn("briefing for project alpha", context["additionalContext"])
        self.assertIn("Store credentials in Vaultwarden.", context["additionalContext"])
        self.assertEqual([True], started)

    def test_never_fails_the_session(self) -> None:
        def broken_learner() -> None:
            raise RuntimeError("cannot start")

        # Input that is not JSON and a learner that cannot start still give the session its briefing.
        output = json.loads(hooks.session_start("not json", start_learner=broken_learner))
        self.assertEqual("SessionStart", output["hookSpecificOutput"]["hookEventName"])
        self.assertIn("KnowItAll2 briefing", output["hookSpecificOutput"]["additionalContext"])
        self.assertIn("background learning could not start", journal.problems_path().read_text(encoding="utf-8"))
        self.home.parent.joinpath("blocker").write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.home.parent / "blocker" / "inside")}):
            self.assertEqual("", hooks.session_start(self.payload(), start_learner=broken_learner))

    def test_main_prints_the_hook_output_and_always_exits_zero(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(hooks, "maybe_start_learner"):
            self.assertEqual(0, hooks.main(["claude-code", "session-start"], stdin=self.payload()))
        self.assertIn("additionalContext", output.getvalue())
        self.assertEqual(0, hooks.main(["unknown"], stdin=""))


class MainTests(HookTestCase):
    """``main`` exits 0 with its normal output whatever fails after the hook itself, and reads stdin as UTF-8."""

    NEWS = json.dumps({"systemMessage": "KnowItAll2 learned 1 memory from this session."})

    def setUp(self) -> None:
        super().setUp()
        # Looking for the session's log must not read the real agents' folders.
        self.agents = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home.parent / "claude"),
                                                   "CODEX_HOME": str(self.home.parent / "codex")})
        self.agents.start()

    def tearDown(self) -> None:
        self.agents.stop()
        super().tearDown()

    def deferring_hook(self, text: str, *, agent: str) -> str:
        # What a hook does when it shows news: mark it as shown once the output is written.
        hooks._deferred.append(([{"id": "n-1"}], None, "s1"))
        return self.NEWS

    def run_main(self, event: str = "stop", **options) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(hooks, "maybe_start_learner"):
            code = hooks.main(["claude-code", event], **options)
        return code, output.getvalue()

    def test_news_that_cannot_be_marked_shown_still_reaches_the_agent(self) -> None:
        with mock.patch.object(hooks, "stop", self.deferring_hook), \
                mock.patch.object(moments, "mark_shown", side_effect=PermissionError(13, "Access is denied")):
            self.assertEqual((0, self.NEWS), self.run_main(stdin=""))
        self.assertIn("Access is denied", journal.problems_path().read_text(encoding="utf-8"))

    def connect(self) -> None:
        (self.home / "server").mkdir(parents=True, exist_ok=True)
        (self.home / "server" / "connection.json").write_text('{"address": "http://127.0.0.1:9"}', encoding="utf-8")

    def test_a_sync_that_cannot_start_does_not_fail_the_hook(self) -> None:
        self.connect()
        with mock.patch.object(hooks, "stop", self.deferring_hook), \
                mock.patch.dict(sys.modules, {"knowitall2.connected": None}):
            self.assertEqual((0, self.NEWS), self.run_main(stdin=""))
        with mock.patch.object(hooks, "stop", self.deferring_hook), \
                mock.patch("knowitall2.connected.nudge", side_effect=RuntimeError("no sync")):
            self.assertEqual((0, self.NEWS), self.run_main(stdin=""))
        self.assertIn("no sync", journal.problems_path().read_text(encoding="utf-8"))

    def test_only_a_connected_computer_starts_a_sync(self) -> None:
        with mock.patch("knowitall2.connected.nudge") as nudge:
            self.run_main(stdin="")
            nudge.assert_not_called()
            self.connect()
            self.run_main(stdin="")
            nudge.assert_called_once_with("claude-code")

    def test_only_a_connected_computer_starts_a_sync_after_a_tool_call(self) -> None:
        from knowitall2.mcp_server import handle_once

        request = {"initialize": {"clientInfo": {"name": "codex"}}, "message": {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "recall", "arguments": {"query": "x"}}}}
        with mock.patch("knowitall2.connected.nudge") as nudge:
            handle_once(request)
            nudge.assert_not_called()
            self.connect()
            handle_once(request)
            self.assertEqual(1, nudge.call_count)

    def test_stdin_is_read_as_utf8_whatever_the_console_code_page(self) -> None:
        project = make_repository(self.project.parent / "alphá")
        payload = json.dumps({"session_id": "s1", "cwd": str(project)}, ensure_ascii=False).encode("utf-8")
        # Windows gives a hook's stdin the ANSI code page, which reads UTF-8 "á" as "Ã¡".
        with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252")):
            code, output = self.run_main("session-start")
        self.assertEqual(0, code)
        self.assertIn("briefing for project alphá", json.loads(output)["hookSpecificOutput"]["additionalContext"])

    def test_stdin_that_is_not_utf8_or_cannot_be_read_does_not_fail_the_hook(self) -> None:
        payload = self.payload().encode("utf-8").replace(b'"s1"', b'"s1\xff"')
        with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")):
            code, output = self.run_main("session-start")
        self.assertEqual(0, code)
        self.assertIn("briefing for project alpha", json.loads(output)["hookSpecificOutput"]["additionalContext"])
        unreadable = mock.Mock(**{"read.side_effect": OSError(22, "Invalid argument"),
                                  "buffer.read.side_effect": OSError(22, "Invalid argument")})
        for stdin in (unreadable, None):  # None: started without a stdin at all
            with self.subTest(stdin=stdin), mock.patch.object(sys, "stdin", stdin):
                self.assertEqual((0, ""), self.run_main("session-start"))


class LearnerStartTests(HookTestCase):
    def test_does_nothing_while_learning_is_off(self) -> None:
        launches = []
        self.assertFalse(hooks.maybe_start_learner(launcher=lambda *a, **k: launches.append(a)))
        self.assertEqual([], launches)

    def test_starts_one_detached_run_then_waits(self) -> None:
        save_settings(LearnerSettings(enabled=True))
        launches = []
        launcher = lambda command, **options: launches.append((command, options))  # noqa: E731
        self.assertTrue(hooks.maybe_start_learner(launcher=launcher))
        command, options = launches[0]
        self.assertEqual([sys.executable, "-B", "-P", "-m", "knowitall2", "learn"], command)
        if sys.platform == "win32":
            self.assertTrue(options["creationflags"] & subprocess.DETACHED_PROCESS)
        self.assertIn("src", options["env"]["PYTHONPATH"])
        self.assertEqual(str(self.home), options["env"]["KNOWITALL2_HOME"])
        self.assertFalse(hooks.maybe_start_learner(launcher=launcher))
        # The user's "Run now" does not wait for the interval.
        self.assertTrue(hooks.maybe_start_learner(launcher=launcher, force=True))
        self.assertTrue(hooks.maybe_start_learner(launcher=launcher, now=time.time() + hooks.SPAWN_INTERVAL_SECONDS + 1))

    def test_does_not_start_while_a_run_holds_the_lock(self) -> None:
        save_settings(LearnerSettings(enabled=True))
        with RunLock():
            self.assertFalse(hooks.maybe_start_learner(launcher=lambda *a, **k: None))


if __name__ == "__main__":
    unittest.main()
