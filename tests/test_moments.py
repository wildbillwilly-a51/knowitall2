import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _support import make_repository
from test_learning import SESSION, FakeExtractor, LogBuilder, text

from knowitall2 import hooks
from knowitall2.cli import main
from knowitall2.learning import moments
from knowitall2.learning.cataloguer import CatalogReport
from knowitall2.learning.learner import LearnReport
from knowitall2.learning.state import LearnerSettings, LearnerState, save_settings
from knowitall2.memory import Memory
from knowitall2.paths import database_path
from knowitall2.store import Store

COMMIT_OUTPUT = "[main 1a2b3c4] Fix the router DNS settings\n 1 file changed, 2 insertions(+)"


class MomentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = make_repository(self.root / "work" / "homelab", "https://gitlab.example.com/me/homelab.git")
        self.environment = mock.patch.dict(os.environ, {
            "KNOWITALL2_HOME": str(self.root / "data"), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
        })
        self.environment.start()
        self.log_path = self.root / "claude" / "projects" / "C--work-homelab" / f"{SESSION}.jsonl"
        self.started: list[dict] = []

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def learning_on(self) -> None:
        save_settings(LearnerSettings(enabled=True))

    def log(self, *, commit: bool = True) -> LogBuilder:
        builder = LogBuilder(self.project).user("Please fix DNS on the router; the router is at 10.20.30.1.")
        builder.tool("t1", "PowerShell", {"command": "ssh root@10.20.30.1 cat /etc/openwrt_release"},
                     "DISTRIB_RELEASE='23.05.3'")
        if commit:
            builder.tool("t2", "Bash", {"command": 'git commit -m "Fix the router DNS settings"'}, COMMIT_OUTPUT)
        return builder.assistant(text("DNS is fixed and committed."))

    def payload(self, **extra) -> str:
        return json.dumps({"session_id": SESSION, "transcript_path": str(self.log_path), "cwd": str(self.project),
                           **extra})

    def start_learner(self, **options) -> None:
        self.started.append(options)

    def stop(self) -> dict:
        output = hooks.stop(self.payload(hook_event_name="Stop"), agent="claude-code", start_learner=self.start_learner)
        return json.loads(output) if output else {}


class CommitTests(MomentTestCase):
    def test_finds_each_commit_once_and_leaves_a_partial_line_for_later(self) -> None:
        later = LogBuilder(self.project).tool("t9", "Bash", {"command": "git commit -am 'Second'"},
                                              "[main 9f8e7d6] Second")
        tail = "".join(json.dumps(record) + "\n" for record in later.records)
        # The command's record is still being written when the first look happens.
        self.log().write(self.log_path, idle=False, partial_tail=tail[:20])
        self.assertEqual(["1a2b3c4"], moments.commits_since_last_look(self.log_path))
        self.assertEqual([], moments.commits_since_last_look(self.log_path))
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(tail[20:])
        self.assertEqual(["9f8e7d6"], moments.commits_since_last_look(self.log_path))

    def test_the_first_look_skips_what_was_already_learned(self) -> None:
        self.log().write(self.log_path, idle=False)
        size = self.log_path.stat().st_size
        self.assertEqual([], moments.commits_since_last_look(self.log_path, first_look_from=lambda: size))

    def test_counts_commands_that_commit_and_not_text_that_quotes_them(self) -> None:
        for tool, command, output, expected in (
            ("Bash", "git commit -m 'First'", "[master (root-commit) abcdef1] First", ["abcdef1"]),
            ("Bash", "git -C \"C:/work/my repo\" commit -m x", "[detached HEAD 1234567] Try", ["1234567"]),
            ("PowerShell", "git add . ; git commit -q -m 'Quiet'", "", [""]),
            ("Bash", "git commit -q -m x && git push", "   30e5e0b..b01f920  HEAD -> codex/work", [""]),
            ("Bash", "git cherry-pick 89abcde", "[codex/moment-learning 89abcde] Work", ["89abcde"]),
            ("Bash", "git commit -m x", "On branch main\nnothing to commit, working tree clean", []),
            ("Bash", "git commit -m x", "Exit code 1\nerror: pathspec 'x' did not match", []),
            ("Bash", "git commit --dry-run", "Changes to be committed:", []),
            ("Bash", "git log --oneline", "abcdef1 First\n1234567 Second", []),
            ("Bash", "grep -n commit moments.py", "12: # ``[main 1a2b3c4] Subject``", []),
            ("Write", "git commit -m x", "[main 1a2b3c4] Subject", []),
        ):
            with self.subTest(command=command, output=output):
                path = self.root / f"{abs(hash((tool, command, output)))}.jsonl"
                LogBuilder(self.project).tool("t1", tool, {"command": command}, output).write(path, idle=False)
                self.assertEqual(expected, moments.commits_since_last_look(path))

    def test_reads_codex_commands(self) -> None:
        log = self.root / "codex" / "sessions" / "2026" / "09" / "29" / f"rollout-2026-09-29T10-00-00-{SESSION}.jsonl"
        log.parent.mkdir(parents=True)
        records = [
            {"type": "session_meta", "payload": {"id": SESSION, "cwd": str(self.project), "source": "cli"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "command": ["pwsh", "-Command", "git commit -m 'Fix'"], "exit_code": 0,
                "aggregated_output": "[codex/work be9f9d5] Fix\n 1 file changed"}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "command": ["pwsh", "-Command", "git commit -m 'Again'"], "exit_code": 1,
                "aggregated_output": "nothing to commit"}}},
        ]
        log.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        self.assertEqual(["be9f9d5"], moments.commits_since_last_look(log))


class StopHookTests(MomentTestCase):
    def test_a_commit_starts_learning_right_away_and_says_so(self) -> None:
        self.learning_on()
        self.log().write(self.log_path, idle=False)
        message = self.stop()["systemMessage"]
        self.assertIn("KnowItAll2 is learning from this session (after your commit 1a2b3c4)", message)
        self.assertEqual([{"requests": True}], self.started)
        [request] = moments.pending_requests()
        self.assertEqual(("commit", "1a2b3c4", str(self.log_path)),
                         (request["reason"], request["detail"], request["transcript"]))
        # The next turn has nothing new: no message, no second request.
        self.assertEqual({}, self.stop())
        self.assertEqual(1, len(moments.pending_requests()))

    def test_does_nothing_while_learning_is_off(self) -> None:
        self.log().write(self.log_path, idle=False)
        self.assertEqual({}, self.stop())
        self.assertEqual([], self.started)
        self.assertEqual([], moments.pending_requests())

    def test_a_turn_without_a_commit_starts_nothing(self) -> None:
        self.learning_on()
        self.log(commit=False).write(self.log_path, idle=False)
        self.assertEqual({}, self.stop())
        self.assertEqual([], self.started)

    def test_shows_what_was_learned_once(self) -> None:
        self.learning_on()
        self.log(commit=False).write(self.log_path, idle=False)
        moments.add_news({
            "reason": "commit", "detail": "1a2b3c4", "status": "done",
            "sessions": moments.session_keys(str(self.log_path), SESSION), "folders": [str(self.project)],
            "saved": [{"id": "k-1", "text": "The homelab router runs OpenWrt 23.05.3."},
                      {"id": "k-2", "text": "dnsmasq needs a restart after editing /etc/config/dhcp."}],
            "updated": [], "already_known": 1, "turned_down": [{"text": "x", "reason": "evidence not in the session"}],
        })
        message = self.stop()["systemMessage"]
        self.assertIn("KnowItAll2 learned 2 things from this session (after your commit 1a2b3c4):", message)
        self.assertIn("  - The homelab router runs OpenWrt 23.05.3.", message)
        self.assertIn("(1 already known, 1 turned down)", message)
        self.assertEqual({}, self.stop())

    def test_the_news_of_an_ended_session_is_shown_by_the_next_one_in_the_same_folder(self) -> None:
        self.learning_on()
        moments.add_news({"reason": "session end", "status": "done", "sessions": ["t-other"],
                          "folders": [str(self.project)], "saved": [{"id": "k-3", "text": "A lesson."}]})
        start = json.loads(hooks.session_start(self.payload(), start_learner=lambda: None))
        self.assertIn("KnowItAll2 learned 1 thing from your last session here (when the session ended)",
                      start["systemMessage"])
        other = self.root / "other.jsonl"
        output = hooks.stop(json.dumps({"transcript_path": str(other), "cwd": str(self.project)}),
                            start_learner=self.start_learner)
        self.assertEqual("", output)

    def test_a_limit_says_how_to_learn_anyway(self) -> None:
        entry = {"reason": "commit", "detail": "1a2b3c4", "status": "limit", "limit": 60}
        self.assertEqual(
            "KnowItAll2 has not learned from this session yet: today's limit of 60 learning calls is used up. "
            "To learn it now, open KnowItAll2, then Learning, then Learn anyway.", moments.describe(entry))


class SilentAppTests(MomentTestCase):
    """The Claude desktop app receives hook messages but does not show them: the agent passes the news on."""

    def setUp(self) -> None:
        super().setUp()
        self.learning_on()
        self.desktop = mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
        self.desktop.start()
        self.addCleanup(self.desktop.stop)
        self.log(commit=False).write(self.log_path, idle=False)
        moments.add_news({"reason": "commit", "detail": "1a2b3c4", "status": "done",
                          "sessions": moments.session_keys(str(self.log_path), SESSION),
                          "folders": [str(self.project)],
                          "saved": [{"id": "k-1", "text": "The homelab router runs OpenWrt 23.05.3."}]})

    def prompt(self) -> dict:
        output = hooks.prompt_submit(self.payload(hook_event_name="UserPromptSubmit", prompt="next"))
        return json.loads(output) if output else {}

    def test_the_news_goes_to_the_agent_with_the_users_next_message_once(self) -> None:
        self.assertEqual({}, self.stop())  # no message the app would drop, and the news is kept
        context = self.prompt()["hookSpecificOutput"]
        self.assertEqual("UserPromptSubmit", context["hookEventName"])
        self.assertTrue(context["additionalContext"].startswith(hooks.RELAY))
        self.assertIn("KnowItAll2 learned 1 thing from this session (after your commit 1a2b3c4)",
                      context["additionalContext"])
        self.assertEqual({}, self.prompt())

    def test_waiting_for_the_limit_is_told_once_however_many_commits_hit_it(self) -> None:
        moments.mark_shown(moments.news(), transcript=str(self.log_path), session_id=SESSION)
        for _ in range(2):
            moments.add_news({"reason": "commit", "status": "limit", "limit": 60, "folders": [str(self.project)],
                              "sessions": moments.session_keys(str(self.log_path), SESSION)})
        context = self.prompt()["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(1, context.count("has not learned from this session yet"))
        self.assertIn("only in a collapsed notice", context)

    def test_learning_in_progress_is_mentioned_once(self) -> None:
        moments.mark_shown(moments.news(), transcript=str(self.log_path), session_id=SESSION)
        moments.set_now({"since": "2026-09-29T12:00:00Z", "doing": "learning from a session", "reason": "commit",
                         "detail": "9f8e7d6", "folder": str(self.project)})
        with mock.patch("knowitall2.learning.state.RunLock.busy", return_value=True):
            first = self.prompt()["hookSpecificOutput"]["additionalContext"]
            self.assertIn("KnowItAll2 is learning from this session (after your commit 9f8e7d6)", first)
            self.assertEqual({}, self.prompt())

    def test_a_new_session_hears_it_at_the_start(self) -> None:
        moments.add_news({"reason": "session end", "status": "done", "sessions": ["t-other"],
                          "folders": [str(self.project)], "saved": [{"id": "k-2", "text": "A lesson."}]})
        output = json.loads(hooks.session_start(json.dumps({"session_id": "new", "cwd": str(self.project),
                                                            "transcript_path": str(self.root / "new.jsonl")}),
                                                start_learner=lambda: None))
        self.assertNotIn("systemMessage", output)
        self.assertIn(hooks.RELAY, output["hookSpecificOutput"]["additionalContext"])

    def test_apps_that_show_messages_get_none_through_the_agent(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            self.assertEqual({}, self.prompt())
            self.assertIn("systemMessage", self.stop())
        codex = hooks.prompt_submit(self.payload(), agent="codex")
        self.assertEqual("", codex)

    def test_the_session_log_names_the_app_when_the_environment_does_not(self) -> None:
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT")
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "attachment", "entrypoint": "claude-desktop"}) + "\n")
        self.assertFalse(hooks.shows_hook_messages(json.loads(self.payload()), "claude-code"))


class EncodingTests(MomentTestCase):
    """Hook output reaches the agent whatever characters memories hold (a memory with "≥" once broke briefings)."""

    TEXT = "Frames need ≥ 2 GB free → 99% ✓ before a run."

    def setUp(self) -> None:
        super().setUp()
        self.learning_on()
        self.log(commit=False).write(self.log_path, idle=False)
        moments.add_news({"reason": "commit", "status": "done", "folders": [str(self.project)],
                          "sessions": moments.session_keys(str(self.log_path), SESSION),
                          "saved": [{"id": "k-1", "text": self.TEXT}]})

    def run_hook(self, event: str) -> subprocess.CompletedProcess:
        environment = {name: value for name, value in os.environ.items() if name != "PYTHONIOENCODING"}
        environment.update({"CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
                            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        code = f"import sys; from knowitall2.hooks import main; raise SystemExit(main(['claude-code', '{event}']))"
        return subprocess.run([sys.executable, "-B", "-c", code], input=self.payload().encode("utf-8"),
                              capture_output=True, env=environment, timeout=60)

    def test_the_agent_gets_every_character_through_a_pipe(self) -> None:
        done = self.run_hook("prompt-submit")
        self.assertEqual(0, done.returncode, done.stderr.decode(errors="replace"))
        done.stdout.decode("ascii")  # plain ASCII, so no code page can break it
        context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.TEXT, context)
        self.assertEqual([], moments.news_to_show(transcript=str(self.log_path), session_id=SESSION,
                                                  cwd=str(self.project)))

    def test_news_that_could_not_be_written_stays_unshown(self) -> None:
        class Broken(io.StringIO):
            def write(self, text):
                raise OSError("the pipe is closed")

        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}), \
                mock.patch("sys.stdout", Broken()):
            self.assertEqual(0, hooks.main(["claude-code", "prompt-submit"], stdin=self.payload()))
        self.assertEqual(1, len(moments.news_to_show(transcript=str(self.log_path), session_id=SESSION,
                                                     cwd=str(self.project))))

    def test_the_command_line_prints_any_memory_to_a_pipe(self) -> None:
        environment = {name: value for name, value in os.environ.items() if name != "PYTHONIOENCODING"}
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        source = Path(__file__).resolve().parents[1] / "src"
        saved = subprocess.run([sys.executable, "-B", "-m", "knowitall2", "remember", self.TEXT], capture_output=True,
                               env=environment, cwd=str(source), timeout=60)
        self.assertEqual(0, saved.returncode, saved.stderr.decode(errors="replace"))
        found = subprocess.run([sys.executable, "-B", "-m", "knowitall2", "recall", "frames free run"],
                               capture_output=True, env=environment, cwd=str(source), timeout=60)
        self.assertEqual(0, found.returncode, found.stderr.decode(errors="replace"))
        self.assertIn(b"Frames need", found.stdout)

    def test_detached_learning_writes_its_log_as_utf8(self) -> None:
        self.assertEqual("utf-8", hooks._learner_environment()["PYTHONIOENCODING"])


class SessionEndTests(MomentTestCase):
    def test_learns_the_rest_of_a_session_only_when_it_grew(self) -> None:
        self.learning_on()
        self.log().write(self.log_path, idle=False)
        hooks.session_end(self.payload(), start_learner=self.start_learner)
        [request] = moments.pending_requests()
        self.assertEqual("session end", request["reason"])
        moments.done_with([request])
        state = LearnerState()
        state.entry(self.log_path)["offset"] = self.log_path.stat().st_size
        state.save()
        hooks.session_end(self.payload(), start_learner=self.start_learner)
        self.assertEqual([], moments.pending_requests())


class AskedTests(MomentTestCase):
    def test_learn_finds_the_current_session_by_its_folder(self) -> None:
        self.learning_on()
        self.log(commit=False).write(self.log_path, idle=False)
        reply = hooks.request_now(cwd=self.project, agent="claude-code", reason="asked",
                                  start_learner=self.start_learner)
        self.assertIn("learning from this session now", reply)
        [request] = moments.pending_requests()
        self.assertEqual(("asked", str(self.log_path)), (request["reason"], request["transcript"]))

    def test_learn_says_when_it_cannot_find_the_session(self) -> None:
        self.learning_on()
        self.assertIn("could not find this session's log",
                      hooks.request_now(cwd=self.project, agent="claude-code", reason="asked",
                                        start_learner=self.start_learner))
        self.assertEqual([], self.started)


class RequestLearningTests(MomentTestCase):
    def run_requests(self, extractor) -> str:
        output = io.StringIO()
        with redirect_stdout(output), \
                mock.patch("knowitall2.learning.command.build_engine", return_value=extractor), \
                mock.patch("knowitall2.learning.command.run_catalog", return_value=CatalogReport()):
            self.assertEqual(0, main(["learn", "--requests"]))
        return output.getvalue()

    def test_a_requested_session_is_learned_while_active_and_the_news_is_shown(self) -> None:
        self.learning_on()
        self.log().write(self.log_path, idle=False)  # still active: the regular pass would wait
        self.stop()
        extractor = FakeExtractor([[
            {"text": "The homelab router runs OpenWrt 23.05.3.", "kind": "fact", "subjects": ["router"],
             "scope": "global", "evidence": "DISTRIB_RELEASE='23.05.3'"},
            {"text": "The router's admin page is at 10.20.30.1 and nowhere else.", "kind": "fact", "subjects": [],
             "scope": "global", "evidence": "a quote that is nowhere in the session"},
        ]])
        self.run_requests(extractor)
        self.assertEqual(1, len(extractor.dossiers))
        self.assertEqual([], moments.pending_requests())
        [entry] = moments.news()
        self.assertEqual(("commit", "done", 1), (entry["reason"], entry["status"], len(entry["saved"])))
        self.assertEqual("evidence not in the session", entry["turned_down"][0]["reason"])
        message = self.stop()["systemMessage"]
        self.assertIn("KnowItAll2 learned 1 thing from this session (after your commit 1a2b3c4):", message)
        self.assertIn("The homelab router runs OpenWrt 23.05.3.", message)
        self.assertIn("1 turned down", message)
        self.assertIsNone(moments.read_now())

    def test_work_is_learned_whatever_the_daily_limit_and_still_counts_toward_it(self) -> None:
        # The daily limit holds back only background learning; the user's work never waits for it.
        save_settings(LearnerSettings(enabled=True, max_calls_per_day=1))
        state = LearnerState()
        state.record_call(at=datetime.now(timezone.utc), session="x", outcome="ok")
        state.save()
        self.log().write(self.log_path, idle=False)
        self.stop()
        extractor = FakeExtractor([[]])
        self.run_requests(extractor)
        self.assertEqual(1, len(extractor.dossiers))
        [entry] = moments.news()
        self.assertEqual("done", entry["status"])
        self.assertEqual(2, LearnerState().calls_since(datetime.now(timezone.utc) - timedelta(days=1)))

    def test_what_is_learned_is_filed_under_the_project_the_work_was_in(self) -> None:
        self.learning_on()
        other = make_repository(self.root / "work" / "beta", "https://gitlab.example.com/me/beta.git")
        store = Store.open(database_path())
        try:
            Memory(store, agent="cli").project_for(other)  # KnowItAll2 has seen project beta
        finally:
            store.close()
        builder = LogBuilder(self.project).user("Let's fix the release in beta.")
        for number in range(3):
            builder.tool(f"b{number}", "Bash", {"command": f'cat "{other / f"release{number}.md"}"'},
                         "Beta ships with make release from the main branch.")
        builder.tool("c1", "Bash", {"command": f'git -C "{other}" commit -m "Fix release"'}, COMMIT_OUTPUT)
        builder.assistant(text("Fixed and committed.")).write(self.log_path, idle=False)
        self.stop()
        extractor = FakeExtractor([[
            {"text": "Beta ships with make release from the main branch.", "kind": "procedure", "subjects": ["beta"],
             "scope": "project", "evidence": "Beta ships with make release from the main branch."},
        ]])
        self.run_requests(extractor)
        self.assertIn("worked in project beta", extractor.dossiers[0].text)
        store = Store.open(database_path())
        try:
            [row] = store.list_active(project_id=None, scope="everywhere", limit=5)
            self.assertEqual("beta", row.project_name)
        finally:
            store.close()

    def test_an_unusable_engine_is_news_too(self) -> None:
        self.learning_on()
        self.log().write(self.log_path, idle=False)
        self.stop()
        output = io.StringIO()
        with redirect_stdout(output), mock.patch("knowitall2.learning.command.build_engine", return_value=None), \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(1, main(["learn", "--requests"]))
        [entry] = moments.news()
        self.assertEqual("stopped", entry["status"])
        self.assertIn("could not learn from this session: no Claude Code engine was found", self.stop()["systemMessage"])


class NewsEntryTests(unittest.TestCase):
    def test_counts_each_outcome(self) -> None:
        from knowitall2.learning.command import news_entry

        report = LearnReport(results=[
            {"outcome": "saved", "ids": ["k-1"], "text": "one"},
            {"outcome": "saved with a question", "ids": ["k-2", "k-0"], "text": "two"},
            {"outcome": "updated", "ids": ["k-3", "k-9"], "text": "three"},
            {"outcome": "already known", "ids": ["k-4"], "text": "four"},
            {"outcome": "rejected (secret)", "ids": [], "text": None},
        ])
        entry = news_entry(report, LearnerSettings(), reason="asked", run="r-1", requests=[])
        self.assertEqual((2, 1, 1, 1, 1), (len(entry["saved"]), entry["questions"], len(entry["updated"]),
                                           entry["already_known"], len(entry["turned_down"])))
        self.assertEqual({"text": "", "reason": "secret"}, entry["turned_down"][0])


class HookSpeedTests(unittest.TestCase):
    def test_the_end_of_turn_hook_stays_light(self) -> None:
        # The hook runs after every turn: it must not load the memory store or the learner.
        import subprocess
        import sys

        code = ("import sys, knowitall2.hooks; "
                "print(any(name in sys.modules for name in "
                "('knowitall2.store', 'knowitall2.memory', 'knowitall2.learning.learner', 'sqlite3')))")
        source = Path(__file__).resolve().parents[1] / "src"
        done = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PYTHONPATH": str(source)})
        self.assertEqual("False", done.stdout.strip(), done.stderr)


if __name__ == "__main__":
    unittest.main()
