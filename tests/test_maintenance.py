import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository

from knowitall2 import review
from knowitall2.cli import main
from knowitall2.learning import moments
from knowitall2.learning.extractor import ClaudeCliExtractor, ExtractionError
from knowitall2.learning.maintenance import (
    REVIEW_PROMPT,
    ClaudeCliReviewer,
    build_groups,
    cluster,
    maintain,
    plan,
    render_group,
)
from knowitall2.learning.state import LearnerState, RunLock
from knowitall2.memory import Memory, MemoryInputError
from knowitall2.store import Store


class FakeReviewer:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.texts = []

    def review(self, text):
        self.texts.append(text)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class MaintenanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = make_repository(self.root / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.store = Store.in_memory()
        self.clock = Clock("2026-09-27T12:00:00Z")
        self.memory = Memory(self.store, agent="maintenance", clock=self.clock)
        self.state = LearnerState(self.root / "state.json")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def save(self, text: str, **options):
        return self.memory.remember(text, project_path=self.project, **options).record

    def run_maintenance(self, reviewer, *, budget: int = 10):
        return maintain(memory=self.memory, reviewer=reviewer, state=self.state, budget=budget, dry_run=False)

    def status(self, record_id: str) -> str:
        return self.store.get(record_id).status


class PlanTests(MaintenanceTestCase):
    def test_a_small_scope_is_one_group_and_a_lone_memory_is_not_reviewed(self) -> None:
        for text in ("The NAS is nas01.", "The router is rtr01.", "The printer is prn01."):
            self.save(text)
        self.save("We release from the main branch.", kind="decision")
        planned = [(group.label, len(group.members), status) for group, status in plan(self.memory)]
        self.assertEqual([("global", 3, "review"), ("project alpha", 1, "too small")], planned)

    def test_large_scopes_are_clustered_by_shared_words(self) -> None:
        routers = [self.save(f"The homelab router runs dnsmasq for DHCP on VLAN {number}.").id for number in range(10, 20)]
        pools = [self.save(f"The NAS pool tank{number} uses ZFS mirrors with {number} disks.").id for number in range(10)]
        for word in ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
                     "november oscar papa quebec romeo sierra tango").split():
            self.save(f"The {word} service keeps its logs in /var/log/{word}.")
        groups = build_groups(self.memory)
        self.assertEqual(40, sum(len(group.members) for group in groups))
        self.assertTrue(all(len(group.members) <= 30 for group in groups))
        for related in (routers, pools):
            homes = {index for index, group in enumerate(groups) for record in group.members if record.id in related}
            self.assertEqual(1, len(homes))

    def test_cluster_keeps_small_inputs_whole(self) -> None:
        records = [self.save(f"Fact number {word} about things.") for word in ("one", "two")]
        self.assertEqual([sorted(records, key=lambda record: (record.created_at, record.id))], cluster(records))

    def test_the_model_sees_ids_verification_and_dates(self) -> None:
        self.save("The NAS is nas01.", source="observed", subjects=["NAS"])
        self.save("Always tag releases.", kind="rule", source="user")
        [group] = build_groups(self.memory)
        rendered = render_group(group)
        self.assertIn("Memories in scope global (2), oldest first:", rendered)
        self.assertRegex(rendered, r"- \[k-[0-9a-f]+\] fact, observed in tool output, saved 2026-09-27, about NAS: ")
        self.assertIn("rule, stated by the user", rendered)


class GateTests(MaintenanceTestCase):
    def test_the_fullest_copy_replaces_duplicates_with_no_stronger_evidence(self) -> None:
        full = self.save("The NAS is nas01 at 10.0.0.5, reached over SSH.", source="observed")
        short = self.save("nas01 (10.0.0.5) is the NAS.")
        self.save("The router is rtr01.")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": full.id, "ids": [f"[{short.id}]"], "reason": "Same NAS address."},
        ]]))
        self.assertEqual({"merged duplicate": 1}, dict(report.changes))
        merged = self.store.get(short.id)
        self.assertEqual(("superseded", full.id), (merged.status, merged.superseded_by))
        self.assertEqual(f"maintenance: duplicate of [{full.id}]: Same NAS address.", merged.retired_reason)
        self.assertIn(f"[{short.id}]", self.memory.history())
        self.assertIn("is also still active", self.memory.restore(short.id))
        restored = self.store.get(short.id)
        self.assertEqual(("active", "user_stated"), (restored.status, restored.verification))

    def test_a_better_verified_copy_is_never_retired_by_a_fuller_one(self) -> None:
        full = self.save("The NAS is nas01 at 10.0.0.5, reached over SSH as admin.")
        stated = self.save("The NAS is nas01.", source="user")
        also = self.save("nas01 is the NAS.")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": full.id, "ids": [stated.id, also.id], "reason": "Same NAS."},
        ]]))
        self.assertEqual({"kept (better verified)": 1, "merged duplicate": 1}, dict(report.changes))
        self.assertEqual(("active", "active", "superseded"), tuple(map(self.status, (full.id, stated.id, also.id))))

    def test_outdated_memories_follow_the_evidence_gates(self) -> None:
        old_seen = self.save("The CI runner is ci01.", source="observed")
        new_guess = self.save("The CI runner moved to ci02.")
        old_guess = self.save("The wiki is at wiki-old.example.test.")
        new_seen = self.save("The wiki is at wiki.example.test.", source="observed")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "outdated", "keep_id": new_guess.id, "ids": [old_seen.id], "reason": "Moved."},
            {"kind": "outdated", "keep_id": new_seen.id, "ids": [old_guess.id], "reason": "Moved."},
        ]]))
        self.assertEqual({"question": 1, "replaced outdated": 1}, dict(report.changes))
        self.assertEqual(("active", "active"), (self.status(old_seen.id), self.status(new_guess.id)))
        self.assertEqual("superseded", self.status(old_guess.id))
        [question] = self.store.open_questions(limit=5)
        self.assertEqual([old_seen.id, new_guess.id], question["record_ids"])

    def test_only_the_user_retires_their_own_words(self) -> None:
        stated = self.save("I am rebuilding the NAS this week.", source="user")
        status = self.save("The VPN is currently disconnected.")
        lesson = self.save("Restarting dnsmasq fixed DHCP.", kind="lesson")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "snapshot", "keep_id": "", "ids": [stated.id, status.id, lesson.id], "reason": "Temporary."},
        ]]))
        self.assertEqual({"question": 1, "retired snapshot": 1, "kept (lesson)": 1}, dict(report.changes))
        self.assertEqual(("active", "retired", "active"), tuple(map(self.status, (stated.id, status.id, lesson.id))))
        [question] = self.store.open_questions(limit=5)
        self.assertEqual("still_true", question["kind"])
        self.assertIn("Forgot", review.answer(self.memory, question["id"], "forget"))
        self.assertEqual("retired", self.status(stated.id))

    def test_the_users_statements_are_merged_or_replaced_only_when_nearly_identical(self) -> None:
        tag = self.save("Always tag releases before deploying them.", kind="rule", source="user")
        tag_again = self.save("Tag every release in Git.", kind="rule", source="user")
        old_host = self.save("Deploy from the build01 host.", kind="rule", source="user")
        new_host = self.save("Deploy from the build02 host from now on.", kind="rule", source="user")
        signed_tags = self.save("Sign every tag, with GPG", kind="rule", source="user")
        noted = self.save("Sign every tag with GPG.", kind="note", source="user")  # the same words, but a note
        sign = self.save("Sign every commit with GPG.", kind="rule", source="user")
        sign_again = self.save("Sign every commit, with GPG", kind="rule", source="user")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": tag.id, "ids": [tag_again.id], "reason": "Same rule."},
            {"kind": "outdated", "keep_id": new_host.id, "ids": [old_host.id], "reason": "Moved."},
            {"kind": "duplicate", "keep_id": noted.id, "ids": [signed_tags.id], "reason": "Same."},
            {"kind": "duplicate", "keep_id": sign.id, "ids": [sign_again.id], "reason": "Same rule."},
        ]]))
        self.assertEqual({"question": 3, "merged duplicate": 1}, dict(report.changes))
        for record in (tag, tag_again, old_host, new_host, signed_tags, noted, sign):
            self.assertEqual("active", self.status(record.id))
        self.assertEqual("superseded", self.status(sign_again.id))
        asked = sorted(question["record_ids"] for question in self.store.open_questions(limit=5))
        self.assertEqual(sorted([[tag_again.id, tag.id], [old_host.id, new_host.id], [signed_tags.id, noted.id]]),
                         asked)

    def test_conflicts_become_questions_with_the_older_memory_first(self) -> None:
        first = self.save("Backups run at 02:00.")
        self.clock.value = "2026-09-27T13:00:00Z"
        second = self.save("Backups run at 04:00.")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "conflict", "keep_id": "", "ids": [second.id, first.id], "reason": "Different times."},
        ]]))
        self.assertEqual({"question": 1}, dict(report.changes))
        [question] = self.store.open_questions(limit=5)
        self.assertEqual([first.id, second.id], question["record_ids"])

    def test_bad_findings_are_rejected(self) -> None:
        first = self.save("The NAS is nas01.")
        second = self.save("The router is rtr01.")
        third = self.save("The printer is prn01.")
        pending = self.save("The switch is sw01.")
        other = self.save("We release from the main branch.", kind="decision")
        review.ask_still_true(self.memory, pending)
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "gossip", "keep_id": "", "ids": [first.id], "reason": ""},
            {"kind": "duplicate", "keep_id": first.id, "ids": [first.id], "reason": ""},
            {"kind": "duplicate", "keep_id": first.id, "ids": [other.id], "reason": ""},
            {"kind": "conflict", "keep_id": "", "ids": [first.id, second.id, third.id], "reason": ""},
            {"kind": "snapshot", "keep_id": "", "ids": [], "reason": ""},
            {"kind": "duplicate", "keep_id": first.id, "ids": [pending.id], "reason": ""},
        ]]))
        self.assertEqual({"rejected (kind)": 1, "rejected (ids)": 4, "skipped (open question)": 1}, dict(report.changes))
        self.assertTrue(all(self.status(item.id) == "active" for item in (first, second, third, pending, other)))


class ScheduleTests(MaintenanceTestCase):
    def test_unchanged_groups_cost_no_call_and_changes_wait_for_the_interval(self) -> None:
        first = self.save("The NAS is nas01.")
        second = self.save("nas01 is the NAS.")
        self.save("The router is rtr01.")
        duplicate = {"kind": "duplicate", "keep_id": first.id, "ids": [second.id], "reason": "Same."}
        self.assertEqual(1, self.run_maintenance(FakeReviewer([[duplicate]])).calls)
        self.assertEqual(0, self.run_maintenance(FakeReviewer([])).calls)
        self.save("The printer is prn01.")
        self.assertEqual(["waiting"], [status for _, status in plan(self.memory)])
        self.clock.value = "2026-09-27T19:00:00Z"
        reviewer = FakeReviewer([[]])
        self.assertEqual(1, self.run_maintenance(reviewer).calls)
        self.assertIn("The printer is prn01.", reviewer.texts[0])
        self.assertNotIn("nas01 is the NAS.", reviewer.texts[0])

    def test_failures_retry_then_give_up_and_backend_problems_stop_the_run(self) -> None:
        for text in ("The NAS is nas01.", "The router is rtr01."):
            self.save(text)
        blocked = self.run_maintenance(FakeReviewer([ExtractionError("Not logged in", blocking=True)]))
        self.assertIn("Not logged in", blocked.blocked)
        self.assertEqual(0, blocked.calls)
        self.assertEqual(["review"], [status for _, status in plan(self.memory)])
        for _ in range(3):
            self.assertEqual(1, self.run_maintenance(FakeReviewer([ExtractionError("bad answer")])).failed)
        self.assertEqual(["gave up"], [status for _, status in plan(self.memory)])
        self.assertEqual(3, self.state.calls_since(self._long_ago()))

    def test_the_budget_defers_groups(self) -> None:
        for text in ("The NAS is nas01.", "The router is rtr01."):
            self.save(text)
        report = self.run_maintenance(FakeReviewer([]), budget=0)
        self.assertEqual((0, 1), (report.calls, report.deferred))

    def test_dry_run_calls_nothing(self) -> None:
        for text in ("The NAS is nas01.", "The router is rtr01."):
            self.save(text)
        report = maintain(memory=self.memory, reviewer=None, state=None, budget=10, dry_run=True)
        described = report.describe(dry_run=True)
        self.assertIn("- global: 2 memories, review", described)
        self.assertIn("no model calls", described)

    @staticmethod
    def _long_ago():
        from datetime import datetime, timezone

        return datetime(2000, 1, 1, tzinfo=timezone.utc)


class RestoreTests(MaintenanceTestCase):
    def test_restore_needs_an_inactive_memory(self) -> None:
        record = self.save("The NAS is nas01.")
        with self.assertRaisesRegex(MemoryInputError, "already active"):
            self.memory.restore(record.id)
        with self.assertRaisesRegex(MemoryInputError, "no memory"):
            self.memory.restore("k-0000000000")
        self.memory.forget(record.id, reason="wrong")
        self.assertIn("retired (wrong)", self.memory.history())
        self.assertIn("Restored", self.memory.restore(f"[{record.id}]"))
        empty = Store.in_memory()   # closed here: left open, Python 3.14 warns at a random later test
        try:
            self.assertEqual("No memory has been retired or replaced yet.", Memory(empty).history())
        finally:
            empty.close()


class ReviewerTests(unittest.TestCase):
    def test_the_review_is_one_sealed_call_with_its_own_schema(self) -> None:
        calls = []

        def runner(command, **options):
            calls.append((command, options))
            answer = {"structured_output": {"findings": [{"kind": "snapshot", "keep_id": "", "ids": [], "reason": ""}]}}
            return subprocess.CompletedProcess(command, 0, json.dumps(answer).encode(), b"")

        reviewer = ClaudeCliReviewer(ClaudeCliExtractor(Path("claude.exe"), runner=runner))
        self.assertEqual(1, len(reviewer.review("group text")))
        command, options = calls[0]
        self.assertIn('"findings"', command[command.index("--json-schema") + 1])
        self.assertEqual(REVIEW_PROMPT, command[command.index("--system-prompt") + 1])
        self.assertEqual(b"group text", options["input"])
        self.assertIn("--safe-mode", command)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.environment = mock.patch.dict(
            os.environ,
            {"KNOWITALL2_HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(Path(self.temporary.name) / "claude"),
             "CODEX_HOME": str(Path(self.temporary.name) / "codex")},
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = main(list(arguments))
        return code, output.getvalue()

    def test_maintain_restore_history_and_stats(self) -> None:
        self.run_cli("remember", "The NAS is nas01.", "--source", "inferred")
        self.run_cli("remember", "The router is rtr01.", "--source", "inferred")
        code, output = self.run_cli("maintain")
        self.assertIn("Learning is off", output)
        code, output = self.run_cli("maintain", "--dry-run")
        self.assertEqual(0, code)
        self.assertIn("- global: 2 memories, review", output)
        code, output = self.run_cli("history")
        self.assertIn("No memory has been retired", output)
        code, output = self.run_cli("stats")
        self.assertIn("Maintenance: 0 memories merged or retired in the last 30 days; last review never", output)

    def test_learning_runs_maintenance_with_the_budget_left(self) -> None:
        self.run_cli("remember", "The NAS is nas01.", "--source", "inferred")
        self.run_cli("remember", "The router is rtr01.", "--source", "inferred")
        with mock.patch("knowitall2.learning.command.find_claude_cli", return_value=Path("claude.exe")), \
                mock.patch("knowitall2.learning.command.find_codex_cli", return_value=None):
            self.run_cli("learn", "--enable")
        reviewer = FakeReviewer([[]])
        with mock.patch("knowitall2.learning.command.find_claude_cli", return_value=Path("claude.exe")), \
                mock.patch("knowitall2.learning.command.EngineReviewer", return_value=reviewer):
            code, output = self.run_cli("learn")
        self.assertEqual(0, code)
        self.assertIn("Maintenance: 1 group(s) of related memories: review 1.", output)
        self.assertIn("Review calls: 1.", output)
        self.assertEqual(1, len(reviewer.texts))

    def test_a_request_made_while_maintenance_runs_starts_the_learner_when_it_ends(self) -> None:
        self.run_cli("remember", "The NAS is nas01.", "--source", "inferred")
        self.run_cli("remember", "The router is rtr01.", "--source", "inferred")
        with mock.patch("knowitall2.learning.command.find_claude_cli", return_value=Path("claude.exe")), \
                mock.patch("knowitall2.learning.command.find_codex_cli", return_value=None):
            self.run_cli("learn", "--enable")
        test = self

        class CommitMeanwhile(FakeReviewer):
            def review(self, text):
                # A turn ends with a commit: its hook finds the lock taken and leaves the request waiting.
                test.assertTrue(RunLock().busy())
                moments.request(transcript=str(test.home / "session.jsonl"), session_id="s1", agent="claude-code",
                                cwd=str(test.home), reason="commit", detail="abc1234")
                return super().review(text)

        reviewer = CommitMeanwhile([[]])
        launches = []

        def start(**options) -> bool:
            launches.append((options, RunLock().busy()))
            return True

        with mock.patch("knowitall2.learning.command.find_claude_cli", return_value=Path("claude.exe")), \
                mock.patch("knowitall2.learning.command.EngineReviewer", return_value=reviewer), \
                mock.patch("knowitall2.hooks.maybe_start_learner", side_effect=start):
            code, output = self.run_cli("maintain")
        self.assertEqual(0, code)
        self.assertEqual(1, len(reviewer.texts))
        self.assertEqual([({"requests": True}, False)], launches)  # started once the lock was free

    def test_the_catalog_follows_maintenance_only_while_this_computer_keeps_its_turn(self) -> None:
        from knowitall2.learning.cataloguer import CatalogReport

        self.run_cli("remember", "The NAS is nas01.", "--source", "inferred")
        with mock.patch("knowitall2.learning.command.find_claude_cli", return_value=Path("claude.exe")), \
                mock.patch("knowitall2.learning.command.find_codex_cli", return_value=None):
            self.run_cli("learn", "--enable")
        for renewed, filed in ((False, 0), (True, 1)):
            with self.subTest(renewed=renewed), \
                    mock.patch("knowitall2.learning.command.find_claude_cli", return_value=Path("claude.exe")), \
                    mock.patch("knowitall2.learning.command.EngineReviewer", return_value=FakeReviewer([[]])), \
                    mock.patch("knowitall2.learning.command.renew_maintenance_turn", return_value=renewed) as renew, \
                    mock.patch("knowitall2.learning.command.run_catalog", return_value=CatalogReport()) as catalog:
                self.assertEqual(0, self.run_cli("learn")[0])
                renew.assert_called_once_with()
                self.assertEqual(filed, catalog.call_count)


if __name__ == "__main__":
    unittest.main()
