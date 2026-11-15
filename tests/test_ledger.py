"""The call ledger and the one budget-charge rule (plan step 1 of the joint review, 2026-11-13).

Every production model call is written to the ledger by ``availability.guarded``; each consumer charges its
budget by ``state.chargeable`` only. The matrix here runs every consumer through success, a content failure
(launched, answered badly), an engine block (usage limit), a refusal before launch, and, for the learner, a
provenance refusal, and checks what was charged.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (sets up the source path and a test home)
import test_catalog
import test_commands
import test_documents
import test_finder
import test_learning as fixtures
import test_maintenance
import test_tool_rules
from knowitall2 import journal
from knowitall2.learning import availability, cataloguer, command_tags, documents, finder, ledger, learner, tool_rules
from knowitall2.learning.extractor import ClaudeCliExtractor, CodexCliExtractor, ExtractionError, OUTPUT_SCHEMA
from knowitall2.learning.history import CachedEngine
from knowitall2.learning.state import LearnerState, chargeable, other_calls

LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)
CONTENT = lambda: ExtractionError("the model's answer was not JSON")                      # noqa: E731
BLOCKED = lambda: ExtractionError("You've hit your weekly limit", blocking=True, category="quota")  # noqa: E731
REFUSED = lambda: ExtractionError("paused", blocking=True, category="cooldown", attempted=False)     # noqa: E731
NOT_STARTED = lambda: ExtractionError("not attempted", attempted=False)                   # noqa: E731


def envelope(**extra) -> bytes:
    return json.dumps({"structured_output": {"memories": []}, "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 5},
        "modelUsage": {"claude-sonnet-test": {}}, **extra}).encode()


class HomeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "home")})
        environment.start()
        self.addCleanup(environment.stop)

    def events(self, kind: str | None = None) -> list[dict]:
        return [event for event in ledger.read() if kind is None or event["event"] == kind]

    def engine(self, runner) -> ClaudeCliExtractor:
        engine = ClaudeCliExtractor(Path("fake-claude"), runner=runner)
        engine.check_availability = True
        return engine


class LedgerFileTests(HomeCase):
    def test_contexts_nest_and_never_leak_into_later_calls(self) -> None:
        with ledger.context(purpose="catalog-file", origin="work"):
            with ledger.context(origin="catalog", run="r-1"):
                self.assertEqual({"purpose": "catalog-file", "origin": "catalog", "run": "r-1"}, ledger.current())
            self.assertEqual({"purpose": "catalog-file", "origin": "work"}, ledger.current())
        self.assertEqual({}, ledger.current())
        with self.assertRaises(ValueError):
            with ledger.context(text="a transcript excerpt"):
                pass

    def test_results_keep_counts_and_short_labels_only(self) -> None:
        ledger.result("a-1", filed=3, outcomes={"saved": 2}, note="x" * 200, label="ok", nested={"a": "text"})
        [event] = self.events("result")
        self.assertEqual((3, {"saved": 2}, "ok"), (event["filed"], event["outcomes"], event["label"]))
        self.assertNotIn("note", event)
        self.assertNotIn("nested", event)
        ledger.result(None, filed=1)   # a call with no ledger entry (a simulation) adds nothing
        self.assertEqual(1, len(self.events("result")))

    def test_a_line_cut_short_by_a_crash_does_not_swallow_the_next(self) -> None:
        ledger.path().parent.mkdir(parents=True)
        ledger.path().write_text('{"v": 2, "event": "start", "id": "a-old"', encoding="utf-8")
        ledger.result("a-new", filed=1)
        self.assertEqual(["a-new"], [event["id"] for event in self.events()])

    def test_lines_written_before_the_ledger_are_read_as_legacy_attempts(self) -> None:
        now = datetime.now(timezone.utc)
        ledger.path().parent.mkdir(parents=True)
        ledger.path().write_text(json.dumps({"backend": "claude-cli", "model": "sonnet", "started_at": now.isoformat(),
                                             "completed_at": now.isoformat(), "outcome": "ok", "category": "ok",
                                             "usage": None}) + "\n", encoding="utf-8")
        [event] = self.events()
        self.assertEqual("legacy", event["event"])
        self.assertEqual({"unlabelled": {"legacy": 1, "ok": 1}}, ledger.summary(now - timedelta(days=1)))

    def test_pruning_keeps_a_calls_events_together(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=ledger.KEPT_DAYS + 3)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        lines = [{"v": 2, "event": "start", "id": "a-gone", "at": old},
                 {"v": 2, "event": "end", "id": "a-gone", "at": old, "outcome": "ok", "launch": "confirmed"},
                 {"v": 2, "event": "start", "id": "a-long", "at": old},
                 {"v": 2, "event": "end", "id": "a-long", "at": recent, "outcome": "ok", "launch": "confirmed"}]
        ledger.path().parent.mkdir(parents=True)
        ledger.path().write_text("".join(json.dumps(line) + "\n" for line in lines) + "not json\n", encoding="utf-8")
        ledger.result("a-new", filed=1)
        self.assertEqual(["a-long", "a-long", "a-new"], [event["id"] for event in self.events()])

    def test_a_damaged_first_line_never_stops_a_call(self) -> None:
        for first in ("[]", "null", "42", '"text"', "{not json"):
            ledger.path().parent.mkdir(parents=True, exist_ok=True)
            ledger.path().write_text(first + "\n", encoding="utf-8")
            ran = []
            engine = self.engine(lambda command, **kwargs: ran.append(1) or subprocess.CompletedProcess(
                command, 0, envelope(), b""))
            self.assertEqual({"memories": []}, engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test"), first)
            self.assertEqual([1], ran, first)
            self.assertEqual(["start", "end"], [event["event"] for event in self.events()], first)

    def test_the_summary_joins_a_call_across_the_window_and_reads_charges_back(self) -> None:
        now = datetime.now(timezone.utc)
        before, inside = (now - timedelta(hours=2)).isoformat(), now.isoformat()
        rows = [
            {"event": "start", "id": "a-cross", "at": before, "purpose": "transcript-final"},
            {"event": "end", "id": "a-cross", "at": inside, "outcome": "ok", "launch": "confirmed"},
            {"event": "charge", "id": "a-cross", "at": inside},
            {"event": "start", "id": "a-lost", "at": inside, "purpose": "transcript-final"},
            {"event": "end", "id": "a-lost", "at": inside, "outcome": "failed", "launch": "confirmed"},
            {"event": "start", "id": "a-quota", "at": inside, "purpose": "transcript-final"},
            {"event": "end", "id": "a-quota", "at": inside, "outcome": "blocked", "launch": "confirmed"},
            {"backend": "claude-cli", "started_at": inside, "completed_at": inside, "outcome": "failed"},
        ]
        ledger.path().parent.mkdir(parents=True)
        ledger.path().write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        summary = ledger.summary(now - timedelta(hours=1))
        self.assertEqual({"launched": 3, "ok": 1, "failed": 1, "blocked": 1, "charged": 1, "charge missing": 1,
                          "exempt": 1}, summary["transcript-final"])
        self.assertEqual({"legacy": 1, "failed": 1}, summary["unlabelled"])   # no launch claimed for old lines

    def test_a_call_whose_start_was_not_recorded_still_counts(self) -> None:
        engine = self.engine(lambda command, **kwargs: subprocess.CompletedProcess(command, 0, envelope(), b""))
        real = ledger._append
        with mock.patch.object(ledger, "_append", side_effect=lambda event: None if event["event"] == "start"
                               else real(event)):
            engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test")
        ledger.charge(engine.last_attempt, origin="work", outcome="ok", session="s-1")
        self.assertEqual({"unlabelled": {"incomplete": 1, "launched": 1, "ok": 1, "charged": 1}},
                         ledger.summary(LONG_AGO))

    def test_an_odd_value_is_written_as_text_and_never_raises(self) -> None:
        with ledger.context(run=object()):
            ledger.result("a-1", filed=1)
        ledger.result(mock.Mock(), filed=1)   # not a ledger id (an engine stand-in's attribute): nothing written
        self.assertEqual(["a-1"], [event["id"] for event in self.events()])

    def test_a_ledger_that_cannot_be_written_never_stops_the_call(self) -> None:
        engine = self.engine(lambda command, **kwargs: subprocess.CompletedProcess(command, 0, envelope(), b""))
        with mock.patch.object(ledger, "_prune", side_effect=OSError("disk full")):
            self.assertEqual({"memories": []}, engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test"))
        self.assertTrue(any("ledger is incomplete" in str(item.get("message")) for item in journal.read_problems()))


class GuardTests(HomeCase):
    def test_an_answered_call_is_one_start_and_one_confirmed_end_with_what_it_was_for(self) -> None:
        engine = self.engine(lambda command, **kwargs: subprocess.CompletedProcess(command, 0, envelope(), b""))
        with ledger.context(purpose="transcript-quick", origin="work", session="s-1", source="codex"):
            engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test")
        [start] = self.events("start")
        [end] = self.events("end")
        self.assertEqual(start["id"], end["id"])
        self.assertEqual(start["id"], engine.last_attempt)
        self.assertEqual(("transcript-quick", "work", "s-1", "codex", "sonnet", "claude-cli"),
                         (start["purpose"], start["origin"], start["session"], start["source"],
                          start["model_configured"], start["actual_engine"]))
        self.assertEqual(("ok", "confirmed", "claude-sonnet-test"), (end["outcome"], end["launch"], end["model_resolved"]))
        # Cached input is kept apart as well as in the total.
        self.assertEqual((1010, 1000), (end["usage"]["input_tokens"], end["usage"]["cache_read_input_tokens"]))
        self.assertEqual({"transcript-quick": {"launched": 1, "ok": 1, "charge missing": 1}},
                         ledger.summary(datetime.now(timezone.utc) - timedelta(hours=1)))

    def test_an_engine_that_cannot_start_is_no_launch_and_no_charge(self) -> None:
        def runner(command, **kwargs):
            raise FileNotFoundError("claude")
        engine = self.engine(runner)
        with self.assertRaises(ExtractionError) as error:
            engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test")
        self.assertFalse(error.exception.attempted)
        self.assertFalse(chargeable(error.exception))
        self.assertEqual(("none", "blocked"), (self.events("end")[0]["launch"], self.events("end")[0]["outcome"]))

    def test_an_engine_that_fails_after_it_started_was_launched(self) -> None:
        def runner(command, **kwargs):
            error = OSError("the pipe broke")
            error.engine_launched = True
            raise error
        with self.assertRaises(ExtractionError) as error:
            self.engine(runner).run("x", schema=OUTPUT_SCHEMA, system_prompt="test")
        self.assertTrue(error.exception.attempted)
        self.assertEqual("confirmed", self.events("end")[0]["launch"])

    def test_the_runner_marks_errors_after_the_process_started(self) -> None:
        import sys
        from knowitall2.learning.runner import run_bounded

        with mock.patch.object(subprocess.Popen, "communicate", side_effect=OSError("read failed")):
            with self.assertRaises(OSError) as error:
                run_bounded([sys.executable, "-c", "pass"], timeout=30)
        self.assertTrue(error.exception.engine_launched)

    def test_a_cleanup_error_never_replaces_the_first_or_loses_its_launch(self) -> None:
        import sys
        from knowitall2.learning.runner import run_bounded

        first = OSError("read failed")
        for failures in ([first, OSError("cleanup failed")],
                         [subprocess.TimeoutExpired("engine", 1), OSError("cleanup failed")]):
            with mock.patch.object(subprocess.Popen, "communicate", side_effect=failures):
                with self.assertRaises((OSError, subprocess.TimeoutExpired)) as error:
                    run_bounded([sys.executable, "-c", "pass"], timeout=30)
            self.assertIs(failures[0], error.exception)
            self.assertTrue(error.exception.engine_launched)

    def test_the_codex_engine_reports_launches_and_its_model(self) -> None:
        def answer(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text('{"memories": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"model: gpt-test\ntokens used: 1,234\n")
        engine = CodexCliExtractor(Path("fake-codex"), runner=answer)
        engine.check_availability = True
        with ledger.context(purpose="transcript-quick", source="codex"):
            engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test")
        [start], [end] = self.events("start"), self.events("end")
        self.assertEqual(("codex-cli", "codex-cli", "codex"),
                         (start["preferred_engine"], start["actual_engine"], start["source"]))
        self.assertEqual(("ok", "confirmed", "gpt-test", 1234),
                         (end["outcome"], end["launch"], end["model_resolved"], end["usage"]["total_tokens"]))

        def missing(command, **kwargs):
            raise FileNotFoundError("codex")
        engine = CodexCliExtractor(Path("fake-codex"), runner=missing)
        engine.check_availability = True
        with self.assertRaises(ExtractionError) as error:
            engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test")
        self.assertFalse(error.exception.attempted)
        self.assertEqual("none", self.events("end")[-1]["launch"])

    def test_a_lean_codex_call_carries_the_learning_prompt_as_its_base_instructions(self) -> None:
        seen = {}

        def answer(command, **kwargs):
            setting = next(item for item in command if item.startswith("model_instructions_file="))
            seen["base"] = Path(json.loads(setting.split("=", 1)[1])).read_text(encoding="utf-8")
            seen["developer"] = next(item for item in command if item.startswith("developer_instructions="))
            seen["command"] = command
            Path(command[command.index("-o") + 1]).write_text('{"memories": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        engine = CodexCliExtractor(Path("fake-codex"), runner=answer, lean=True)
        engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="THE LEARNING PROMPT")
        self.assertEqual("THE LEARNING PROMPT", seen["base"])
        self.assertNotIn("THE LEARNING PROMPT", seen["developer"])
        self.assertIn("project_doc_max_bytes=0", seen["command"])
        plain = {}

        def answer_plain(command, **kwargs):
            plain["command"] = command
            Path(command[command.index("-o") + 1]).write_text('{"memories": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        engine = CodexCliExtractor(Path("fake-codex"), runner=answer_plain, lean=False)
        engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="THE LEARNING PROMPT")
        self.assertFalse(any(item.startswith("model_instructions_file=") for item in plain["command"]))

    def test_a_timeout_ran_but_is_charged_to_no_budget(self) -> None:
        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)
        with self.assertRaises(ExtractionError) as error:
            self.engine(runner).run("x", schema=OUTPUT_SCHEMA, system_prompt="test")
        self.assertFalse(chargeable(error.exception))
        [end] = self.events("end")
        self.assertEqual(("confirmed", "blocked", "timeout"), (end["launch"], end["outcome"], end["category"]))

    def test_a_crash_inside_the_engine_leaves_its_launch_unknown(self) -> None:
        def runner(command, **kwargs):
            raise RuntimeError("the process wrapper broke")
        with self.assertRaises(RuntimeError):
            self.engine(runner).run("x", schema=OUTPUT_SCHEMA, system_prompt="test")
        [end] = self.events("end")
        self.assertEqual(("unknown", "error"), (end["launch"], end["outcome"]))
        self.assertEqual(1, ledger.summary(LONG_AGO)["unlabelled"]["uncertain"])

    def test_a_paused_backend_is_a_deferral_and_reports_no_earlier_usage(self) -> None:
        answers = [subprocess.CompletedProcess([], 0, envelope(), b""),
                   subprocess.CompletedProcess([], 1, json.dumps({"is_error": True,
                                                                  "result": "You've hit your weekly limit"}).encode(), b"")]
        engine = self.engine(lambda command, **kwargs: answers.pop(0))
        engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test")
        with self.assertRaises(ExtractionError):
            engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test")   # the usage limit: launched, blocked
        with self.assertRaises(ExtractionError) as error:
            engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test")   # paused: refused before launch
        self.assertEqual("cooldown", error.exception.category)
        self.assertIsNone(engine.last_usage)
        [deferred] = self.events("deferred")
        self.assertEqual(deferred["id"], engine.last_attempt)
        self.assertEqual(2, len(self.events("start")))

    def test_a_start_with_no_end_is_counted_as_uncertain(self) -> None:
        with ledger.context(purpose="transcript-final"):
            ledger.start(Engine())
        self.assertEqual({"transcript-final": {"uncertain": 1, "charge unknown": 1}}, ledger.summary(LONG_AGO))


class ChargeRuleTests(unittest.TestCase):
    def test_only_answers_and_launched_content_failures_are_charged(self) -> None:
        self.assertTrue(chargeable(None))
        self.assertTrue(chargeable(CONTENT()))
        for error in (BLOCKED(), REFUSED(), NOT_STARTED(), RuntimeError("bug"),
                      ExtractionError("timed out", blocking=True, category="timeout")):
            self.assertFalse(chargeable(error), error)


class FakeExtractor(fixtures.FakeEngine):
    """The learner's engine, naming each call as the ledger would (``prefix`` tells two engines' calls apart)."""

    def __init__(self, results=(), *, prefix="a", **options) -> None:
        super().__init__(results, **options)
        self.prefix = prefix

    def extract(self, dossier):
        self.last_attempt = f"{self.prefix}-{len(self.dossiers)}"
        return super().extract(dossier)

    def run(self, text, *, schema, system_prompt):
        self.last_attempt = f"{self.prefix}-run-{len(self.inputs)}"
        return super().run(text, schema=schema, system_prompt=system_prompt)


class LearnerChargeTests(HomeCase):
    def learning(self):
        fixture = fixtures.TwoPassTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def charges(self, t) -> list[tuple[str, str]]:
        return [(call["outcome"], call.get("origin"), call.get("attempt")) for call in t.state.calls]

    def test_quick_pass_charges_answers_and_failed_calls_but_not_blocks_or_refusals(self) -> None:
        for error, expected in ((CONTENT(), [("failed", "work", "a-0")]), (BLOCKED(), []), (REFUSED(), [])):
            t = self.learning()
            t.sample_log().write(t.log_path, idle=False)
            t.learn(FakeExtractor([error]), quick=True)
            self.assertEqual(expected, self.charges(t), error)
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(FakeExtractor([[t.DNSMASQ]]), quick=True)
        self.assertEqual([("ok", "work", "a-0")], self.charges(t))

    def final_pass(self, error):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(FakeExtractor([[t.DNSMASQ]]), quick=True)
        fixtures.quiet_longer(t.log_path)
        t.state.calls.clear()
        return t, t.learn(FakeExtractor(finals=[error]))

    def test_final_pass_refused_before_any_call_is_a_deferral_not_a_charge(self) -> None:
        t, report = self.final_pass(ExtractionError("source review is pending for k-1", attempted=False))
        self.assertEqual([], t.state.calls)
        self.assertEqual(0, report.calls)
        [deferred] = self.events("deferred")
        self.assertEqual(("transcript-final", "provenance", "learning"),
                         (deferred["purpose"], deferred["category"], deferred["origin"]))
        t, report = self.final_pass(CONTENT())
        self.assertEqual([("failed", "learning", "a-run-0")], self.charges(t))
        t, report = self.final_pass(BLOCKED())
        self.assertEqual([], t.state.calls)

    def test_a_charge_is_in_the_ledger_as_it_is_made(self) -> None:
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(FakeExtractor([[t.DNSMASQ]]), quick=True)
        self.assertEqual([("a-0", "work", "ok")],
                         [(event["id"], event["origin"], event["outcome"]) for event in self.events("charge")])

    def stopped(self, t, at: str, engine) -> None:
        """A learning run killed at ``at``: nothing after it runs, including the budget file's save."""

        class Killed(BaseException):
            pass

        target = {"publish": (learner, "publish_with_ids"), "result": (ledger, "result"),
                  "finish": (learner, "_finish")}[at]
        with mock.patch.object(*target, side_effect=Killed), mock.patch.object(LearnerState, "save"):
            with self.assertRaises(Killed):
                t.learn(engine, quick=at != "finish")
        t.state = LearnerState(t.root / "state.json")   # the restart reads what was saved

    def test_a_quick_pass_killed_at_each_boundary_resumes_without_duplicates(self) -> None:
        for at in ("publish", "result"):
            t = self.learning()
            t.sample_log().write(t.log_path, idle=False)
            self.stopped(t, at, FakeExtractor([[t.DNSMASQ, t.ROUTER]], prefix=f"{at}1"))
            t.learn(FakeExtractor([[t.DNSMASQ, t.ROUTER]], prefix=f"{at}2"), quick=True)
            texts = sorted(row.text for row in t.store.list_active(project_id=None, scope="all", limit=50))
            self.assertEqual(sorted([t.DNSMASQ["text"], t.ROUTER["text"]]), texts, at)
            # The call that ran before the stop stays charged in the ledger; the budget file has the rerun's.
            ledger_charges = [event["id"] for event in self.events("charge") if event["id"].startswith(at)]
            self.assertEqual([f"{at}1-0", f"{at}2-0"], ledger_charges, at)
            self.assertEqual([f"{at}2-0"], [call.get("attempt") for call in t.state.calls], at)

    def test_a_final_pass_killed_after_it_applied_settles_once(self) -> None:
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(FakeExtractor([[t.DNSMASQ]]), quick=True)
        [record] = t.provisional()
        fixtures.quiet_longer(t.log_path)
        self.stopped(t, "finish", FakeExtractor(finals=[{"reviews": [fixtures.review("p1", "confirm")],
                                                          "memories": []}], prefix="f1"))
        self.assertEqual([], t.provisional())   # the review was applied before the stop
        report = t.learn(FakeExtractor(prefix="f2"))
        self.assertEqual([], report.failed)
        self.assertEqual([record.id], [row.id for row in t.store.list_active(project_id=None, scope="all", limit=50)])

    def test_an_override_run_leaves_the_background_allowance_untouched(self) -> None:
        t = self.learning()
        t.sample_log().write(t.log_path)
        from knowitall2.learning.learner import learn

        learn(logs=[t.log_path], state=t.state, settings=t.settings, memory_factory=lambda: t.memory,
              extractor=FakeExtractor([[t.DNSMASQ]]), dry_run=False, origin="override")
        self.assertTrue(t.state.calls)
        self.assertEqual({"override"}, {call["origin"] for call in t.state.calls})
        self.assertEqual(0, t.state.limited_calls_since(LONG_AGO))
        self.assertEqual(len(t.state.calls), t.state.calls_since(LONG_AGO))

    def test_the_ignore_daily_limit_command_charges_its_sweep_as_override(self) -> None:
        from knowitall2.learning import command
        from knowitall2.learning.learner import LearnReport

        seen = []
        requested = mock.Mock(saved=0, blocked=None)
        with mock.patch.object(command, "learn", side_effect=lambda **kwargs: seen.append(
                     (kwargs.get("origin"), ledger.current().get("mode"))) or LearnReport(logs=0)), \
                mock.patch.object(command, "learn_requests", return_value=requested), \
                mock.patch.object(command, "session_logs", return_value=[]), \
                mock.patch.object(command, "maintenance_turn") as turn:
            turn.return_value.__enter__.return_value = False
            command._learn_once(command.LearnerSettings(enabled=True), mock.Mock(), sweep=True, override=True)
            command._learn_once(command.LearnerSettings(enabled=True), mock.Mock(), sweep=True)
        self.assertEqual([("override", "override"), (None, "background")], seen)


class CatalogChargeTests(test_catalog.CatalogTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = LearnerState(self.root / "state.json")

    def run_with(self, *replies):
        self.save("vCenter vc01 is at 10.20.30.16 and manages two ESXi hosts.", source="observed")
        engine = test_catalog.FakeEngine(file=list(replies))
        return cataloguer.run_catalog(memory=cataloguer.Memory(self.store, agent="catalog", clock=self.clock),
                                      engine=engine, budget=1, state=self.state)

    def test_only_launched_failures_are_charged(self) -> None:
        for reply, expected in ((CONTENT(), ["failed"]), (BLOCKED(), []), (NOT_STARTED(), [])):
            self.state.calls.clear()
            report = self.run_with(reply)
            self.assertEqual(expected, [call["outcome"] for call in self.state.calls], reply)
            self.assertEqual(len(expected), report.calls)

    def test_a_batch_refused_until_it_is_filed_as_general_is_reported(self) -> None:
        self.save("The access point join fails until the controller certificate is issued again.")
        results = []
        for _ in range(cataloguer.SKIPS_BEFORE_GENERAL):
            engine = test_catalog.FakeEngine(file=[CONTENT()])
            engine.last_attempt = f"a-refused-{len(results)}"
            with mock.patch.object(ledger, "result") as result:
                report = cataloguer.run_catalog(memory=cataloguer.Memory(self.store, agent="catalog", clock=self.clock),
                                                engine=engine, budget=1, state=self.state)
            results.append(result.call_args_list[0])
        self.assertEqual(0, self.store.count_unnoted())
        self.assertEqual(1, report.filed_general)
        self.assertEqual([(1, 0), (1, 1)], [(call.kwargs["refused"], call.kwargs["filed_general"]) for call in results])
        self.assertEqual(0, results[-1].kwargs["latency_max_s"])

    def test_a_filing_call_reports_its_batch_and_how_long_memories_waited(self) -> None:
        record = self.save("vCenter vc01 is at 10.20.30.16 and manages two ESXi hosts.", source="observed")
        engine = test_catalog.FakeEngine(file=[{"notes": [test_catalog.note(record, "vCenter's address", name="vCenter")]}])
        engine.last_attempt = "a-file"
        with mock.patch.object(ledger, "result") as result:
            report = cataloguer.run_catalog(memory=cataloguer.Memory(self.store, agent="catalog", clock=self.clock),
                                            engine=engine, budget=2, state=self.state)
        self.assertEqual(1, report.filed)
        call = result.call_args_list[0]   # filing; describing the new system follows
        self.assertEqual("a-file", call.args[0])
        self.assertEqual((1, 1), (call.kwargs["input"], call.kwargs["filed"]))
        self.assertIsNotNone(call.kwargs["latency_max_s"])
        self.assertEqual("a-file", self.state.calls[-1]["attempt"])
        self.assertEqual(0, call.kwargs["latency_max_s"])
        details = report.details()
        self.assertEqual((1, 0), (details["queue_before"]["unfiled"], details["queue_after"]["unfiled"]))
        self.assertIn("oldest_unfiled_seconds", details["queue_before"])
        later = self.save("The NAS is nas01 and holds the backups.", source="observed")
        engine = test_catalog.FakeEngine(
            file=[{"notes": [test_catalog.note(later, "The NAS", name="NAS")]}],
            during={"file": lambda: setattr(self.clock, "value", "2026-09-28T12:05:00Z")})
        engine.last_attempt = "a-later"
        with mock.patch.object(ledger, "result") as result:
            cataloguer.run_catalog(memory=cataloguer.Memory(self.store, agent="catalog", clock=self.clock),
                                   engine=engine, budget=1, state=self.state)
        self.assertEqual(300, result.call_args_list[0].kwargs["latency_max_s"])   # the call's own time counts


class MaintenanceChargeTests(test_maintenance.MaintenanceTestCase):
    def test_only_launched_failures_are_charged(self) -> None:
        for text in ("The NAS is nas01.", "The router is rtr01."):
            self.save(text)
        for error, expected in ((BLOCKED(), 0), (NOT_STARTED(), 0), (CONTENT(), 1)):
            self.state.calls.clear()
            self.run_maintenance(test_maintenance.FakeReviewer([error]))
            self.assertEqual(expected, self.state.calls_since(LONG_AGO), error)


class CommandTagChargeTests(test_commands.CommandTestCase):
    def test_only_launched_failures_are_charged(self) -> None:
        self.lesson("Lesson: the deploy script needs --force here, or it stops halfway.")
        state = LearnerState(self.home / "learner" / "state.json")
        for error, expected in ((BLOCKED(), 0), (REFUSED(), 0), (RuntimeError("engine down"), 0), (CONTENT(), 1)):
            state.calls.clear()
            command_tags.tag_commands(self.store, test_commands.FakeEngine(error), budget=1, now="now", state=state)
            self.assertEqual(expected, state.calls_since(LONG_AGO), error)


class ToolRuleChargeTests(test_tool_rules.RuleTestCase):
    def test_only_launched_failures_are_charged(self) -> None:
        self.heredoc_lessons()
        state = LearnerState(self.home / "learner" / "state.json")
        for error, expected in ((BLOCKED(), 0), (REFUSED(), 0), (CONTENT(), 1)):
            state.calls.clear()
            self.write(error, state=state)
            self.assertEqual(expected, state.calls_since(LONG_AGO), error)


class DocumentChargeTests(unittest.TestCase):
    # The documents tests' project and store, without running their tests again here.
    setUp = test_documents.DocumentsTests.setUp
    tearDown = test_documents.DocumentsTests.tearDown

    def test_only_launched_failures_are_charged(self) -> None:
        dossiers = documents.build(self.project, self.project_root, documents.project_documents(self.project_root))
        for number, (error, expected) in enumerate(((BLOCKED(), []), (REFUSED(), []), (CONTENT(), ["failed"]))):
            state = LearnerState(self.root / f"state-{number}.json")
            documents.learn(self.memory, state, fixtures.FakeExtractor([error]), dossiers, max_calls=5)
            self.assertEqual(expected, [call["outcome"] for call in state.calls], error)



class FinderChargeTests(test_finder.FinderTestCase):
    def charges(self) -> list[str]:
        return [call["outcome"] for call in other_calls(self.root / "home" / "learner")]

    def test_a_failure_is_charged_only_when_the_engine_was_launched(self) -> None:
        for error in (BLOCKED(), REFUSED()):
            finder.run(self.system_id, engine=test_finder.FakeEngine(error), store=self.store)
        self.assertEqual([], self.charges())
        finder.run(self.system_id, engine=test_finder.FakeEngine(CONTENT()), store=self.store)
        self.assertEqual(["failed"], self.charges())


class Engine:
    """A plain stand-in engine: a name and a model, as the ledger and history repair read them."""

    name, model = "claude-cli", "sonnet"

    def __init__(self, answer=None, attempt="a-1") -> None:
        self.answer, self.last_attempt, self.last_usage, self.calls = answer, attempt, {}, 0

    def run(self, text, *, schema, system_prompt):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class HistoryChargeTests(HomeCase):
    def adapter(self, engine, name="h") -> CachedEngine:
        manifest = {"id": "h-1", "session": "s-1", "max_calls": 5}
        self.state = LearnerState(self.root / f"{name}-state.json")
        return CachedEngine(engine, self.root / f"{name}-checkpoint.json", manifest, self.state)

    def test_failures_follow_the_shared_rule_and_a_cached_step_is_never_charged(self) -> None:
        for number, (error, expected, reserved) in enumerate(((CONTENT(), ["failed"], 1), (BLOCKED(), [], 1),
                                                              (NOT_STARTED(), [], 0))):
            adapter = self.adapter(Engine(error), name=f"case{number}")
            with self.assertRaises(ExtractionError):
                adapter.run("x", schema={}, system_prompt="p")
            self.assertEqual(expected, [call["outcome"] for call in self.state.calls], error)
            self.assertEqual(reserved, adapter.data["calls"])
        engine = Engine({"reviews": []}, attempt="a-2")
        adapter = self.adapter(engine)
        adapter.run("x", schema={}, system_prompt="p")
        adapter.run("x", schema={}, system_prompt="p")
        self.assertEqual([("ok", "a-2")], [(call["outcome"], call["attempt"]) for call in self.state.calls])
        self.assertEqual(1, engine.calls)

    def test_a_refusal_after_an_answered_step_reports_no_usage_of_its_own(self) -> None:
        engine = Engine({"reviews": []}, attempt="a-4")
        engine.last_usage = {"input_tokens": 5}
        adapter = self.adapter(engine)
        adapter.manifest["max_calls"] = 1
        adapter.run("x", schema={}, system_prompt="p")
        self.assertEqual({"input_tokens": 5}, adapter.last_usage)
        with self.assertRaises(ExtractionError):
            adapter.run("y", schema={}, system_prompt="p")   # the cap: refused before launch
        self.assertEqual(({}, None), (adapter.last_usage, adapter.last_attempt))

    def test_a_stop_between_answer_and_cache_charges_the_call_that_ran(self) -> None:
        adapter = self.adapter(Engine({"reviews": []}, attempt="a-3"))
        saves = []

        def save():
            saves.append(1)
            if len(saves) == 2:   # the reservation is saved, then the answer: stop before the answer is kept
                raise KeyboardInterrupt
        with mock.patch.object(adapter, "_save", side_effect=save):
            with self.assertRaises(KeyboardInterrupt):
                adapter.run("x", schema={}, system_prompt="p")
        self.assertEqual(["ok"], [call["outcome"] for call in LearnerState(self.root / "h-state.json").calls])


if __name__ == "__main__":
    unittest.main()
