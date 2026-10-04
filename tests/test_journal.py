import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository
from test_learning import SESSION, FakeExtractor, LogBuilder, text

from knowitall2 import hooks, journal, review
from knowitall2.cli import main
from knowitall2.learning.command import record_learning_run
from knowitall2.learning.extractor import ClaudeCliExtractor, claude_usage, codex_usage
from knowitall2.learning.learner import LearnReport, learn
from knowitall2.learning.maintenance import maintain
from knowitall2.learning.state import LearnerSettings, LearnerState
from knowitall2.mcp_server import McpServer
from knowitall2.memory import Memory
from knowitall2.paths import database_path
from knowitall2.store import Store, StoreError


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class HomeTestCase(unittest.TestCase):
    """A private data home, so problems written to the file can be read back."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {
            "KNOWITALL2_HOME": str(self.root / "home"), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
        })
        self.environment.start()
        journal._last_problem.clear()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()


class JournalTests(HomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = make_repository(self.root / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.store = Store.in_memory()
        self.memory = Memory(self.store, agent="cli", clock=Clock("2026-09-28T12:00:00Z"))

    def tearDown(self) -> None:
        self.store.close()
        super().tearDown()

    def recorded(self) -> list[tuple[str, str]]:
        return [(event["kind"], event["outcome"]) for event in reversed(self.store.events(limit=100))]

    def test_every_memory_operation_is_recorded(self) -> None:
        first = self.memory.remember("The NAS is nas01.", subjects=["NAS"]).record
        self.memory.remember("The NAS is nas01.")
        newer = self.memory.remember("The NAS is nas02.", replaces=first.id).record
        self.memory.recall("nas", project_path=self.project)
        self.memory.recall("printer toner", project_path=self.project)
        self.memory.briefing(project_path=self.project)
        self.memory.forget(newer.id, reason="moved to a new host")
        self.memory.restore(newer.id)
        self.assertEqual([
            ("remember", "saved"), ("remember", "already known"), ("remember", "updated"), ("recall", "found"),
            ("recall", "nothing"), ("briefing", "empty"), ("forget", "retired"), ("restore", "restored"),
        ], self.recorded())
        events = {(event["kind"], event["outcome"]): event for event in self.store.events(limit=100)}
        updated = events[("remember", "updated")]
        self.assertEqual(([newer.id, first.id], "cli", "2026-09-28T12:00:00Z"),
                         (updated["record_ids"], updated["agent"], updated["at"]))
        self.assertEqual('Recall "nas" for project alpha: 1 found', events[("recall", "found")]["summary"])
        self.assertEqual('Recall "printer toner" for project alpha: nothing found',
                         events[("recall", "nothing")]["summary"])
        self.assertEqual("Briefing for project alpha: 0 memories shown", events[("briefing", "empty")]["summary"])
        self.assertEqual({"reason": "moved to a new host"}, events[("forget", "retired")]["details"])

    def test_questions_and_answers_are_recorded(self) -> None:
        stated = self.memory.remember("Backups run at 02:00.", source="user").record
        newer = self.memory.remember("Backups run at 04:00.").record
        review.ask_conflict(self.memory, stated, newer)
        [question] = self.store.open_questions(limit=5)
        review.answer(self.memory, question["id"], "keep_both")
        self.assertEqual([("question", "asked"), ("answer", "keep_both")], self.recorded()[-2:])
        [answered] = self.store.events(kinds=["answer"])
        self.assertEqual(([stated.id, newer.id], question["id"]), (answered["record_ids"], answered["details"]["question"]))

    def test_the_memorys_own_entries_can_be_turned_off(self) -> None:
        Memory(self.store, agent="learner", record_events=False).remember("The router is rtr01.")
        self.assertEqual([], self.store.events())

    def test_texts_are_redacted_and_capped(self) -> None:
        token = "sk-" + "a" * 30
        journal.record(self.store, "remember", f"uses {token} " + "x" * 400, details={"evidence": f"key {token}"})
        [event] = self.store.events()
        self.assertNotIn(token, json.dumps(event))
        self.assertIn("[REDACTED API key]", event["summary"])
        self.assertLessEqual(len(event["summary"]), journal.SUMMARY_CHARACTERS)

    def test_database_passwords_are_redacted(self) -> None:
        password = "Xk9" + "#mQ2vL8pR"  # split so repository secret scanners do not flag it
        for text in (f"PGPASSWORD={password} psql -h db -U app", f"machine nas01\n  login admin\n  password {password}",
                     f"DB_PASSWORD={password}"):
            journal.record(self.store, "recall", text, details={"command": text})
        events = self.store.events()
        self.assertEqual(3, len(events))
        self.assertNotIn(password, json.dumps(events))
        self.assertEqual(3, sum("[REDACTED password]" in event["summary"] for event in events))

    def test_the_journal_never_breaks_an_operation(self) -> None:
        broken = mock.Mock()
        broken.add_event.side_effect = RuntimeError("disk full")
        journal.record(broken, "recall", "anything")
        with mock.patch.object(self.store, "add_event", side_effect=sqlite3.OperationalError("database is locked")):
            result = self.memory.remember("The router is rtr01.")
        self.assertEqual("saved", result.status)

    def test_events_filter_and_old_ones_are_pruned(self) -> None:
        journal.record(self.store, "recall", "long ago", at="2026-01-01T00:00:00Z")
        journal.record(self.store, "candidate", "from the run", run="r-1", record_ids=["k-1"])
        journal.record(self.store, "candidate", "from another run", run="r-2")
        self.assertEqual(["from the run"], [event["summary"] for event in self.store.events(run_id="r-1")])
        self.assertEqual(["from the run"], [event["summary"] for event in self.store.events(record_id="k-1")])
        self.assertEqual(2, len(self.store.events(kinds=["candidate"])))
        journal.prune(self.store, now=datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertNotIn("long ago", [event["summary"] for event in self.store.events()])


class HousekeepingTests(HomeTestCase):
    """Old events and uses go in everyday use, with learning off (review findings M18 growth, T3)."""

    def setUp(self) -> None:
        super().setUp()
        self.project = make_repository(self.root / "alpha", "https://gitlab.example.com/team/alpha.git")
        now = datetime.now(timezone.utc)
        self.store = Store.open(database_path())
        memory = Memory(self.store, agent="cli")
        self.record_id = memory.remember("The router is rtr01.").record.id
        for days, operation in ((400, "recall"), (400, "briefing"), (3, "recall")):
            self.store.log_usage(at=_stamp(now - timedelta(days=days)), operation=operation, agent="codex",
                                 project_id=None, query="router", record_ids=[self.record_id])
        journal.record(self.store, "recall", "long ago", at=_stamp(now - timedelta(days=100)))
        journal.record(self.store, "recall", "last week", at=_stamp(now - timedelta(days=7)))
        self.before = self.uses()

    def tearDown(self) -> None:
        self.store.close()
        super().tearDown()

    def uses(self) -> tuple[int, dict[str, int]]:
        details = self.store.record_details(self.record_id)
        return details["recall_count"], details["uses"]

    def usage_rows(self) -> int:
        return self.store.connection.execute("SELECT COUNT(*) FROM usage").fetchone()[0]

    def summaries(self) -> list[str]:
        return [event["summary"] for event in self.store.events(kinds=["recall"], limit=100)]

    def test_a_session_start_with_learning_off_drops_old_events_and_uses_and_keeps_the_counts(self) -> None:
        self.assertFalse(LearnerSettings().enabled)
        self.assertEqual((3, {"recall": 2, "briefing": 1}), self.before)
        hooks.session_start(json.dumps({"session_id": SESSION, "cwd": str(self.project)}),
                            start_learner=lambda: None)
        self.assertNotIn("long ago", self.summaries())
        self.assertIn("last week", self.summaries())
        self.assertEqual(2, self.usage_rows())  # the recent recall and the briefing just given
        self.assertEqual(self.before, self.uses())

    def test_the_tools_tidy_up_too_but_at_most_once_a_day(self) -> None:
        from knowitall2.mcp_server import handle_once

        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": "printer"}}}
        handle_once({"initialize": {"clientInfo": {"name": "codex"}}, "message": call})
        self.assertNotIn("long ago", self.summaries())
        self.assertEqual(self.before, self.uses())
        journal.record(self.store, "recall", "found later", at="2020-01-01T00:00:00Z")
        handle_once({"initialize": {"clientInfo": {"name": "codex"}}, "message": call})
        self.assertIn("found later", self.summaries())

    def test_uses_not_yet_sent_to_the_server_are_kept(self) -> None:
        from knowitall2.connected import USAGE_CURSOR_KEY

        self.store.set_meta(USAGE_CURSOR_KEY, "1")  # the first old use was sent, the second not yet
        journal.tidy(self.store)
        self.assertEqual(2, self.usage_rows())
        self.assertEqual(self.before, self.uses())


class ProblemTests(HomeTestCase):
    def test_problems_are_kept_newest_first_and_repeats_are_written_once(self) -> None:
        journal.problem("MCP server (codex)", "recall failed: disk I/O error")
        journal.problem("MCP server (codex)", "recall failed: disk I/O error")
        journal.problem("learning", "learning stopped: Not logged in")
        self.assertEqual(["learning", "MCP server (codex)"], [item["source"] for item in journal.read_problems()])

    def test_a_large_file_is_rotated_and_both_are_read(self) -> None:
        with mock.patch.object(journal, "PROBLEMS_MAX_BYTES", 300):
            for number in range(8):
                journal.problem("learning", f"problem number {number}")
        self.assertTrue(journal.problems_path().with_name("problems.1.jsonl").exists())
        self.assertEqual(8, len(journal.read_problems()))

    def test_writing_a_problem_never_raises(self) -> None:
        (self.root / "home").write_text("a file where the data folder should be", encoding="utf-8")
        journal.problem("learning", "cannot be written")
        self.assertEqual([], journal.read_problems())

    def test_the_mcp_server_records_an_unavailable_store(self) -> None:
        def unavailable(agent):
            raise StoreError("the memory store was upgraded to schema 9 by a newer KnowItAll2")

        server = McpServer(memory_factory=unavailable, cwd=self.root)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"clientInfo": {"name": "codex-mcp-client"}}})
        response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "recall", "arguments": {"query": "router"}}})
        self.assertTrue(response["result"]["isError"])
        [entry] = journal.read_problems()
        self.assertEqual("MCP server (codex)", entry["source"])
        self.assertIn("recall failed: the memory store was upgraded", entry["message"])

    def test_the_session_start_hook_records_a_missing_briefing(self) -> None:
        with mock.patch("knowitall2.store.Store.open", side_effect=StoreError("the memory store is unreadable")):
            self.assertEqual("", hooks.session_start("{}", agent="codex", start_learner=lambda: None))
        [entry] = journal.read_problems()
        self.assertEqual(("session start (codex)", "no briefing was added: the memory store is unreadable"),
                         (entry["source"], entry["message"]))


class LearningJournalTests(HomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = make_repository(self.root / "work" / "homelab", "https://gitlab.example.com/me/homelab.git")
        self.log_path = self.root / "claude" / "projects" / "C--work-homelab" / f"{SESSION}.jsonl"
        (LogBuilder(self.project)
         .user("Please check the router release.")
         .tool("t1", "Bash", {"command": "cat /etc/openwrt_release"}, "DISTRIB_RELEASE='23.05.3'")
         .assistant(text("The router runs OpenWrt 23.05.3."))
         .write(self.log_path))
        self.store = Store.in_memory()
        self.memory = Memory(self.store, agent="learner", clock=Clock("2026-09-28T12:00:00Z"), record_events=False)

    def tearDown(self) -> None:
        self.store.close()
        super().tearDown()

    def learn_with(self, extractor) -> LearnReport:
        return learn(
            logs=[self.log_path], state=LearnerState(self.root / "state.json"), settings=LearnerSettings(enabled=True),
            memory_factory=lambda: self.memory, extractor=extractor, dry_run=False, run="r-test",
        )

    def test_each_candidate_is_recorded_with_why_it_was_rejected(self) -> None:
        self.learn_with(FakeExtractor([[
            {"text": "The homelab router runs OpenWrt 23.05.3.", "kind": "fact", "subjects": ["router"],
             "scope": "global", "evidence": "DISTRIB_RELEASE='23.05.3'", "relation": "new", "known_id": ""},
            {"text": "The homelab router has 512 MB of memory.", "kind": "fact", "subjects": ["router"],
             "scope": "global", "evidence": "the router has 512 MB of RAM", "relation": "new", "known_id": ""},
            {"text": "The router admin password is hunter22 for now.", "kind": "fact", "subjects": [],
             "scope": "global", "evidence": "password is hunter22", "relation": "new", "known_id": ""},
        ]]))
        events = {event["summary"]: event for event in self.store.events(kinds=["candidate"], run_id="r-test")}
        self.assertEqual(3, len(events))
        kept = events["The homelab router runs OpenWrt 23.05.3."]
        [record] = self.store.list_active(project_id=None, scope="global", limit=5)
        self.assertEqual(("saved", [record.id], SESSION, "claude-code"),
                         (kept["outcome"], kept["record_ids"], kept["session"], kept["agent"]))
        invented = events["The homelab router has 512 MB of memory."]
        self.assertEqual(("rejected", "evidence not in the session", "the router has 512 MB of RAM"),
                         (invented["outcome"], invented["details"]["reason"], invented["details"]["evidence"]))
        secret = events["A candidate that contained a secret; nothing of it was kept."]
        self.assertEqual("secret", secret["details"]["reason"])
        self.assertNotIn("hunter22", json.dumps(self.store.events()))
        self.assertEqual([], self.store.events(kinds=["remember"]))

    def test_usage_is_added_up_across_calls(self) -> None:
        extractor = FakeExtractor([[]])
        extractor.last_usage = {"input_tokens": 1200, "output_tokens": 80, "cost_usd": 0.004}
        report = self.learn_with(extractor)
        self.assertEqual({"input_tokens": 1200, "output_tokens": 80, "cost_usd": 0.004}, dict(report.usage))

    def test_a_run_is_recorded_once_with_its_numbers(self) -> None:
        report = self.learn_with(FakeExtractor([[
            {"text": "The homelab router runs OpenWrt 23.05.3.", "kind": "fact", "subjects": ["router"],
             "scope": "global", "evidence": "DISTRIB_RELEASE='23.05.3'", "relation": "new", "known_id": ""},
            {"text": "short", "kind": "fact", "subjects": [], "scope": "global", "evidence": "", "relation": "new",
             "known_id": ""},
        ]]))
        engine = mock.Mock(model="haiku")
        engine.name = "claude-cli"
        record_learning_run(self.store, "r-test", report, None, engine=engine, seconds=12.34)
        [event] = self.store.events(kinds=["learning"])
        self.assertEqual(("ok", "r-test"), (event["outcome"], event["run_id"]))
        self.assertEqual("Learned from 1 session: 1 model calls, 1 memories kept, 1 candidates rejected", event["summary"])
        self.assertEqual(({"saved": 1, "rejected (length)": 1}, "claude-cli", 12.3),
                         (event["details"]["outcomes"], event["details"]["engine"], event["details"]["seconds"]))

    def test_a_stopped_run_is_also_a_problem(self) -> None:
        report = LearnReport(logs=3, blocked="the Claude CLI reported: Not logged in")
        engine = mock.Mock(model="haiku")
        engine.name = "claude-cli"
        record_learning_run(self.store, "r-stop", report, None, engine=engine, seconds=1)
        [event] = self.store.events(kinds=["learning"])
        self.assertEqual("stopped", event["outcome"])
        self.assertIn("Not logged in", journal.read_problems()[0]["message"])


class MaintenanceJournalTests(unittest.TestCase):
    def test_each_change_is_recorded_with_the_memory_it_keeps(self) -> None:
        store = Store.in_memory()
        try:
            memory = Memory(store, agent="maintenance", clock=Clock("2026-09-28T12:00:00Z"))
            kept = memory.remember("The NAS nas01 serves backups over SMB.").record
            copy = memory.remember("nas01 serves backups.").record

            class Reviewer:
                last_usage = {"input_tokens": 500}

                def review(self, text):
                    return [{"kind": "duplicate", "keep_id": kept.id, "ids": [copy.id], "reason": "same fact"}]

            report = maintain(memory=memory, reviewer=Reviewer(), state=None, budget=5, dry_run=False, run="r-m")
            [event] = store.events(kinds=["change"])
            self.assertEqual(("merged duplicate", [copy.id, kept.id], "r-m", "same fact"),
                             (event["outcome"], event["record_ids"], event["run_id"], event["details"]["reason"]))
            self.assertEqual({"input_tokens": 500}, dict(report.usage))
        finally:
            store.close()


class UsageTests(unittest.TestCase):
    def test_claude_reports_tokens_and_an_estimated_cost(self) -> None:
        envelope = {"structured_output": {"memories": []}, "total_cost_usd": 0.0123,
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 50}}
        self.assertEqual({"input_tokens": 1010, "output_tokens": 50, "cost_usd": 0.0123},
                         claude_usage(json.dumps(envelope).encode()))
        self.assertIsNone(claude_usage(b"not json"))

        def runner(command, **options):
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope).encode(), b"")

        extractor = ClaudeCliExtractor(Path("claude.exe"), runner=runner)
        extractor.extract(mock.Mock(text="session text", known=[]))
        self.assertEqual(50, extractor.last_usage["output_tokens"])

    def test_codex_reports_a_token_total(self) -> None:
        self.assertEqual({"total_tokens": 12345}, codex_usage(b"done\ntokens used\n12,345\n", b""))
        self.assertIsNone(codex_usage(b"", b""))


class ActivityCommandTests(HomeTestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = main(list(arguments))
        return code, output.getvalue()

    def test_activity_shows_the_journal_and_problems(self) -> None:
        self.assertIn("Nothing has been recorded yet.", self.run_cli("activity")[1])
        project = make_repository(self.root / "alpha")
        self.run_cli("remember", "The NAS is nas01.")
        self.run_cli("recall", "nas", "--project-path", str(project))
        code, output = self.run_cli("activity")
        self.assertEqual(0, code)
        self.assertIn("remember (saved)  cli: The NAS is nas01.", output)
        self.assertIn('recall (found)  cli: Recall "nas" for project alpha: 1 found', output)
        self.assertNotIn("remember", self.run_cli("activity", "--kind", "recall")[1])
        self.assertIn("No problems have been recorded.", self.run_cli("activity", "--problems")[1])
        journal.problem("learning", "learning stopped: Not logged in")
        self.assertIn("learning: learning stopped: Not logged in", self.run_cli("activity", "--problems")[1])
        self.assertIn("Problems in the last 7 days: 1 (see: knowitall2 activity --problems)", self.run_cli("stats")[1])


if __name__ == "__main__":
    unittest.main()
