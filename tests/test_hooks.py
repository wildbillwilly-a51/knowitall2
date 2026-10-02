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

from knowitall2 import hooks
from knowitall2.learning.state import LearnerSettings, save_settings
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

        self.assertTrue(hooks.session_start("not json", start_learner=broken_learner) is not None)
        self.home.parent.joinpath("blocker").write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.home.parent / "blocker" / "inside")}):
            self.assertEqual("", hooks.session_start(self.payload(), start_learner=broken_learner))

    def test_main_prints_the_hook_output_and_always_exits_zero(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(hooks, "maybe_start_learner"):
            self.assertEqual(0, hooks.main(["claude-code", "session-start"], stdin=self.payload()))
        self.assertIn("additionalContext", output.getvalue())
        self.assertEqual(0, hooks.main(["unknown"], stdin=""))


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
        self.assertEqual([sys.executable, "-B", "-m", "knowitall2", "learn"], command)
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
        lock = self.home / "learner" / "lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("123", encoding="utf-8")
        self.assertFalse(hooks.maybe_start_learner(launcher=lambda *a, **k: None))


if __name__ == "__main__":
    unittest.main()
