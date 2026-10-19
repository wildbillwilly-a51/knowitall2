"""Integrity regressions using temporary homes, synthetic transcripts and isolated databases."""

import io
import json
import os
import re
import argparse
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _support
import test_learning as fixtures
import test_server as server_fixtures
from knowitall2 import connected, known, sync
from knowitall2.learning import availability, recovery
from knowitall2.learning.extractor import ClaudeCliExtractor, ExtractionError, OUTPUT_SCHEMA, parse_cli_payload
from knowitall2.learning.settle import confirm_stale
from knowitall2.learning.state import LearnerState, record_other_call, other_calls
from knowitall2.memory import Memory
from knowitall2.server import exchange
from knowitall2.server.web import ServerHandler


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)

    def learning(self):
        fixture = fixtures.TwoPassTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def server(self):
        fixture = server_fixtures.ExchangeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        return fixture

    def quota(self, message="You've hit your weekly limit"):
        with self.assertRaises(ExtractionError) as error:
            parse_cli_payload(1, json.dumps({"is_error": True, "result": message}).encode(), b"")
        return error.exception

    def test_actual_allowance_messages_are_backend_blocks(self):
        for message in ("You've hit your weekly limit", "You've hit your session limit", "You’ve hit your session limit"):
            error = self.quota(message)
            self.assertTrue(error.blocking)
            self.assertEqual("quota", error.category)
            self.assertIsNotNone(error.retry_at)

    def repeat_case(self, original, target, *, first_pass=False, reason="repeat"):
        t = self.learning()
        (fixtures.LogBuilder(t.project).user("Inspect the backup procedure.")
         .assistant(fixtures.text(original)).write(t.log_path, idle=False))
        known = t.memory.remember(target, kind="procedure", subjects=["backup"], source="observed").record
        candidate = {"text": original, "kind": "procedure", "subjects": ["backup"], "scope": "global",
                     "project": "", "evidence": original, "relation": "new", "known_id": ""}
        t.learn(fixtures.FakeEngine([[candidate]]), quick=True)
        [record] = t.provisional()
        fixtures.quiet_longer(t.log_path)
        decision = fixtures.review("p1", "retire", retire_reason=reason, repeat_of=known.id) if first_pass else fixtures.review("p1", "confirm")
        check = {"decisions": [{"index": 0, "action": "repeat", "known_id": known.id, "text": "", "reason": "same topic"}]}
        report = t.learn(fixtures.FakeEngine(finals=[{"reviews": [decision], "memories": []}], checks=[] if first_pass else [check]))
        self.assertEqual([], report.failed)
        self.assertEqual("active", t.store.get(record.id).status)
        self.assertEqual(original, t.store.get(record.id).text)
        self.assertEqual([], t.provisional())
        self.assertEqual("active", t.store.get(known.id).status)

    def test_final_repeat_cannot_discard_a_backup_safety_step(self):
        original = "SQLite production backups run online using mode=ro and integrity_check before deployment."
        target = "SQLite production backups run online before deployment."
        for first_pass in (False, True):
            with self.subTest(first_pass=first_pass):
                self.repeat_case(original, target, first_pass=first_pass)

    def test_same_topic_is_not_a_duplicate_of_technical_details(self):
        from knowitall2.learning.maintenance import keeps_everything
        base = "The database backup procedure runs online before each production deployment and preserves the current store"
        for detail in ("mode=ro", "integrity_check", "--verify", "backup()", "snapshot.copy()", "`PRAGMA quick_check`", "port 5353", "/srv/snapshots", "never delete originals"):
            with self.subTest(detail=detail):
                self.assertFalse(keeps_everything([base + " with " + detail + "."], base + "."))

    def test_self_or_missing_repeat_targets_never_retire_the_only_copy(self):
        for target in ("p1", "k-missing"):
            t = self.learning()
            t.sample_log().write(t.log_path, idle=False)
            t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
            [record] = t.provisional()
            fixtures.quiet_longer(t.log_path)
            report = t.learn(fixtures.FakeEngine(finals=[{"reviews": [fixtures.review("p1", "retire", retire_reason="repeat", repeat_of=target)], "memories": []}]))
            self.assertEqual([], report.failed)
            self.assertEqual("active", t.store.get(record.id).status)
            self.assertEqual([], t.provisional())

    def test_a_repeat_of_a_retired_sibling_cannot_lose_its_extra_path(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(fixtures.FakeEngine([[t.ROUTER, t.DNSMASQ]]), quick=True)
        first, second = t.provisional()
        fixtures.quiet_longer(t.log_path)
        t.learn(fixtures.FakeEngine(finals=[{"reviews": [fixtures.review("p1", "retire", retire_reason="overturned"), fixtures.review("p2", "retire", retire_reason="repeat", repeat_of="p1")], "memories": []}]))
        self.assertEqual("retired", t.store.get(first.id).status)
        self.assertEqual("active", t.store.get(second.id).status)

    def test_a_new_candidate_cannot_be_dropped_as_a_partial_earlier_repeat(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        fixtures.quiet_longer(t.log_path)
        repeat = {**t.DNSMASQ, "relation": "repeats", "known_id": "c0"}
        engine = fixtures.FakeEngine(finals=[{"reviews": [], "memories": [t.ROUTER, repeat]}], checks=[{"decisions": [
            {"index": 0, "action": "new", "known_id": "", "text": "", "reason": ""},
            {"index": 1, "action": "repeat", "known_id": "c0", "text": "", "reason": "same router"}]}])
        report = t.learn(engine)
        self.assertEqual([], report.failed)
        self.assertEqual(2, t.store.count_active(project_id=None, scope="all"))

    def test_duplicate_retirement_cannot_drop_an_ordinary_safety_qualifier(self):
        self.repeat_case("The database backup procedure creates encrypted nightly copies and verifies the stored snapshot before deployment.",
                         "The database backup procedure creates nightly copies and verifies the stored snapshot before deployment.")

    def test_an_empty_retirement_reason_cannot_bypass_duplicate_preservation(self):
        self.repeat_case("SQLite backups use mode=ro and integrity_check before deployment.",
                         "SQLite backups run before deployment.", first_pass=True, reason="")

    def test_an_unexplained_retirement_keeps_the_review_pending(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
        [record] = t.provisional()
        fixtures.quiet_longer(t.log_path)
        report = t.learn(fixtures.FakeEngine(finals=[{"reviews": [fixtures.review("p1", "retire")], "memories": []}]))
        self.assertTrue(report.failed)
        self.assertEqual(record.id, t.provisional()[0].id)
        self.assertEqual(0, t.state.entry(t.log_path)["settled"])

    def test_explicit_overturned_retirement_does_not_create_a_duplicate_link(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        known = t.memory.remember("The homelab router runs OpenWrt.", subjects=["router"]).record
        t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
        [record] = t.provisional()
        fixtures.quiet_longer(t.log_path)
        report = t.learn(fixtures.FakeEngine(finals=[{"reviews": [fixtures.review("p1", "retire", retire_reason="overturned", repeat_of=known.id)], "memories": []}]))
        self.assertEqual([], report.failed)
        self.assertEqual("retired", t.store.get(record.id).status)
        self.assertIsNone(t.store.get(record.id).superseded_by)

    def test_reconciliation_new_overrides_an_extraction_repeat_label(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        fixtures.quiet_longer(t.log_path)
        candidate = {**t.ROUTER, "relation": "repeats", "known_id": "k-missing"}
        report = t.learn(fixtures.FakeEngine(finals=[{"reviews": [], "memories": [candidate]}]))
        self.assertEqual([], report.failed)
        self.assertEqual(1, t.store.count_active(project_id=None, scope="all"))

    def test_a_merge_cannot_omit_a_new_candidates_backup_safety_step(self):
        t = self.learning()
        original = "SQLite production backups run online using mode=ro and integrity_check before deployment."
        partial = "SQLite production backups run online before deployment."
        (fixtures.LogBuilder(t.project).user("Inspect the database backup procedure.")
         .assistant(fixtures.text(original)).write(t.log_path, idle=False))
        known = t.memory.remember(partial, kind="procedure", subjects=["backup"], source="observed").record
        candidate = {"text": original, "kind": "procedure", "subjects": ["backup"], "scope": "global", "project": "",
                     "evidence": original, "relation": "new", "known_id": ""}
        fixtures.quiet_longer(t.log_path)
        report = t.learn(fixtures.FakeEngine(finals=[{"reviews": [], "memories": [candidate]}], checks=[{"decisions": [
            {"index": 0, "action": "update", "known_id": known.id, "text": partial, "reason": "merge"}]}]))
        self.assertEqual([], report.failed)
        self.assertEqual("active", t.store.get(known.id).status)
        self.assertTrue(any(row.text==original for row in t.store.list_active(project_id=None, scope="all", limit=20)))

    def test_a_correction_can_repeat_a_known_fact_without_reconfirming_the_wrong_original(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        known = t.memory.remember(t.DNSMASQ["text"], kind="lesson", subjects=["router", "dnsmasq"], source="observed").record
        wrong = {**t.DNSMASQ, "text": "On the homelab router, dnsmasq never needs a restart after editing /etc/config/dhcp."}
        t.learn(fixtures.FakeEngine([[wrong]]), quick=True)
        [record] = t.provisional()
        fixtures.quiet_longer(t.log_path)
        correction = fixtures.review("p1", "correct", text=t.DNSMASQ["text"], kind="lesson", scope="global", subjects=["router", "dnsmasq"], evidence=t.DNSMASQ["evidence"])
        report = t.learn(fixtures.FakeEngine(finals=[{"reviews": [correction], "memories": []}], checks=[{"decisions": [
            {"index": 0, "action": "repeat", "known_id": known.id, "text": "", "reason": "corrected fact"}]}]))
        self.assertEqual([], report.failed)
        self.assertEqual("superseded", t.store.get(record.id).status)
        self.assertEqual(known.id, t.store.get(record.id).superseded_by)

    def test_upkeep_cannot_undo_the_final_pass_safety_qualifier(self):
        from knowitall2.learning.maintenance import may_merge, keeps_everything
        t = self.learning()
        original = t.memory.remember("The database backup procedure creates encrypted nightly copies and verifies the stored snapshot before deployment.", source="observed").record
        partial = t.memory.remember("The database backup procedure creates nightly copies and verifies the stored snapshot before deployment.", source="observed").record
        self.assertFalse(may_merge(original, partial))
        self.assertFalse(keeps_everything([original.text], partial.text))

    def corrected_later_review(self, target, *, later_reason="repeat", reconcile_repeat=False):
        t = self.learning()
        builder = t.sample_log()
        for _ in range(8):
            builder.assistant(fixtures.text("Additional unrelated output. " + "x " * 800))
        builder.write(t.log_path, idle=False)
        known = t.memory.remember(t.DNSMASQ["text"] if reconcile_repeat else "The homelab router uses dnsmasq.",
                                  kind="lesson", subjects=["router", "dnsmasq"], source="observed").record
        wrong = {**t.DNSMASQ, "text": "On the homelab router, dnsmasq never needs a restart after editing /etc/config/dhcp."}
        t.learn(fixtures.FakeEngine([[wrong, t.ROUTER] if target == "sibling" else [wrong]]), quick=True)
        record = next(row for row in t.provisional() if row.text == wrong["text"])
        fixtures.quiet_longer(t.log_path)
        class Engine(fixtures.FakeEngine):
            def run(self, text, *, schema, system_prompt):
                if "reviews" not in schema["properties"]:
                    return super().run(text, schema=schema, system_prompt=system_prompt)
                self.inputs.append(("final", text))
                if "review so far: pending" in text:
                    answer = fixtures.review("p1", "correct", text=t.DNSMASQ["text"], kind="lesson", scope="global", evidence=t.DNSMASQ["evidence"])
                elif "review so far: corrected" in text:
                    answer = fixtures.review("p1", "retire", retire_reason=later_reason,
                                             repeat_of=known.id if target == "known" else "p2" if target == "sibling" else target)
                else:
                    return {"reviews": [], "memories": []}
                return {"reviews": [answer, *([fixtures.review("p2", "confirm")] if target == "sibling" else [])], "memories": []}
        engine = Engine(checks=[{"decisions": [{"index": 0, "action": "repeat", "known_id": known.id,
                                                "text": "", "reason": "same"}]}] if reconcile_repeat else [])
        with mock.patch("knowitall2.learning.settle.PART_CHARACTERS", 4000):
            report = t.learn(engine)
        self.assertGreater(len(engine.inputs), 1)
        self.assertEqual([], report.failed)
        self.assertNotEqual("active", t.store.get(record.id).status)
        self.assertEqual([], t.provisional())
        self.assertGreater(t.state.entry(t.log_path)["settled"], 0)
        if later_reason in ("", "repeat"):
            self.assertTrue(any(row.text == t.DNSMASQ["text"] for row in t.store.list_active(project_id=None, scope="all", limit=20)))
        else:
            self.assertEqual("retired", t.store.get(record.id).status)
        if reconcile_repeat:
            self.assertEqual(known.id, t.store.get(record.id).superseded_by)

    def test_a_later_invalid_repeat_publishes_the_correction_instead_of_confirming_the_wrong_original(self):
        for target in ("k-missing", "", "p1", "known", "sibling"):
            with self.subTest(target=target):
                self.corrected_later_review(target)

    def test_a_later_repeat_inferred_from_its_target_keeps_the_correction(self):
        self.corrected_later_review("known", later_reason="")

    def test_a_later_safe_repeat_reconciles_the_correction(self):
        self.corrected_later_review("known", reconcile_repeat=True)

    def test_a_later_substantive_retirement_still_overturns_a_correction(self):
        for reason in ("wrong", "overturned"):
            for target in ("", "known", "p2"):
                with self.subTest(reason=reason, target=target):
                    self.corrected_later_review(target, later_reason=reason)

    def test_reconciliation_cannot_replace_an_observation_by_a_weaker_duplicate(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        known = t.memory.remember("OpenWrt 23.05.3 runs on the homelab router.", subjects=["router"]).record
        t.learn(fixtures.FakeEngine([[t.ROUTER]]), quick=True)
        [record] = t.provisional()
        self.assertEqual("observed", record.verification)
        fixtures.quiet_longer(t.log_path)
        t.learn(fixtures.FakeEngine(finals=[{"reviews": [fixtures.review("p1", "confirm")], "memories": []}], checks=[{"decisions": [
            {"index": 0, "action": "repeat", "known_id": known.id, "text": "", "reason": "same"}]}]))
        self.assertEqual("active", t.store.get(record.id).status)
        self.assertEqual("observed", t.store.get(record.id).verification)

    def test_a_new_memory_survives_an_incomplete_known_repeat(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        known = t.memory.remember("The homelab router runs OpenWrt.", subjects=["router"]).record
        fixtures.quiet_longer(t.log_path)
        candidate = {**t.ROUTER, "relation": "repeats", "known_id": known.id}
        report = t.learn(fixtures.FakeEngine(finals=[{"reviews": [], "memories": [candidate]}], checks=[{"decisions": [
            {"index": 0, "action": "repeat", "known_id": known.id, "text": "", "reason": "same router"}]}]))
        self.assertEqual([], report.failed)
        self.assertEqual(2, t.store.count_active(project_id=None, scope="all"))

    def test_an_incomplete_review_names_the_undecided_record(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
        [record] = t.provisional()
        fixtures.quiet_longer(t.log_path)
        engine = fixtures.FakeEngine(finals=[{"reviews": [fixtures.review("p1", "not_here")], "memories": []}])
        report = t.learn(engine)
        self.assertTrue(report.failed)
        self.assertIn(record.id, t.state.entry(t.log_path)["last_error"])
        self.assertIn("deferred", t.state.entry(t.log_path)["last_error"])
        self.assertIn("original evidence (session data)", engine.inputs[0][1])

    def test_structured_quota_does_not_need_english_words(self):
        payload = {"type": "error", "error": {"type": "rate_limit_error", "message": "indisponible",
                                               "retry_after": 3600}}
        with self.assertRaises(ExtractionError) as error:
            parse_cli_payload(0, json.dumps(payload).encode(), b"")
        self.assertTrue(error.exception.blocking)
        self.assertEqual("backend retry_after", error.exception.reset_source)

    def test_reset_metadata_and_unknown_reset_are_distinguished(self):
        now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        at, source = availability.reset_time({}, "resets 1pm (UTC)", now=now)
        self.assertEqual(datetime(2026, 10, 18, 13, tzinfo=timezone.utc), datetime.fromisoformat(at))
        at, source = availability.reset_time({}, "unknown", now=now)
        self.assertEqual(now + timedelta(minutes=15), datetime.fromisoformat(at))
        self.assertIn("unknown", source)

    def test_durable_gate_covers_separate_consumers_and_one_explicit_probe(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, json.dumps({"is_error": True,
                "result": "You've hit your weekly limit"}).encode(), b"")
        first = ClaudeCliExtractor(Path("fake-claude"), runner=runner)
        second = ClaudeCliExtractor(Path("fake-claude"), runner=runner)
        first.check_availability = second.check_availability = True
        for engine in (first, second):
            with self.assertRaises(ExtractionError):
                engine.run("synthetic", schema=OUTPUT_SCHEMA, system_prompt="test")
        self.assertEqual(1, len(calls))
        second.retry_backend_once = True
        with self.assertRaises(ExtractionError):
            second.run("synthetic", schema=OUTPUT_SCHEMA, system_prompt="test")
        with self.assertRaises(ExtractionError) as blocked:
            second.explore("synthetic", folder=self.root, schema=OUTPUT_SCHEMA, system_prompt="test")
        self.assertFalse(blocked.exception.attempted)
        self.assertEqual(2, len(calls))
        attempts = (self.root / "home/learner/backend-attempts.jsonl").read_text().splitlines()
        self.assertEqual(2, len(attempts))

    def test_bookkeeping_contention_never_discards_a_valid_result(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, b'{"structured_output":{"memories":[]}}', b"")
        engine = ClaudeCliExtractor(Path("fake"), runner=runner)
        engine.check_availability = True
        with mock.patch.object(availability, "clear", side_effect=OSError("bookkeeping unavailable")):
            self.assertEqual({"memories": []}, engine.run("x", schema=OUTPUT_SCHEMA, system_prompt="test"))

    def test_repeated_quota_rejections_never_skip_or_confirm(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
        fixtures.quiet_longer(t.log_path)
        for _ in range(12):
            report = t.learn(fixtures.FakeEngine(finals=[self.quota()]))
        entry = t.state.entry(t.log_path)
        self.assertEqual(0, entry["settled"])
        self.assertEqual(0, entry.get("final_failures", 0))
        self.assertEqual([], report.skipped)
        self.assertEqual(1, len(t.provisional()))
        self.assertEqual(0, entry["pending_reviews"]["final"]["start"])

    def test_quick_failures_cannot_spend_final_pass_retries(self):
        t = self.learning()
        builder = t.sample_log()
        builder.write(t.log_path, idle=False)
        t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
        builder.user("The resolver now reads /etc/config/dhcp at startup.").write(t.log_path, idle=False)
        for _ in range(2):
            t.learn(fixtures.FakeEngine([ExtractionError("bad JSON")] ), quick=True)
        fixtures.quiet_longer(t.log_path)
        report = t.learn(fixtures.FakeEngine(finals=[ExtractionError("bad JSON")]))
        self.assertEqual(1, t.state.entry(t.log_path)["final_failures"])
        self.assertEqual(2, t.state.entry(t.log_path)["failures"])
        self.assertEqual([], report.skipped)
        self.assertEqual(1, len(t.provisional()))

    def test_missing_or_omitted_reviews_never_implicitly_confirm(self):
        for answer in ({"memories": []}, {"reviews": [], "memories": []}):
            t = self.learning()
            t.sample_log().write(t.log_path, idle=False)
            t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
            fixtures.quiet_longer(t.log_path)
            before = len(t.state.calls)
            report = t.learn(fixtures.FakeEngine(finals=[answer]))
            self.assertEqual(1, report.calls)
            self.assertEqual(before + 1, len(t.state.calls))
            self.assertTrue(report.failed)
            self.assertEqual(1, len(t.provisional()))
            self.assertEqual(0, t.state.entry(t.log_path)["settled"])

    def test_duplicate_copy_cannot_turn_failed_final_work_into_success(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
        twin = t.log_path.with_name("twin.jsonl")
        twin.write_bytes(t.log_path.read_bytes())
        fixtures.quiet_longer(t.log_path)
        fixtures.quiet_longer(twin)
        report = t.learn(fixtures.FakeEngine(finals=[ExtractionError("bad JSON"), ExtractionError("bad JSON")]),
                         logs=[t.log_path, twin])
        self.assertEqual(1, len(t.provisional()))
        self.assertEqual(0, t.state.entry(t.log_path)["settled"])
        self.assertEqual(0, t.state.entry(twin)["settled"])
        self.assertNotIn("duplicate dossier", report.outcomes)

    def test_bounded_rereview_preserves_memories_from_later_activity(self):
        t = self.learning()
        builder = t.sample_log()
        builder.write(t.log_path, idle=False)
        bound = t.log_path.stat().st_size
        builder.user("The later backup lives in /srv/later-backups.").write(t.log_path, idle=False)
        candidate = {"text": "The backup lives in /srv/later-backups.", "kind": "fact", "scope": "global",
                     "subjects": ["backup"], "evidence": "The later backup lives in /srv/later-backups.",
                     "project": "", "relation": "new", "known_id": ""}
        t.learn(fixtures.FakeEngine([[candidate]]), quick=True)
        self.assertEqual(1, len(t.provisional()))
        entry = t.state.entry(t.log_path)
        entry.update(learn_to=bound, resume_at=entry["offset"])
        fixtures.quiet_longer(t.log_path)
        report = t.learn(fixtures.FakeEngine())
        self.assertEqual([], report.failed)
        self.assertEqual(1, len(t.provisional()))

    def test_malformed_reconciliation_cannot_publish_or_confirm(self):
        for answer in ({"decisions": []}, {}, {"decisions": [{"index": 0, "action": "repeat", "known_id": "k-unshown"}]}):
            t = self.learning()
            t.sample_log().write(t.log_path, idle=False)
            t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
            fixtures.quiet_longer(t.log_path)
            report = t.learn(fixtures.FakeEngine(finals=[{"reviews": [fixtures.review("p1", "confirm")], "memories": []}],
                                                checks=[answer]))
            self.assertEqual(2, report.calls)
            self.assertTrue(report.failed)
            self.assertEqual(1, len(t.provisional()))
            self.assertEqual(0, t.state.entry(t.log_path)["settled"])

    def test_stale_promotion_cannot_hide_a_pending_review(self):
        t = self.learning()
        t.sample_log().write(t.log_path, idle=False)
        t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
        now = datetime.fromisoformat(t.memory.now().replace("Z", "+00:00")) + timedelta(days=8)
        self.assertEqual(0, confirm_stale(t.memory, now=now, exclude_sessions={fixtures.SESSION}))
        self.assertEqual(1, len(t.provisional()))

    def test_partial_review_cannot_omit_an_in_range_memory(self):
        from knowitall2.learning.settle import settle, PART_CHARACTERS
        for decisions in ([], [fixtures.review("p1", "not_here")]):
            t = self.learning()
            t.sample_log().write(t.log_path, idle=False)
            t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
            session, _ = fixtures.read_session(t.log_path)
            parts = fixtures.build_dossiers(session, budget=PART_CHARACTERS)
            with self.assertRaises(ExtractionError) as error:
                settle(t.memory, fixtures.FakeEngine(finals=[{"reviews": decisions, "memories": []}]),
                       parts, session=session, run=None, partial=True)
            self.assertTrue(error.exception.counted)
            self.assertEqual(1, len(t.provisional()))

    def test_busy_work_cannot_erase_the_active_background_budget(self):
        state = LearnerState(self.root / "state.json")
        now = datetime.now(timezone.utc)
        for _ in range(60):
            state.record_call(at=now, session="background", outcome="ok", origin="learning")
        for _ in range(1000):
            state.record_call(at=now, session="work", outcome="ok", origin="work")
        state.save()
        loaded = LearnerState(state.path)
        self.assertEqual(60, loaded.limited_calls_since(now - timedelta(hours=24)))
        self.assertEqual(1060, loaded.calls_since(now - timedelta(hours=24)))

    def test_user_call_history_keeps_the_active_window(self):
        now = datetime.now(timezone.utc)
        for _ in range(410):
            record_other_call("synthetic", at=now, folder=self.root)
        self.assertEqual(410, len(other_calls(self.root)))

    def test_approved_maintenance_reset_does_not_clear_a_changed_review(self):
        t = self.learning()
        at = "2026-10-17T08:35:31Z"
        t.store.record_review("same", scope_key="global", outcome="failed", now=at)
        t.store.record_review("changed", scope_key="global", outcome="failed", now=at)
        items = [{"fingerprint": key, "scope_key": "global", "reviewed_at": at, "failures": 1}
                 for key in ("same", "changed")]
        t.state.review_resets = items
        t.store.record_review("changed", scope_key="global", outcome="reviewed", now="2026-10-18T00:00:00Z")
        self.assertEqual(1, recovery.reset_reviews(t.store, t.state))
        self.assertIsNone(t.store.review("same"))
        self.assertEqual("reviewed", t.store.review("changed")["outcome"])
        self.assertEqual([], LearnerState(t.state.path).review_resets)

    def test_sensitive_rejection_does_not_copy_its_payload_or_retry_it(self):
        t = self.server()
        rid = t.remember(t.a, "The build server is build-1.")
        operation = sync.pending_operations(t.a)[0]
        token = "gl" + "pat-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        operation["row"]["text"] = "The deploy token is " + token
        t.a.connection.execute("UPDATE records SET text=? WHERE id=?", (operation["row"]["text"], rid))
        t.a.connection.commit()
        with t.a.transaction():
            sync.quarantine(t.a, operation, "refused token " + token)
        [item] = sync.rejections(t.a)
        self.assertTrue(item["sensitive"])
        self.assertIsNone(item["operation"])
        self.assertNotIn(token, t.a.get_meta(sync.REJECTIONS_KEY))
        with self.assertRaises(sync.SyncError):
            sync.resolve_rejection(t.a, item["id"], retry=True)

    def test_invalid_base_is_rebased_and_conflict_checked_on_next_push(self):
        t = self.server()
        rid = t.remember(t.a, "The build server is build-1.")
        t.send(t.a, "c-a")
        sync.apply_pulled(t.b, sync.changes_since(t.server, 0)["changes"], cursor=1)
        Memory(t.b, agent="codex", clock=server_fixtures.Clock("2026-10-01T12:30:00Z")).forget(rid, reason="older edit")
        with t.b.transaction():
            t.b.connection.execute("UPDATE sync_seen SET seq=1000000000 WHERE tbl='records' AND key=?", (rid,))
        class Remote:
            def push(self, operations):
                return {"results": exchange.apply_operations(t.server, "c-b", operations, now="2026-10-01T14:00:00Z")}
        self.assertEqual((0, 0), connected.push(t.b, Remote(), {}))
        self.assertNotIn("base", sync.pending_operations(t.b)[0])
        self.assertEqual((1, 0), connected.push(t.b, Remote(), {}))
        self.assertEqual([], sync.rejections(t.b))

    def test_terminal_rejection_preserves_new_and_existing_local_content(self):
        for existing in (False, True):
            t = self.server()
            rid = t.remember(t.a, "The build server is build-1.")
            if existing:
                t.send(t.a, "c-a")
            t.a.connection.execute("UPDATE records SET text=? WHERE id=?", ("x" * 20001, rid))
            t.a.connection.commit()
            class Remote:
                def push(self, operations):
                    return {"results": exchange.apply_operations(t.server, "c-a", operations, now=server_fixtures.NOW)}
            self.assertEqual((0, 1), connected.push(t.a, Remote(), {}))
            self.assertEqual(20001, len(t.a.get(rid).text))
            self.assertEqual([], sync.pending_operations(t.a))
            [rejection] = sync.rejections(t.a)
            self.assertEqual(rid, rejection["key"])
            connected.pull(t.a, type("Pull", (), {"changes": lambda _, since, limit: sync.changes_since(t.server, since)})())
            self.assertEqual(20001, len(t.a.get(rid).text))
            if existing:
                sync.resolve_rejection(t.a, rejection["id"])
                connected.pull(t.a, type("Pull", (), {"changes": lambda _, since, limit: sync.changes_since(t.server, since)})())
                self.assertEqual("The build server is build-1.", t.a.get(rid).text)

    def test_corrected_rejection_can_be_retried_and_resolved(self):
        t = self.server()
        rid = t.remember(t.a, "The build server is build-1.")
        operation = sync.pending_operations(t.a)[0]
        with t.a.transaction():
            sync.quarantine(t.a, operation, "synthetic validation refusal")
            sync.acknowledge(t.a, [operation])
        sync.resolve_rejection(t.a, operation["op_id"], retry=True)
        self.assertEqual(1, len(sync.pending_operations(t.a)))
        self.assertEqual([], sync.rejections(t.a))

    def test_disconnect_archives_old_rejections_without_protecting_new_server_rows(self):
        t = self.server()
        rid = t.remember(t.a, "The build server is build-1.")
        operation = sync.pending_operations(t.a)[0]
        with t.a.transaction():
            sync.quarantine(t.a, operation, "synthetic rejection")
        connected.disconnect(t.a)
        self.assertEqual([], sync.rejections(t.a))
        self.assertNotIn(("records", rid), sync.pending_keys(t.a))
        self.assertEqual(operation["op_id"], json.loads(t.a.get_meta(sync.ARCHIVED_REJECTIONS_KEY))[0]["id"])

    def test_quarantined_rows_make_connection_health_incomplete(self):
        connection = connected.Connection("https://memory.example.test", {})
        with mock.patch.object(connected, "load", return_value=connection), mock.patch.object(
                connected, "read_status", return_value={"unresolved_rejections": 2}):
            healthy, lines = connected.describe_status(check=False)
        self.assertFalse(healthy)
        self.assertIn("2 rejected", "\n".join(lines))
        from knowitall2.app.api import _add_sharing_health
        health = {"level": "ok", "headline": "Everything is working", "items": []}
        _add_sharing_health(health, {"agents": ["codex"], "problem_status": None, "problem": None,
                                     "address": connection.address, "unresolved_rejections": 2})
        self.assertEqual("attention", health["level"])
        self.assertIn("2 local", health["items"][0]["text"])

    def test_rejection_resolution_cannot_race_an_active_sync(self):
        t = self.server()
        operation = sync.pending_operations(t.a)
        rid = t.remember(t.a, "The build server is build-1.")
        operation = sync.pending_operations(t.a)[0]
        with t.a.transaction():
            sync.quarantine(t.a, operation, "test")
        args = argparse.Namespace(quiet=True, agent=None, rejections=False,
                                  retry_rejected=None, dismiss_rejected=operation["op_id"])
        # run_command owns a second handle to this same isolated database.
        with connected.SyncLock(), mock.patch.object(connected.Store, "open", return_value=t.a), mock.patch.object(
                t.a, "close"), mock.patch.object(connected, "is_connected", return_value=True):
            self.assertEqual(1, connected.run_command(args))
        self.assertEqual(1, len(sync.rejections(t.a)))

    def test_forged_future_revision_is_rejected_without_overwriting(self):
        t = self.server()
        rid = t.remember(t.a, "The build server is build-1.")
        t.send(t.a, "c-a")
        changes = sync.changes_since(t.server, 0)
        sync.apply_pulled(t.b, changes["changes"], cursor=changes["next"])
        Memory(t.a, agent="codex", clock=server_fixtures.Clock("2026-10-01T13:00:00Z")).forget(rid, reason="newer edit")
        t.send(t.a, "c-a", now="2026-10-01T13:00:00Z")
        Memory(t.b, agent="codex", clock=server_fixtures.Clock("2026-10-01T12:30:00Z")).forget(rid, reason="older edit")
        operation = sync.pending_operations(t.b)[0]
        operation["base"] = 10**9
        answer = exchange.apply_operations(t.server, "c-b", [operation], now="2026-10-01T14:00:00Z")[0]
        self.assertEqual(("rejected", "invalid_base", True), (answer["result"], answer["code"], answer["retry"]))
        self.assertEqual("newer edit", t.server.get(rid).retired_reason)

    def test_project_export_controls_are_scoped(self):
        first = _support.make_repository(self.root / "one")
        second = _support.make_repository(self.root / "two")
        known.set_project_enabled(first, False)
        self.assertIn("disabled", known.held_back(first))
        self.assertIsNone(known.held_back(second))
        known.set_project_enabled(first, True)
        self.assertIsNone(known.held_back(first))

    def test_export_streams_in_chunks_and_cleans_up_on_disconnect(self):
        server = mock.Mock(home=self.root, database=self.root / "source.db", export_slots=threading.BoundedSemaphore(2))
        def snapshot(database, target):
            target.write_bytes(b"x" * 200_000)
            return 7
        handler = object.__new__(ServerHandler)
        handler.server = server
        handler._head_only = False
        handler._headers = mock.Mock()
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = BrokenPipeError("client disconnected")
        with self.assertRaises(BrokenPipeError), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            handler.export(snapshot, prefix="test-")
        self.assertLessEqual(len(handler.wfile.write.call_args.args[0]), 65536)
        self.assertEqual([], list((self.root / "tmp").iterdir()))
        self.assertTrue(server.export_slots.acquire(blocking=False))
        self.assertTrue(server.export_slots.acquire(blocking=False))

    def test_exports_refuse_more_than_two_concurrent_snapshots(self):
        from knowitall2.server.web import RequestError
        server = mock.Mock(export_slots=threading.BoundedSemaphore(2))
        server.export_slots.acquire()
        server.export_slots.acquire()
        handler = object.__new__(ServerHandler)
        handler.server = server
        snapshot = mock.Mock()
        with self.assertRaises(RequestError):
            handler.export(snapshot, prefix="test-")
        snapshot.assert_not_called()

    def confirm_shown_engine(self):
        class Engine(fixtures.FakeEngine):
            def run(self, text, *, schema, system_prompt):
                if "reviews" not in schema["properties"]:
                    return super().run(text, schema=schema, system_prompt=system_prompt)
                self.inputs.append(("final", text))
                return {"reviews": [fixtures.review(key, "confirm")
                                    for key in re.findall(r"^- \[(p[0-9]+)\]", text, re.M)], "memories": []}
        return Engine()

    def test_recovery_leaves_later_assistant_memory_untouched(self):
        t = self.learning()
        builder = t.sample_log()
        builder.write(t.log_path, idle=False)
        t.learn(fixtures.FakeEngine([[t.DNSMASQ]]), quick=True)
        end = t.log_path.stat().st_size
        later = "The observatory telescope uses a cobalt calibration lamp at midnight."
        builder.assistant(fixtures.text(later)).write(t.log_path, idle=False)
        candidate = {"text": later, "kind": "fact", "subjects": ["telescope"], "scope": "global",
                     "project": "", "evidence": later, "relation": "new", "known_id": ""}
        t.learn(fixtures.FakeEngine([[candidate]]), quick=True)
        record = next(r for r in t.provisional() if r.text == later)
        self.assertNotEqual("user_stated", record.verification)
        before = t.store.get(record.id)
        cursors = {key: t.state.entry(t.log_path).get(key, 0)
                   for key in ("offset", "event_index", "settled", "settled_event_index")}
        item = {"path": str(t.log_path.resolve()), "session": fixtures.SESSION, "start": 0,
                "event_index": 0, "end": end, "end_event_index": 0, "scope": "historical_whole_session",
                "sha256": recovery.prefix_hash(t.log_path, end)}
        item["id"] = recovery.range_id(item)
        recovery.queue({"format": 1, "ranges": [item], "unresolved": []}, t.state, allow_whole_session=True)
        engine = self.confirm_shown_engine()
        report = t.learn(engine)
        self.assertEqual([], report.failed)
        self.assertTrue(engine.inputs)
        self.assertTrue(all(later not in text and record.id not in text for _step, text in engine.inputs))
        self.assertEqual(before, t.store.get(record.id))
        self.assertEqual(cursors, {key: t.state.entry(t.log_path).get(key, 0) for key in cursors})
        self.assertIn(item["id"], t.state.entry(t.log_path)["recovery_completed"])

    def test_the_final_recovery_slice_does_not_review_an_earlier_slices_memories_again(self):
        from knowitall2.learning.dossier import build_dossiers
        from knowitall2.learning.transcripts import read_session
        from knowitall2.learning.settle import settle, _in_parts
        t = self.learning()
        builder = t.sample_log()
        for _ in range(8):
            builder.assistant(fixtures.text("Unrelated investigation output. " + "x " * 800))
        later = "The observatory telescope uses a cobalt calibration lamp at midnight."
        builder.assistant(fixtures.text(later)).write(t.log_path, idle=False)
        candidate = {"text": later, "kind": "fact", "subjects": ["telescope"], "scope": "global",
                     "project": "", "evidence": later, "relation": "new", "known_id": ""}
        t.learn(fixtures.FakeEngine([[t.DNSMASQ, candidate]]), quick=True)
        first = next(r for r in t.provisional() if r.text == t.DNSMASQ["text"])
        session, _ = read_session(t.log_path)
        parts = build_dossiers(session, budget=4000)
        self.assertGreater(len(parts), 1)
        self.assertFalse(_in_parts(t.DNSMASQ, parts[-1:]))
        self.assertFalse(_in_parts(candidate, parts[:-1]))
        settle(t.memory, self.confirm_shown_engine(), parts[:-1], session=session, run=None,
               partial=True, recovery=True)
        before = t.store.get(first.id)
        last = self.confirm_shown_engine()
        settle(t.memory, last, parts[-1:], session=session, run=None, recovery=True)
        self.assertTrue(last.inputs)
        self.assertTrue(all(first.text not in text and first.id not in text for _step, text in last.inputs))
        self.assertEqual(before, t.store.get(first.id))
        self.assertEqual([], t.provisional())

    def test_recovery_is_hash_bound_idempotent_and_does_not_rewind_progress(self):
        t = self.learning()
        t.sample_log().write(t.log_path)
        t.learn(fixtures.FakeEngine())
        original = dict(t.state.entry(t.log_path))
        end = t.log_path.stat().st_size
        item = {"path": str(t.log_path.resolve()), "session": fixtures.SESSION, "start": 0,
                "event_index": 0, "end": end, "end_event_index": 0,
                "scope": "historical_whole_session", "sha256": recovery.prefix_hash(t.log_path, end)}
        item["id"] = recovery.range_id(item)
        manifest = {"format": 1, "ranges": [item], "unresolved": []}
        with self.assertRaises(ValueError):
            recovery.queue(manifest, t.state)
        self.assertEqual(1, recovery.queue(manifest, t.state, allow_whole_session=True))
        # Normal failure diagnostics are independent of recovery's own backoff.
        t.state.entry(t.log_path).setdefault("pending_reviews", {})["final"] = {"reason": "normal pending range"}
        self.assertEqual(original["settled"], t.state.entry(t.log_path)["settled"])
        self.assertIn("final", t.state.entry(t.log_path)["pending_reviews"])
        engine = fixtures.FakeEngine()
        report = t.learn(engine)
        self.assertTrue(engine.inputs)
        self.assertEqual(original["settled"], t.state.entry(t.log_path)["settled"])
        self.assertIn("final", t.state.entry(t.log_path)["pending_reviews"])
        self.assertEqual(0, recovery.queue(manifest, t.state, allow_whole_session=True))
        t.log_path.write_text("changed\n")
        with self.assertRaises(ValueError):
            recovery.validate(item)


if __name__ == "__main__":
    unittest.main()
