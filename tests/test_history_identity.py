"""Transcript identity, durable ownership and honest phase status."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _support
import test_learning as f
from knowitall2 import journal, hooks
from knowitall2.learning import moments, provenance
from knowitall2.learning.dossier import Dossier, build_dossiers
from knowitall2.learning.extractor import ExtractionError
from knowitall2.learning.learner import learn, _finish
from knowitall2.learning.log_identity import path_key
from knowitall2.learning.settle import settle, confirm_stale, confirm_session
from knowitall2.learning.state import LearnerState
from knowitall2.learning.transcripts import read_session, Event


class IdentityAndSourceTests(unittest.TestCase):
    def setUp(self):
        self.t = f.TwoPassTests()
        self.t.setUp()
        self.addCleanup(self.t.tearDown)

    def saved(self):
        self.t.sample_log().write(self.t.log_path, idle=False)
        self.t.learn(f.FakeEngine([[self.t.DNSMASQ]]), quick=True)
        return self.t.provisional()[0]

    def parts(self):
        session, _ = read_session(self.t.log_path)
        return session, build_dossiers(session)

    def alias(self):
        if os.name != "nt":
            self.skipTest("Windows extended paths")
        return Path("\\\\?\\" + str(self.t.log_path.resolve()))

    def test_drive_and_unc_extended_spellings_have_one_key(self):
        self.assertEqual(path_key(r"C:\work\log.jsonl"), path_key(r"\\?\C:\WORK\log.jsonl"))
        self.assertEqual(path_key(r"\\server\share\log.jsonl"), path_key(r"\\?\UNC\SERVER\share\log.jsonl"))
        if os.name != "nt":
            self.assertNotEqual(path_key("/tmp/Case"), path_key("/tmp/case"))

    def test_extended_alias_cannot_relearn_settled_bytes(self):
        self.t.sample_log().write(self.t.log_path)
        end = self.t.log_path.stat().st_size
        canonical = self.t.state.entry(self.t.log_path)
        canonical.update(offset=end, settled=end)
        engine = f.FakeEngine([[self.t.DNSMASQ]])
        report = self.t.learn(engine, quick=True, logs=[self.alias()])
        self.assertEqual((0, []), (report.calls, self.t.provisional()))
        self.assertIs(canonical, self.t.state.entry(self.alias()))

    def test_existing_stale_alias_is_redirected_without_cursor_merge(self):
        self.t.sample_log().write(self.t.log_path)
        end = self.t.log_path.stat().st_size
        chosen = self.t.state.entry(self.t.log_path)
        chosen.update(offset=end, settled=end)
        stale = {"offset": 1, "settled": 0}
        self.t.state.logs[os.path.normcase(str(self.alias()))] = stale
        self.assertIs(chosen, self.t.state.entry(self.alias()))
        self.assertEqual({"offset": 1, "settled": 0}, stale)
        self.assertEqual(0, self.t.learn(f.FakeEngine(), quick=True, logs=[self.alias()]).calls)

    def test_alias_with_independent_pending_work_blocks_instead_of_merging(self):
        self.t.sample_log().write(self.t.log_path)
        chosen = self.t.state.entry(self.t.log_path)
        self.t.state.logs[str(self.alias())] = {"offset": 0, "settled": 0,
                                                "pending_reviews": {"final": {"reason": "unfinished"}}}
        report = self.t.learn(f.FakeEngine(), quick=True)
        self.assertIn("aliases", report.blocked)
        self.assertEqual(0, chosen["offset"])

    def test_physical_hardlink_alias_uses_canonical_entry(self):
        self.t.sample_log().write(self.t.log_path)
        chosen = self.t.state.entry(self.t.log_path)
        other = self.t.log_path.with_name("different-name.jsonl")
        os.link(self.t.log_path, other)
        self.assertIs(chosen, self.t.state.entry(other))

    def test_quick_cursor_cannot_precede_settled_cursor(self):
        self.t.sample_log().write(self.t.log_path)
        end = self.t.log_path.stat().st_size
        entry = self.t.state.entry(self.t.log_path)
        entry.update(offset=0, settled=end)
        self.assertEqual(0, self.t.learn(f.FakeEngine(), quick=True).calls)
        self.assertEqual([], self.t.provisional())

    def test_request_groups_use_same_windows_identity(self):
        items = [{"transcript": r"C:\work\chat.jsonl"}, {"transcript": r"\\?\C:\WORK\chat.jsonl"}]
        self.assertEqual([items], moments.by_session(items))

    def test_hot_hook_uses_cached_identity_without_file_stats(self):
        self.saved()
        alias = self.alias()
        self.t.state.entry(alias)
        with mock.patch.object(hooks, "LearnerState", return_value=self.t.state), \
                mock.patch("os.stat", side_effect=AssertionError("hot hook stat")):
            self.assertEqual(self.t.state.entry(self.t.log_path, physical=False)["offset"],
                             hooks._learned_to(str(alias)))

    def test_history_hold_preserves_requests_and_stops_quick_learning(self):
        from knowitall2.learning.command import learn_requests
        self.t.sample_log().write(self.t.log_path, idle=False)
        entry = self.t.state.entry(self.t.log_path)
        entry["history_hold"] = {"reason": "approved repair"}
        request = {"transcript": str(self.t.log_path), "reason": "finished", "agent": "claude-code"}
        with mock.patch.object(moments, "pending_requests", return_value=[request]), \
                mock.patch.object(moments, "done_with") as dropped:
            report = learn_requests(self.t.store, self.t.state, self.t.settings, f.FakeEngine())
        self.assertEqual(0, report.calls)
        dropped.assert_not_called()
        self.assertEqual(1, self.t.learn(f.FakeEngine(), quick=True).deferred)
        self.assertEqual(0, entry["offset"])

    def test_temporary_hold_does_not_create_a_permanent_source_queue(self):
        record = self.saved()
        self.t.state.entry(self.t.log_path)["history_hold"] = True
        self.assertTrue(provenance.held(self.t.store, record, state=self.t.state))
        self.assertEqual([], provenance.waiting(self.t.store))
        self.t.state.entry(self.t.log_path).pop("history_hold")
        self.assertFalse(provenance.held(self.t.store, record, state=self.t.state))

    def test_budget_limited_final_slices_do_not_strand_quick_source_windows(self):
        import re
        builder = f.LogBuilder(self.t.project)
        for number in range(12):
            statement = f"Processing batch{number} uses worker{number} for the staging service."
            builder.user(f"Inspect batch{number}.").assistant(f.text(statement))
            for noise in range(10):
                builder.assistant(f.text(f"Batch{number} detail{noise}: " + "context " * 260))
            builder.write(self.t.log_path, idle=False)
            candidate = {"text": statement, "kind": "fact", "subjects": [f"worker{number}"],
                         "scope": "global", "project": "", "evidence": statement,
                         "relation": "new", "known_id": ""}
            self.t.learn(f.FakeEngine([[candidate]]), quick=True)
        self.assertEqual(12, len(self.t.provisional()))
        self.t.settings.max_calls_per_day = 2
        class ReviewEngine:
            name = "synthetic"
            def run(self, text, *, schema, system_prompt):
                if "reviews" in schema["properties"]:
                    return {"reviews": [f.review(key, "confirm") for key in re.findall(r"^- \[(p\d+)\]", text, re.M)], "memories": []}
                return {"decisions": [{"index": int(i), "action": "new", "known_id": "", "text": "", "reason": "test"}
                                      for i in re.findall(r"^Candidate (\d+)", text, re.M)]}
        first = self.t.learn(ReviewEngine(), right_away=True)
        self.assertFalse(first.failed)
        self.assertGreater(len(self.t.provisional()), 0)
        self.assertLess(len(self.t.provisional()), 12)
        self.assertEqual([], provenance.waiting(self.t.store))
        self.t.learn(ReviewEngine(), right_away=True)
        self.assertEqual([], self.t.provisional())
        self.assertEqual(self.t.log_path.stat().st_size, self.t.state.entry(self.t.log_path)["settled"])

    def test_source_and_record_publication_are_atomic(self):
        self.t.sample_log().write(self.t.log_path, idle=False)
        with mock.patch.object(provenance, "put", side_effect=OSError("source write failed")):
            report = self.t.learn(f.FakeEngine([[self.t.DNSMASQ]]), quick=True)
        self.assertIn("source write failed", report.blocked)
        self.assertEqual([], self.t.provisional())

    def test_source_survives_candidate_journal_pruning(self):
        record = self.saved()
        source = provenance.get(self.t.store, record.id)
        self.t.store.connection.execute("DELETE FROM events")
        session, parts = self.parts()
        report = settle(self.t.memory, f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")],
                                                            "memories": []}]), parts, session=session, run=None)
        self.assertEqual(1, report.outcomes["confirmed"])
        self.assertEqual(source, provenance.get(self.t.store, record.id))

    def test_repeated_quote_in_later_stretch_does_not_own_old_memory(self):
        record = self.saved()
        boundary = self.t.log_path.stat().st_size
        later = f.LogBuilder(self.t.project).user("Later work.").assistant(f.text(self.t.DNSMASQ["evidence"]))
        with self.t.log_path.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(row) + "\n" for row in later.records))
        session, _ = read_session(self.t.log_path, start=boundary)
        parts = build_dossiers(session, require_user=False)
        engine = f.FakeEngine()
        settle(self.t.memory, engine, parts, session=session, run=None, review_start=(boundary, 0))
        self.assertNotIn(record.text, engine.inputs[0][1])
        self.assertEqual([record.id], [r["id"] for r in provenance.waiting(self.t.store)])
        self.assertIn("provisional", self.t.store.get(record.id).tags)

    def test_unknown_source_is_visible_pending_and_not_promoted_when_learning_stops(self):
        record = self.saved()
        self.t.store.connection.execute("DELETE FROM meta WHERE key=?", (provenance.SOURCE + record.id,))
        session, parts = self.parts()
        settle(self.t.memory, f.FakeEngine(), parts, session=session, run=None)
        self.assertEqual(1, len(provenance.waiting(self.t.store)))
        self.assertEqual(0, confirm_session(self.t.memory, f.SESSION))
        self.assertEqual(0, confirm_stale(self.t.memory, now=datetime.now(timezone.utc) + timedelta(days=10)))

    def test_queue_cleans_missing_or_no_longer_provisional_rows(self):
        record = self.saved()
        provenance.hold(self.t.store, record, "pending")
        provenance.hold(self.t.store, SimpleNamespace(id="missing", source_session=f.SESSION), "pending")
        self.t.store.set_tags(record.id, [], now=self.t.memory.now())
        self.assertEqual([], provenance.waiting(self.t.store))

    def test_legacy_binding_uses_original_run_not_repeated_quote(self):
        self.t.sample_log().write(self.t.log_path, idle=False)
        end = self.t.log_path.stat().st_size
        learn(logs=[self.t.log_path], state=self.t.state, settings=self.t.settings,
              memory_factory=lambda: self.t.memory, extractor=f.FakeEngine([[self.t.DNSMASQ]]),
              dry_run=False, quick=True, right_away=True, run="r-original")
        record = self.t.provisional()[0]
        journal.record(self.t.store, "learning", "original quick pass", run="r-original", details={"sessions": [
            {"session": f.SESSION, "path": str(self.t.log_path), "start": 0, "end": end, "event_index": 0}]})
        self.t.store.connection.execute("DELETE FROM meta WHERE key=?", (provenance.SOURCE + record.id,))
        source = provenance.locate_legacy(self.t.memory, record, self.t.log_path, {})
        self.assertIsNotNone(source)
        self.assertLessEqual(source["end"][0], end)

    def test_event_indices_do_not_overlap_adjacent_logical_events(self):
        path = str(self.t.log_path)
        first = Dossier(f.SESSION, "codex", None, None, 0, text="first", source_path=path,
                        start_offset=10, start_event_index=0, end_offset=10, end_event_index=1,
                        source_events=[Event("user", "first", start=10, end=100, index=0)])
        second = Dossier(f.SESSION, "codex", None, None, 1, text="second", source_path=path,
                         start_offset=10, start_event_index=1, end_offset=100,
                         source_events=[Event("user", "second", start=10, end=100, index=1)])
        self.assertFalse(provenance.owned(provenance.descriptor(first), [second]))
        self.assertTrue(provenance.owned(provenance.descriptor(first), [first]))

    def test_partial_context_does_not_own_a_whole_original_source_window(self):
        record = self.saved()
        session, _ = self.parts()
        split = build_dossiers(session, budget=200)
        self.assertGreater(len(split), 1)
        partial = split[:1]
        self.assertFalse(provenance.owned(provenance.get(self.t.store, record.id), partial))

    def test_selected_record_audit_forbids_new_extraction(self):
        record = self.saved()
        session, parts = self.parts()
        engine = f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": [self.t.ROUTER]}])
        with self.assertRaisesRegex(ExtractionError, "may not extract"):
            settle(self.t.memory, engine, parts, session=session, run=None,
                   selected_ids={record.id}, extract_new=False)
        self.assertIn("provisional", self.t.store.get(record.id).tags)

    def test_apply_seal_check_runs_before_any_review_changes(self):
        record = self.saved()
        session, parts = self.parts()
        def changed():
            self.assertTrue(self.t.store.connection.in_transaction)
            raise ValueError("record seal changed")
        with self.assertRaisesRegex(ValueError, "seal changed"):
            settle(self.t.memory, f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []}]),
                   parts, session=session, run=None, selected_ids={record.id}, extract_new=False,
                   before_apply=changed)
        self.assertIn("provisional", self.t.store.get(record.id).tags)

    def test_quick_success_does_not_hide_failed_final_review(self):
        entry = {"offset": 0, "settled": 0, "final_failures": 2,
                 "pending_reviews": {"final": {"reason": "source review incomplete"}}}
        _finish(entry, f.SESSION, 100, datetime.now(timezone.utc))
        self.assertEqual("review pending", entry["status"])
        self.assertEqual("source review incomplete", entry["last_error"])
        self.assertEqual(2, entry["final_failures"])
        self.t.state.logs[str(self.t.log_path)] = entry
        self.assertEqual(1, self.t.state.progress()["quick_done"])
        _finish(entry, f.SESSION, 100, datetime.now(timezone.utc), settled=True)
        self.assertEqual("done", entry["status"])
        self.assertFalse(entry["pending_reviews"])
