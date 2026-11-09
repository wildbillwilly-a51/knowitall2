"""A repair is bounded, allowlisted, source-aware and safe to resume."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

import _support
import test_learning as f
from knowitall2.learning.history import prepare, prepare_alias_conflicts, execute, validate, record_seal, RECEIPTS
from knowitall2.learning.learner import learn
from knowitall2.learning.extractor import ExtractionError
from knowitall2.learning import provenance
from knowitall2.learning.state import LearnerState


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.t = f.TwoPassTests()
        self.t.setUp()
        self.addCleanup(self.t.tearDown)
        self.builder = self.t.sample_log()
        self.builder.write(self.t.log_path, idle=False)
        learn(logs=[self.t.log_path], state=self.t.state, settings=self.t.settings,
              memory_factory=lambda: self.t.memory, extractor=f.FakeEngine([[self.t.DNSMASQ]]),
              dry_run=False, quick=True, right_away=True, run="r-replay")
        self.record = self.t.provisional()[0]
        self.start = self.t.log_path.stat().st_size
        self.builder.user("Later work.").assistant(f.text("This is a separate final stretch.")).write(self.t.log_path, idle=False)
        self.end = self.t.log_path.stat().st_size
        self.entry = self.t.state.entry(self.t.log_path)
        self.entry.update(offset=self.end, settled=self.start, final_failures=2,
                          pending_reviews={"final": {"start": self.start, "end": self.end,
                                                     "reason": "old source deferred", "attempts": 2}})
        self.alias = self.t.log_path.with_name("alias.jsonl")
        os.link(self.t.log_path, self.alias)
        self.t.state.logs[str(self.alias)] = {"session_id": f.SESSION, "offset": 1, "settled": 0, "failures": 0}
        self.t.state.save()
        self.plan_path = self.t.root / "history.json"
        self.checkpoint = self.t.root / "model-cache.json"

    def plan(self):
        return prepare(self.t.store, self.t.state, session_id=f.SESSION, replay_runs=["r-replay"],
                       output=self.plan_path)

    def engine(self, *, final=None):
        return f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []},
                                   final or {"reviews": [], "memories": []}])

    def run_plan(self, plan, engine=None):
        return execute(self.t.store, self.t.state, plan, engine or self.engine(), checkpoint=self.checkpoint)

    def test_prepare_changes_no_database_or_progress(self):
        before = self.t.store.connection.total_changes
        original = json.loads(json.dumps(self.entry))
        plan = self.plan()
        self.assertEqual(before, self.t.store.connection.total_changes)
        self.assertEqual(original, self.entry)
        self.assertEqual([self.record.id], plan["selected_ids"])
        self.assertTrue(Path(plan["source"]["snapshot"]).is_file())

    def test_complete_repair_preserves_quick_cursor_and_only_finishes_reviewed_range(self):
        plan = self.plan()
        result = self.run_plan(plan)
        self.assertEqual(self.end, self.entry["offset"])
        self.assertEqual(self.end, self.entry["settled"])
        self.assertEqual(0, self.entry["final_failures"])
        self.assertFalse(self.entry["pending_reviews"])
        self.assertNotIn("history_hold", self.entry)
        self.assertNotIn("provisional", self.t.store.get(self.record.id).tags)
        self.assertNotIn(str(self.alias), self.t.state.logs)
        self.assertTrue(result["completed_at"])

    def test_named_physical_history_reviews_its_records_and_leaves_sibling_intact(self):
        from knowitall2.learning.dossier import build_dossiers
        from knowitall2.learning.transcripts import read_session

        sibling = self.t.log_path.with_name("sibling.jsonl")
        statement = "The backup observatory uses a separate cobalt lamp."
        self.builder.user(statement).write(sibling, idle=False)
        sibling_entry = self.t.state.entry(sibling)
        sibling_entry.update(session_id=f.SESSION, offset=sibling.stat().st_size)
        other = self.t.memory.remember(statement, session=f.SESSION).record
        self.t.store.set_tags(other.id, ["provisional"], now=self.t.memory.now())
        source, _ = read_session(sibling)
        provenance.put(self.t.store, other.id, provenance.descriptor(build_dossiers(source)[-1]))
        before_source = provenance.get(self.t.store, other.id)
        before_entry = dict(sibling_entry)
        self.t.state.logs[str(self.alias)]["offset"] = 0
        plan = prepare_alias_conflicts(self.t.store, self.t.state, session_id=f.SESSION,
                                       transcript=self.t.log_path.resolve(), output=self.plan_path)
        self.assertEqual([self.record.id], [binding["id"] for binding in plan["bindings"]])
        self.assertEqual(0, plan["forward_start"])  # The original provisional source precedes final progress.
        self.run_plan(plan, f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []}]))
        self.assertEqual(before_entry, self.t.state.entry(sibling))
        self.assertEqual(before_source, provenance.get(self.t.store, other.id))
        self.assertIsNone(self.t.store.get_meta(provenance.PENDING + other.id))
        self.assertIn("provisional", self.t.store.get(other.id).tags)
        self.assertNotIn("provisional", self.t.store.get(self.record.id).tags)

    def test_named_single_checkpoint_resumes_after_forward_commit(self):
        from knowitall2.learning.log_identity import path_key

        del self.t.state.logs[str(self.alias)]
        sibling = self.t.log_path.with_name("sibling.jsonl")
        self.builder.write(sibling, idle=False)
        self.t.state.entry(sibling).update(session_id=f.SESSION, offset=0)
        self.t.state.save()
        plan = prepare_alias_conflicts(self.t.store, self.t.state, session_id=f.SESSION,
                                       transcript=self.t.log_path.resolve(), output=self.plan_path)
        save = self.t.state.save

        def interrupt():
            if "history_hold" not in self.entry:
                raise OSError("interrupted before checkpoint save")
            save()

        engine = f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []}])
        with mock.patch.object(self.t.state, "save", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "before checkpoint save"):
                self.run_plan(plan, engine)
        restarted = LearnerState(self.t.state.path)
        sibling_entry = restarted.entry(sibling)
        sibling_entry["offset"] = sibling.stat().st_size
        extra_route = path_key(sibling.with_name("new-route.jsonl"))
        restarted.log_aliases[extra_route] = path_key(sibling)
        before = dict(sibling_entry)
        never = f.FakeEngine()
        execute(self.t.store, restarted, plan, never, checkpoint=self.checkpoint)
        self.assertEqual([], never.inputs)
        self.assertEqual(self.end, restarted.entry(self.t.log_path)["settled"])
        self.assertNotIn("history_hold", restarted.entry(self.t.log_path))
        self.assertEqual(before, restarted.entry(sibling))
        self.assertEqual(path_key(sibling), restarted.log_aliases[extra_route])

    def test_named_pending_final_range_preserves_the_completed_prefix(self):
        self.t.store.set_tags(self.record.id, [], now=self.t.memory.now())
        self.t.state.logs[str(self.alias)]["offset"] = 0
        plan = prepare_alias_conflicts(self.t.store, self.t.state, session_id=f.SESSION,
                                       transcript=self.t.log_path.resolve(), output=self.plan_path)
        self.assertEqual(self.start, plan["forward_start"])
        engine = f.FakeEngine(finals=[{"reviews": [], "memories": []}])
        self.run_plan(plan, engine)
        self.assertEqual(0, self.entry["final_failures"])
        self.assertEqual({}, self.entry["pending_reviews"])

    def test_completed_repair_is_idempotent(self):
        plan = self.plan()
        first = self.run_plan(plan)
        never = f.FakeEngine(finals=[AssertionError("model should not be called")])
        second = self.run_plan(plan, never)
        self.assertEqual(first, second)
        self.assertEqual([], never.inputs)

    def test_new_transcript_append_does_not_expand_approved_review(self):
        plan = self.plan()
        self.builder.user("An even newer request.").assistant(f.text("New work after planning.")).write(self.t.log_path, idle=False)
        validate(plan)
        self.run_plan(plan)
        self.assertEqual(plan["forward_end"], self.entry["settled"])
        self.assertLess(self.entry["settled"], self.t.log_path.stat().st_size)

    def test_changed_prefix_is_rejected_before_model_or_application(self):
        plan = self.plan()
        with self.t.log_path.open("r+b") as stream:
            stream.write(b"X")
        with self.assertRaisesRegex(ValueError, "prefix changed"):
            self.run_plan(plan)
        self.assertIn("provisional", self.t.store.get(self.record.id).tags)

    def test_changed_record_is_rejected_before_model(self):
        plan = self.plan()
        self.t.store.set_tags(self.record.id, ["provisional", "changed"], now=self.t.memory.now())
        with self.assertRaisesRegex(ValueError, "record changed"):
            self.run_plan(plan)
        self.assertEqual(self.start, self.entry["settled"])

    def test_changed_cursor_is_not_reset_to_make_a_plan_fit(self):
        plan = self.plan()
        self.entry["offset"] += 1
        with self.assertRaisesRegex(ValueError, "progress changed"):
            self.run_plan(plan)
        self.assertEqual(self.end + 1, self.entry["offset"])

    def test_transport_interruption_resumes_without_reapplying_audited_records(self):
        plan = self.plan()
        with self.assertRaises(ExtractionError):
            self.run_plan(plan, self.engine(final=ExtractionError("temporary model failure")))
        self.assertNotIn("provisional", self.t.store.get(self.record.id).tags)
        self.assertEqual(self.start, self.entry["settled"])
        self.assertIn("history_hold", self.entry)
        seal = record_seal(self.t.store, self.record.id)
        self.run_plan(plan, f.FakeEngine(finals=[{"reviews": [], "memories": []}]))
        self.assertEqual(seal, record_seal(self.t.store, self.record.id))
        self.assertEqual(self.end, self.entry["settled"])

    def test_cached_part_is_not_repeated_after_reconciliation_interruption(self):
        plan = self.plan()
        engine = f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []}],
                              checks=[ExtractionError("check interrupted")])
        with self.assertRaises(ExtractionError):
            self.run_plan(plan, engine)
        self.assertIn("provisional", self.t.store.get(self.record.id).tags)
        resumed = f.FakeEngine(finals=[{"reviews": [], "memories": []}])
        self.run_plan(plan, resumed)
        self.assertEqual(["check", "final"], [kind for kind, _ in resumed.inputs])

    def test_record_change_during_model_review_is_rejected_inside_transaction(self):
        plan = self.plan()
        engine = self.engine()
        original = engine.run
        def edit(text, **kwargs):
            result = original(text, **kwargs)
            if "decisions" in kwargs["schema"]["properties"]:
                self.t.store.set_tags(self.record.id, ["provisional", "concurrent"], now=self.t.memory.now())
            return result
        engine.run = edit
        with self.assertRaisesRegex(ValueError, "record changed during"):
            self.run_plan(plan, engine)
        self.assertIn("provisional", self.t.store.get(self.record.id).tags)
        self.assertEqual(self.start, self.entry["settled"])

    def test_plan_tampering_is_rejected(self):
        plan = self.plan()
        plan["max_calls"] = 100
        with self.assertRaisesRegex(ValueError, "identity"):
            self.run_plan(plan)

    def test_alias_with_independent_work_is_not_retired(self):
        self.t.state.logs[str(self.alias)]["pending_reviews"] = {"final": {"reason": "other work"}}
        with self.assertRaisesRegex(ValueError, "independent"):
            self.plan()

    def alias_plan(self):
        self.entry["final_failures"] = 0
        self.entry["pending_reviews"] = {"quick": {"start": self.start, "end": self.end}}
        self.t.state.logs[str(self.alias)].update(
            offset=self.start, pending_reviews={"quick": {"start": 0, "end": self.end}})
        self.t.state.save()
        return prepare_alias_conflicts(self.t.store, self.t.state, session_id=f.SESSION, output=self.plan_path)

    def test_conflicting_quick_ranges_are_reviewed_before_checkpoints_are_consolidated(self):
        plan = self.alias_plan()
        self.assertEqual(0, plan["forward_start"])
        original = json.loads(json.dumps(plan["alias_entries"]))
        engine = f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []}])
        result = self.run_plan(plan, engine)
        self.assertEqual(original, result["forward"]["original_alias_entries"])
        self.assertEqual(self.end, self.entry["settled"])
        self.assertFalse(self.entry["pending_reviews"])
        self.assertNotIn(str(self.alias), self.t.state.logs)
        self.assertNotIn("provisional", self.t.store.get(self.record.id).tags)
        self.assertEqual(result, self.run_plan(plan, f.FakeEngine()))

    def test_alias_plan_keeps_established_final_prefix_when_all_sources_follow_it(self):
        from knowitall2.learning.dossier import build_dossiers
        from knowitall2.learning.transcripts import read_session
        self.t.store.set_tags(self.record.id, [], now=self.t.memory.now())
        statement = "The observatory uses a cobalt backup lamp."
        self.builder.user(statement).write(self.t.log_path, idle=False)
        self.end = self.t.log_path.stat().st_size
        self.entry.update(offset=self.end, final_failures=0, pending_reviews={})
        self.t.state.logs[str(self.alias)].update(
            offset=self.start, pending_reviews={"quick": {"start": self.start, "end": self.end}})
        later = self.t.memory.remember(statement, session=f.SESSION).record
        self.t.store.set_tags(later.id, ["provisional"], now=self.t.memory.now())
        suffix, _ = read_session(self.t.log_path, start=self.start)
        provenance.put(self.t.store, later.id, provenance.descriptor(build_dossiers(suffix, require_user=False)[0]))
        plan = prepare_alias_conflicts(self.t.store, self.t.state, session_id=f.SESSION, output=self.plan_path)
        self.assertEqual(self.start, plan["forward_start"])
        engine = f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []}])
        result = self.run_plan(plan, engine)
        self.assertEqual(self.end, result["forward"]["final_end"])
        self.assertNotIn("provisional", self.t.store.get(later.id).tags)

    def test_alias_receipt_can_resume_after_database_commit_before_state_save(self):
        plan = self.alias_plan()
        save = self.t.state.save
        def interrupted():
            if str(self.alias) not in self.t.state.logs:
                raise OSError("interrupted before alias save")
            save()
        engine = f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []}])
        with mock.patch.object(self.t.state, "save", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "before alias save"):
                self.run_plan(plan, engine)
        restarted = LearnerState(self.t.state.path)
        self.assertIn(str(self.alias), restarted.logs)
        never = f.FakeEngine()
        result = execute(self.t.store, restarted, plan, never, checkpoint=self.checkpoint)
        self.assertNotIn(str(self.alias), restarted.logs)
        self.assertEqual([], never.inputs)
        self.assertEqual(plan["alias_entries"], result["forward"]["original_alias_entries"])

    def test_alias_plan_rejects_new_pending_work_before_using_model(self):
        plan = self.alias_plan()
        self.t.state.logs[str(self.alias)]["pending_reviews"]["quick"]["end"] += 1
        engine = f.FakeEngine()
        with self.assertRaisesRegex(ValueError, "checkpoint changed"):
            self.run_plan(plan, engine)
        self.assertEqual([], engine.inputs)
        self.assertIn("provisional", self.t.store.get(self.record.id).tags)

    def test_new_alias_plan_can_replace_failed_review_without_releasing_checkpoints(self):
        plan = self.alias_plan()
        wrong = f.FakeEngine(finals=[{"reviews": [f.review("p1", "not_here")], "memories": []}])
        with self.assertRaisesRegex(ExtractionError, "unreviewed"):
            self.run_plan(plan, wrong)
        self.assertEqual(self.start, self.entry["settled"])
        self.assertIn(str(self.alias), self.t.state.logs)
        self.assertIn("provisional", self.t.store.get(self.record.id).tags)
        new = prepare_alias_conflicts(self.t.store, LearnerState(self.t.state.path), session_id=f.SESSION,
                                      output=self.t.root / "retry.json")
        execute(self.t.store, self.t.state, new,
                f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []}]),
                checkpoint=self.t.root / "retry-model-cache.json")
        prior = json.loads(self.t.store.get_meta(RECEIPTS + new["id"] + ".supersedes"))
        self.assertEqual(plan["id"], prior["plan"])
        self.assertEqual(self.end, self.entry["settled"])

    def test_alias_repair_resumes_after_state_save_before_completion_receipt(self):
        plan = self.alias_plan()
        set_meta = self.t.store.set_meta
        def interrupted(key, value):
            if key == RECEIPTS + plan["id"] + ".complete":
                raise OSError("interrupted before completion receipt")
            set_meta(key, value)
        engine = f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm")], "memories": []}])
        with mock.patch.object(self.t.store, "set_meta", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "before completion receipt"):
                self.run_plan(plan, engine)
        restarted = LearnerState(self.t.state.path)
        self.assertNotIn(str(self.alias), restarted.logs)
        never = f.FakeEngine()
        execute(self.t.store, restarted, plan, never, checkpoint=self.checkpoint)
        self.assertEqual([], never.inputs)
        self.assertEqual(self.end, restarted.entry(self.t.log_path)["settled"])

    def test_forward_commit_receipt_survives_interruption_before_cursor_save(self):
        plan = self.plan()
        original = self.t.state.save
        def stop_after_commit():
            if self.entry["settled"] == self.end:
                raise OSError("interrupted before cursor save")
            original()
        with mock.patch.object(self.t.state, "save", side_effect=stop_after_commit):
            with self.assertRaisesRegex(OSError, "cursor save"):
                self.run_plan(plan)
        resumed_state = LearnerState(self.t.state.path)
        never = f.FakeEngine(finals=[AssertionError("review must not repeat")])
        execute(self.t.store, resumed_state, plan, never, checkpoint=self.checkpoint)
        entry = resumed_state.entry(self.t.log_path)
        self.assertEqual(self.end, entry["settled"])
        self.assertEqual([], never.inputs)
        self.assertNotIn("history_hold", entry)

    def test_audit_cannot_update_an_unselected_existing_record(self):
        other = self.t.memory.remember("The homelab router also handles local DNS.", subjects=["router"]).record
        seal = record_seal(self.t.store, other.id)
        plan = self.plan()
        engine = self.engine()
        engine.checks = [{"decisions": [{"index": 0, "action": "update", "known_id": other.id,
                                         "text": other.text + " dnsmasq needs a restart.", "reason": "merge"}]}]
        # Only a target actually shown can be named. Ensure it appears in
        # the reconciliation, then verify the history mode keeps it intact.
        self.run_plan(plan, engine)
        self.assertEqual(seal, record_seal(self.t.store, other.id))

    def test_suppressed_backend_probe_does_not_spend_the_repair_cap(self):
        plan = self.plan()
        blocked = f.FakeEngine(finals=[ExtractionError("backend paused", blocking=True, attempted=False)])
        with self.assertRaises(ExtractionError):
            self.run_plan(plan, blocked)
        self.assertEqual(0, json.loads(self.checkpoint.read_text())["calls"])

    def test_forward_can_confirm_a_word_for_word_known_duplicate(self):
        statement = "This is a separate final stretch."
        known = self.t.memory.remember(statement, kind="fact", subjects=["stretch"]).record
        self.t.store.confirm(known.id, verification=known.verification, now="2025-01-01T00:00:00Z")
        plan = self.plan()
        candidate = {"text": statement, "kind": "fact", "scope": "global", "project": "",
                     "subjects": ["stretch"], "evidence": statement, "relation": "new", "known_id": ""}
        engine = self.engine(final={"reviews": [], "memories": [candidate]})
        self.run_plan(plan, engine)
        self.assertEqual("active", self.t.store.get(known.id).status)
        self.assertNotEqual("2025-01-01T00:00:00Z", self.t.store.get(known.id).confirmed_at)
        self.assertEqual(self.end, self.entry["settled"])

    def test_non_replay_source_crossing_forward_boundary_is_selected(self):
        from knowitall2.learning.transcripts import read_session
        from knowitall2.learning.dossier import build_dossiers
        self.builder.user("The observatory uses a cobalt backup lamp.").write(self.t.log_path, idle=False)
        later = self.t.memory.remember("The observatory uses a cobalt backup lamp.", session=f.SESSION).record
        self.t.store.set_tags(later.id, ["provisional"], now=self.t.memory.now())
        source, _ = read_session(self.t.log_path, start=self.start)
        provenance.put(self.t.store, later.id, provenance.descriptor(build_dossiers(source, require_user=False)[0]))
        self.entry["offset"] = self.t.log_path.stat().st_size
        plan = self.plan()
        self.assertIn(later.id, plan["selected_ids"])
        self.assertEqual(2, len(plan["selected_ids"]))

    def test_fresh_plan_after_audit_has_no_audit_model_calls(self):
        plan = self.plan()
        with self.assertRaises(ExtractionError):
            self.run_plan(plan, self.engine(final=ExtractionError("forward interrupted")))
        fresh = prepare(self.t.store, self.t.state, session_id=f.SESSION, replay_runs=["r-replay"],
                        output=self.t.root / "fresh.json", max_calls=2)
        self.assertEqual([], fresh["selected_ids"])
        self.assertEqual(0, fresh["audit_parts"])
        engine = f.FakeEngine(finals=[{"reviews": [], "memories": []}])
        execute(self.t.store, self.t.state, fresh, engine, checkpoint=self.t.root / "fresh-cache.json")
        self.assertEqual(1, len(engine.inputs))

    def test_confirmation_of_audited_record_does_not_block_forward_resume(self):
        plan = self.plan()
        with self.assertRaises(ExtractionError):
            self.run_plan(plan, self.engine(final=ExtractionError("forward interrupted")))
        self.t.store.confirm(self.record.id, verification=self.t.store.get(self.record.id).verification,
                             now="2026-11-03T00:00:00Z")
        seal = record_seal(self.t.store, self.record.id)
        self.run_plan(plan, f.FakeEngine(finals=[{"reviews": [], "memories": []}]))
        self.assertEqual(seal, record_seal(self.t.store, self.record.id))

    def test_independent_quick_or_catchup_work_cannot_be_erased(self):
        for field, value in (("learn_to", self.end), ("resume_at", self.end)):
            self.entry[field] = value
            with self.assertRaisesRegex(ValueError, "quick/catch-up"):
                self.plan()
            self.entry.pop(field)

    def test_audit_correction_can_reconcile_a_known_numbered_preference(self):
        old = "I prefer keeping 3 router backups per device."
        new = "I prefer keeping 5 router backups per device."
        self.builder.user(old).user(new).write(self.t.log_path, idle=False)
        earlier = self.t.memory.remember(old, kind="note", tags=["preference"], source="user",
                                         subjects=["router backups"], session=f.SESSION).record
        self.t.store.set_tags(earlier.id, ["preference", "provisional"], now=self.t.memory.now())
        from knowitall2.learning.transcripts import read_session
        from knowitall2.learning.dossier import build_dossiers
        original, _ = read_session(self.t.log_path)
        provenance.put(self.t.store, earlier.id, provenance.descriptor(build_dossiers(original)[0]))
        known = self.t.memory.remember(new, kind="note", tags=["preference"], source="user",
                                       subjects=["router backups"]).record
        self.t.store.confirm(known.id, verification=known.verification, now="2025-01-01T00:00:00Z")
        self.entry["offset"] = self.t.log_path.stat().st_size
        plan = self.plan()
        engine = f.FakeEngine(finals=[{"reviews": [f.review("p1", "confirm"),
            f.review("p2", "correct", text=new, kind="preference", scope="global", project="",
                     subjects=["router backups"], evidence=new, disputed="3")], "memories": []},
                                     {"reviews": [], "memories": []}])
        self.run_plan(plan, engine)
        reviewed = self.t.store.get(earlier.id)
        self.assertTrue(reviewed.status != "active" or "provisional" not in reviewed.tags)
        self.assertEqual("active", self.t.store.get(known.id).status)
        self.assertEqual(new, self.t.store.get(known.id).text)
