"""Windows can leave an actively appended Codex log's mtime unchanged."""

import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _support
from test_codex_learning import RolloutBuilder
from test_learning import FakeExtractor, LogBuilder
from knowitall2 import hooks
from knowitall2.learning import catchup, moments, transcripts
from knowitall2.learning.learner import learn
from knowitall2.learning.state import LearnerSettings, LearnerState, save_settings


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "work"
        self.now = datetime.now(timezone.utc)
        self.old = (self.now - timedelta(days=4)).timestamp()
        self.environment = mock.patch.dict(os.environ, {
            "KNOWITALL2_HOME": str(self.root / "data"), "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def stamp(self, minutes=0):
        return (self.now - timedelta(minutes=minutes)).isoformat()

    def log(self, *, name="main", day=None, modified=None, timestamp=None, source="vscode", padding=0):
        day = day or self.now
        path = self.root / "codex" / "sessions" / day.strftime("%Y/%m/%d") / f"rollout-{name}.jsonl"
        builder = RolloutBuilder(self.project, source=source).user("Check the router.").agent("Checked.")
        for record in builder.records:
            record["timestamp"] = timestamp or self.stamp()
        builder.write(path, idle=False)
        if padding:
            with path.open("ab") as stream:
                stream.write((b'{"timestamp":"' + (timestamp or self.stamp()).encode() +
                              b'","payload":"' + b"x" * padding + b'"}\n'))
        os.utime(path, (self.old if modified is None else modified,) * 2)
        return path

    def activity(self, path, **kwargs):
        return transcripts.session_activity(path, modified=self.old, now=self.now, **kwargs)

    def register(self, path):
        folder = self.root / "data" / "chats"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "s-current.json").write_text(json.dumps({"session": "main", "log": str(path)}))

    def test_stale_mtime_uses_top_level_activity(self):
        path = self.log()
        self.assertEqual(self.now.timestamp(), self.activity(path))
        self.assertEqual(self.old, path.stat().st_mtime)

    def test_nested_and_quoted_timestamps_do_not_count(self):
        path = self.log(timestamp=self.stamp(300))
        nested = {"payload": {"timestamp": self.stamp(), "text": '\n{"timestamp":"' + self.stamp() + '"}'}}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(nested) + "\n")
        self.assertAlmostEqual(self.now.timestamp() - 300 * 60, self.activity(path))

    def test_naive_malformed_and_far_future_timestamps_fall_back(self):
        for stamp in ("bad", "2026-11-02T12:00:00", self.stamp(-3)):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.old, self.activity(self.log(timestamp=stamp)))

    def test_small_clock_skew_is_allowed(self):
        self.assertEqual(self.now.timestamp() + 60, self.activity(self.log(timestamp=self.stamp(-1))))

    def test_incomplete_record_prefix_counts_but_is_not_learned(self):
        path = self.log(timestamp=self.stamp(300))
        with path.open("ab") as stream:
            stream.write(b'{"timestamp":"' + self.stamp().encode() + b'","type":"event_msg"')
        self.assertEqual(self.now.timestamp(), self.activity(path))
        session, end = transcripts.read_session(path)
        self.assertLess(end, path.stat().st_size)
        self.assertEqual(2, len(session.events))

    def test_truncated_timestamp_does_not_count(self):
        path = self.log(timestamp=self.stamp(300))
        with path.open("ab") as stream:
            stream.write(b'{"timestamp":"2026-11-')
        self.assertAlmostEqual(self.now.timestamp() - 300 * 60, self.activity(path))

    def test_extends_backwards_for_large_record_and_caps_total_bytes(self):
        path = self.log(padding=200_000)
        original = Path.open
        reads = []

        class Tracked(io.BytesIO):
            def read(self, size=-1):
                self.assert_bound(size)
                result = super().read(size)
                reads.append(len(result))
                return result

            @staticmethod
            def assert_bound(size):
                if size < 0:
                    raise AssertionError("unbounded activity read")

        def opened(target, *args, **kwargs):
            if target == path and args == ("rb",):
                with original(target, "rb") as stream:
                    return Tracked(stream.read())
            return original(target, *args, **kwargs)

        with mock.patch.object(Path, "open", opened):
            self.assertEqual(self.now.timestamp(), self.activity(path))
        self.assertGreater(sum(reads), transcripts.ACTIVITY_TAIL_BYTES)
        self.assertLessEqual(sum(reads), transcripts.ACTIVITY_MAX_BYTES)
        self.log(padding=transcripts.ACTIVITY_MAX_BYTES + 100)
        reads.clear()
        with mock.patch.object(Path, "open", opened):
            self.assertEqual(self.old, self.activity(path))
        self.assertEqual(transcripts.ACTIVITY_MAX_BYTES, sum(reads))
        reads.clear()
        with mock.patch.object(Path, "open", opened):
            self.assertEqual(self.old, self.activity(path, max_bytes=transcripts.ACTIVITY_TAIL_BYTES))
        self.assertEqual(transcripts.ACTIVITY_TAIL_BYTES, sum(reads))

    def test_missing_or_non_codex_log_retains_mtime(self):
        self.assertEqual(self.old, self.activity(self.root / "rollout-missing.jsonl"))
        with mock.patch.object(Path, "open", side_effect=AssertionError("Claude tail read")):
            self.assertEqual(self.old, self.activity(self.root / "claude.jsonl"))

    def test_current_date_stale_mtime_is_discovered(self):
        path = self.log()
        self.register(path)
        self.assertEqual(path, moments.newest_session_log(self.project, "codex"))

    def test_remembered_old_date_log_is_discovered(self):
        path = self.log(day=self.now - timedelta(days=4))
        moments._remember_found(moments.key(f"codex\n{moments._same_folder(str(self.project))}"), path)
        self.assertEqual(path, moments.newest_session_log(self.project, "codex"))

    def test_hook_state_discovers_old_date_log_after_cache_changed(self):
        path = self.log(day=self.now - timedelta(days=4))
        self.register(path)
        self.assertEqual(path, moments.newest_session_log(self.project, "codex"))

    def test_activity_ranks_both_fresh_and_stale_mtime_candidates(self):
        current = self.log(name="active", modified=self.now.timestamp() - 25 * 60)
        self.log(name="other", modified=self.now.timestamp() - 10 * 60, timestamp=self.stamp(9))
        self.assertEqual(current, moments.newest_session_log(self.project, "codex"))
        os.utime(current, (self.old,) * 2)
        self.assertEqual(current, moments.newest_session_log(self.project, "codex"))

    def test_helper_and_exec_logs_do_not_displace_interactive_chat(self):
        current = self.log(timestamp=self.stamp(2))
        self.register(current)
        for name, source in (("helper", {"subagent": "worker"}), ("exec", "exec")):
            self.log(name=name, source=source, modified=self.now.timestamp())
        self.assertEqual(current, moments.newest_session_log(self.project, "codex"))

    def test_unrelated_project_does_not_read_tail(self):
        self.log()
        with mock.patch.object(transcripts, "session_activity", side_effect=AssertionError("unrelated tail")):
            self.assertIsNone(moments.newest_session_log(self.root / "elsewhere", "codex"))

    def test_never_indexed_stale_logs_are_not_scanned(self):
        self.log(day=self.now - timedelta(days=4))
        self.log(name="today")
        with mock.patch.object(transcripts, "session_activity", side_effect=AssertionError("historical tail")):
            self.assertIsNone(moments.newest_session_log(self.project, "codex"))

    def test_stale_directory_candidates_do_not_open_headers_or_tails(self):
        older_day = self.now - timedelta(days=4)
        for number in range(200):
            self.log(name=f"idle-{number}", timestamp=self.stamp(300))
            self.log(name=f"old-idle-{number}", day=older_day, timestamp=self.stamp(300))
        current = self.log(name="current", day=older_day)
        self.register(current)
        moments._remember_found(moments.key(f"codex\n{moments._same_folder(str(self.project))}"), current)
        with mock.patch.object(transcripts, "_codex_meta", wraps=transcripts._codex_meta) as headers, \
                mock.patch.object(transcripts, "session_activity", wraps=transcripts.session_activity) as tails:
            self.assertEqual(current, moments.newest_session_log(self.project, "codex"))
        self.assertEqual(1, headers.call_count)
        self.assertEqual(1, tails.call_count)

    def test_request_now_queues_correct_long_running_chat(self):
        path = self.log()
        self.register(path)
        save_settings(LearnerSettings(enabled=True))
        message = hooks.request_now(cwd=str(self.project), agent="codex", reason="asked", start_learner=lambda **kw: True)
        self.assertIn("learning from this session now", message)
        self.assertEqual([str(path)], [item["transcript"] for item in moments.pending_requests()])

    def test_claude_new_chat_takes_over_while_remembered_chat_is_fresh(self):
        folder = self.root / "claude" / "projects" / "work"
        first = LogBuilder(self.project).user("Earlier work.").write(folder / "first.jsonl", idle=False)
        os.utime(first, (self.now.timestamp() - 60,) * 2)
        self.assertEqual(first, moments.newest_session_log(self.project, "claude-code"))
        newer = LogBuilder(self.project).user("New work.").write(folder / "newer.jsonl", idle=False)
        self.assertEqual(newer, moments.newest_session_log(self.project, "claude-code"))

    def run_learner(self, path, state=None):
        return learn(logs=[path], state=state or LearnerState(self.root / "state.json"),
                     settings=LearnerSettings(enabled=True), memory_factory=None, extractor=FakeExtractor([]),
                     dry_run=True, now=self.now)

    def test_background_waits_for_active_stale_mtime_chat(self):
        report = self.run_learner(self.log())
        self.assertEqual(1, report.active)
        self.assertEqual([], report.ready)

    def test_idle_chat_with_oversized_final_record_still_settles(self):
        report = self.run_learner(self.log(timestamp=self.stamp(300), padding=transcripts.ACTIVITY_MAX_BYTES + 100))
        self.assertEqual(0, report.active)
        self.assertEqual(1, len(report.ready))

    def test_background_does_not_tail_already_read_or_fresh_logs(self):
        path = self.log()
        state = LearnerState(self.root / "state.json")
        state.entry(path)["settled"] = path.stat().st_size
        with mock.patch.object(transcripts, "session_activity", side_effect=AssertionError("unnecessary tail")):
            self.assertEqual([], self.run_learner(path, state).ready)
            fresh = self.log(name="fresh", modified=self.now.timestamp())
            self.assertEqual(1, self.run_learner(fresh).active)

    def test_exhausted_budget_does_not_read_activity(self):
        path = self.log()
        os.utime(path, (self.now.timestamp() - 90 * 86400,) * 2)
        settings = LearnerSettings(enabled=True, max_calls_per_run=0)
        state = LearnerState(self.root / "state.json")
        with mock.patch.object(transcripts, "session_activity", side_effect=AssertionError("budget exhausted")):
            report = learn(logs=[path], state=state, settings=settings,
                           memory_factory=None, extractor=FakeExtractor([]), dry_run=False, now=self.now)
        self.assertEqual(1, report.deferred)
        self.assertEqual(0, report.calls)
        self.assertEqual(0, report.set_aside)
        self.assertEqual(0, state.entry(path)["settled"])

    def test_catchup_date_uses_activity_only_when_mtime_would_exclude_log(self):
        path = self.log()
        state = LearnerState(self.root / "state.json")
        state.entry(path)["status"] = catchup.SKIPPED
        with mock.patch.object(catchup, "session_logs", return_value=[path]):
            found = catchup._skipped(state, since=self.now.strftime("%Y-%m-%d"))
            self.assertEqual([path], [log for log, _ in found])
            with mock.patch.object(transcripts, "session_activity", side_effect=AssertionError("unnecessary tail")):
                self.assertEqual(1, len(catchup._skipped(state, since=None)))
                os.utime(path, (self.now.timestamp(),) * 2)
                self.assertEqual(1, len(catchup._skipped(state, since=self.now.strftime("%Y-%m-%d"))))
