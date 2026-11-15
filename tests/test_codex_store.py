"""Codex logs that Codex compressed (``.jsonl.zst``) or archived (``archived_sessions``) keep their identity and
progress and are read where they are now (2026-11-13: unlearned chats were lost to learning that way)."""

import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from knowitall2.learning import codex_store
from knowitall2.learning.learner import learn
from knowitall2.learning.state import LearnerSettings, LearnerState
from knowitall2.learning.transcripts import codex_listing, read_codex_session

# A real zstd frame (made by Node.js zlib.zstdCompressSync) of a four-record Codex rollout, and its decoded bytes.
PLAIN = base64.b64decode(
    "eyJ0aW1lc3RhbXAiOiAiMjAyNi0xMC0yMFQwNjoyNzowMFoiLCAidHlwZSI6ICJzZXNzaW9uX21ldGEiLCAicGF5bG9hZCI6IHsiaWQiOiAiMD"
    "FhMTU5MTEtMDAwMC03MDAwLTgwMDAtMDAwMDAwMDAwMDAxIiwgImN3ZCI6ICJDOlxcd29ya1xcaG9tZWxhYiIsICJzb3VyY2UiOiAidnNjb2Rl"
    "In19CnsidGltZXN0YW1wIjogIjIwMjYtMTAtMjBUMDY6Mjc6MDFaIiwgInR5cGUiOiAiZXZlbnRfbXNnIiwgInBheWxvYWQiOiB7InR5cGUiOi"
    "AiaXRlbV9jb21wbGV0ZWQiLCAiaXRlbSI6IHsidHlwZSI6ICJVc2VyTWVzc2FnZSIsICJjb250ZW50IjogW3sidHlwZSI6ICJ0ZXh0IiwgInRl"
    "eHQiOiAiQ2hlY2sgdGhlIE5BUyBzaGFyZS4gQmFja3VwcyBsaXZlIGluIC9zcnYvYmFja3Vwcy4ifV19fX0KeyJ0aW1lc3RhbXAiOiAiMjAyNi"
    "0xMC0yMFQwNjoyNzowMloiLCAidHlwZSI6ICJldmVudF9tc2ciLCAicGF5bG9hZCI6IHsidHlwZSI6ICJpdGVtX2NvbXBsZXRlZCIsICJpdGVt"
    "IjogeyJ0eXBlIjogIkNvbW1hbmRFeGVjdXRpb24iLCAiY29tbWFuZCI6IFsicHdzaCIsICItQ29tbWFuZCIsICJHZXQtU21iU2hhcmUgYmFja3"
    "VwcyJdLCAiYWdncmVnYXRlZF9vdXRwdXQiOiAiYmFja3VwcyAgXFxcXG5hczAxXFxiYWNrdXBzIiwgImV4aXRfY29kZSI6IDB9fX0KeyJ0aW1l"
    "c3RhbXAiOiAiMjAyNi0xMC0yMFQwNjoyNzowM1oiLCAidHlwZSI6ICJldmVudF9tc2ciLCAicGF5bG9hZCI6IHsidHlwZSI6ICJpdGVtX2NvbX"
    "BsZXRlZCIsICJpdGVtIjogeyJ0eXBlIjogIkFnZW50TWVzc2FnZSIsICJjb250ZW50IjogW3sidHlwZSI6ICJUZXh0IiwgInRleHQiOiAiVGhl"
    "IGJhY2t1cHMgc2hhcmUgaXMgb24gbmFzMDEuIn1dfX19Cg==")
PACKED = base64.b64decode(
    "KLUv/WBbAo0LAJaUQSQwqegBw3TN5quyPIv2fLEXC0lHlS8U2ElIcNFGXS5LAwsrDEI3ADcAOAAMY4wxrQ0ENNgYS66s4wdWiQIn+D0dWqV7e0"
    "+U511YgTGnaFyCOVjDsMYkGtbwOp4mfO8pnysBtJ/W2BB07wOqPeHpb2maNTqzX0I+nadtdTNcF/dTo1uRqqrzLgyBUs8avRehquQ9x5DExgQU"
    "AZFLJo60yq9iTwoeCuesjOZ6XHyAICzKEUvltiWl9amKREiwh6Zg5NtYIGxHv5HYEYPX/aR/YR1/huu6HhcfltZ1uZHD8bShBQBb57ycNO/yXm"
    "MO6Xvv7XI1fVWdpx9CTUEQ5F0evA3IbDDfBkwrIFACQQrnDuaFnWsAgBRYx9KHPz8qIOQHOZfDkydIdiV7jUrBhfG3wZL3aoqGjfu5RgbroffY"
    "RBsRa7UFL3IGP2Bu8oIJgTqAxKDNCZAKch6X9QM5J0P6WhdW4N4Zgxgo2d8tbVi0DA==")
NAME = "rollout-2026-10-20T06-27-00-01a15911-0000-7000-8000-000000000001.jsonl"


def fake_decoder(data: bytes = PLAIN):
    return ("test decoder", lambda path: data)


class StoredLogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "codex"
        self.sessions = self.home / "sessions"
        self.plain = self.sessions / "2026" / "10" / "20" / NAME
        self.plain.parent.mkdir(parents=True)
        self.archive = self.home / codex_store.ARCHIVE
        codex_store.reset_decoder()
        self.addCleanup(codex_store.reset_decoder)
        self.state = LearnerState(Path(self.temporary.name) / "state.json")

    def use(self, decoder) -> None:
        patch = mock.patch.object(codex_store, "_decoder", decoder)
        patch.start()
        self.addCleanup(patch.stop)
        codex_store._cache = None

    def compressed(self, *, archived: bool = False, days: float = 30) -> Path:
        folder = self.archive if archived else self.plain.parent
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (NAME + codex_store.COMPRESSED)
        path.write_bytes(PACKED)
        moment = time.time() - days * 86400
        os.utime(path, (moment, moment))
        return path


class IdentityAndDiscoveryTests(StoredLogTestCase):
    def test_every_form_names_the_plain_sessions_spelling(self) -> None:
        for form in (self.plain, self.plain.with_name(NAME + ".zst"), self.archive / NAME,
                     self.archive / (NAME + ".zst")):
            with self.subTest(form=form.name, folder=form.parent.name):
                self.assertEqual(self.plain, codex_store.canonical(form))
        other = Path(self.temporary.name) / "claude" / "session.jsonl"
        self.assertEqual(other, codex_store.canonical(other))

    def test_stored_listing_finds_compressed_and_archived_logs_once_by_their_plain_name(self) -> None:
        packed = self.compressed()
        self.assertEqual([], codex_listing(self.sessions))   # a chat in use is a plain log; nothing else is listed
        self.assertEqual([self.plain], [path for _, path in codex_listing(self.sessions, stored=True)])
        self.plain.write_bytes(PLAIN)   # Codex compressing: both forms at once
        self.assertEqual([self.plain], [path for _, path in codex_listing(self.sessions, stored=True)])
        self.plain.unlink()
        packed.replace(self.archive / packed.name) if self.archive.is_dir() else (
            self.archive.mkdir(), packed.replace(self.archive / packed.name))
        self.assertEqual([self.plain], [path for _, path in codex_listing(self.sessions, stored=True)])

    def test_a_compressed_log_keeps_its_entry_and_the_entry_survives_saving(self) -> None:
        entry = self.state.entry(self.plain)
        entry.update({"offset": 300, "settled": 300, "status": "partial", "session_id": "s",
                      "updated_at": (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()})
        self.compressed()
        self.state.save()   # saving forgets a long-unchanged log only when no form of it exists any more
        state = LearnerState(self.state.path)
        self.assertIs(state.entry(self.plain.with_name(NAME + ".zst")), state.entry(self.plain))
        self.assertEqual(300, state.entry(self.archive / NAME)["settled"])
        self.assertEqual(1, len(state.logs))
        self.plain.with_name(NAME + ".zst").unlink()   # gone in every form: forgotten as before
        state.save()
        self.assertEqual({}, LearnerState(self.state.path).logs)

    def test_an_archived_log_finds_the_one_entry_recorded_under_another_days_folder(self) -> None:
        elsewhere = self.sessions / "2026" / "10" / "19" / NAME
        self.state.entry(elsewhere).update({"offset": 120, "settled": 120})
        self.assertEqual(120, self.state.entry(self.archive / (NAME + ".zst"))["settled"])
        self.assertEqual(1, len(self.state.logs))


    def test_a_stored_log_keeps_the_hold_on_independent_work_in_another_recorded_spelling(self) -> None:
        # Review 2026-11-13: without its plain file, matching skipped the alias group and cleared the hold.
        from knowitall2.learning.log_identity import path_key

        self.compressed()
        alias = str(Path(self.temporary.name) / "spelling" / NAME)
        self.state.logs = {str(self.plain): {"offset": 100, "settled": 100, "session_id": "s", "log_path": str(self.plain)},
                           alias: {"offset": 200, "settled": 0, "session_id": "s", "log_path": alias,
                                   "pending_reviews": {"final": {"start": 0}}}}
        self.state.log_aliases = {path_key(self.plain): str(self.plain), path_key(alias): str(self.plain)}
        entry = self.state.entry(self.plain)
        self.assertEqual(100, entry["offset"])
        self.assertEqual([alias], entry["identity_conflicts"])

    def test_an_archived_log_found_by_name_keeps_the_hold_of_that_entrys_alias(self) -> None:
        # Review 2026-11-13: the alias group was gathered before the name fallback, so it was skipped there.
        from knowitall2.learning.log_identity import path_key

        self.compressed(archived=True)
        elsewhere = str(self.sessions / "2026" / "10" / "19" / NAME)
        alias = str(Path(self.temporary.name) / "spelling" / "renamed.jsonl")
        self.state.logs = {elsewhere: {"offset": 100, "settled": 100, "session_id": "s", "log_path": elsewhere},
                           alias: {"offset": 300, "settled": 0, "session_id": "s", "log_path": alias,
                                   "pending_reviews": {"final": {"start": 0}}}}
        self.state.log_aliases = {path_key(elsewhere): elsewhere, path_key(alias): elsewhere}
        entry = self.state.entry(self.archive / (NAME + ".zst"))
        self.assertEqual(100, entry["offset"])
        self.assertEqual([alias], entry["identity_conflicts"])
        if os.name == "nt":   # an extended spelling of the same Codex home is the same home
            extended = Path("\\\\?\\" + str(self.plain))
            self.assertEqual(codex_store.home_of(self.plain), codex_store.home_of(extended))

    def test_several_entries_with_an_archived_logs_name_are_held_not_started_again(self) -> None:
        self.compressed(archived=True)
        for day in ("18", "19"):
            other = self.sessions / "2026" / "10" / day / NAME
            self.state.logs[str(other)] = {"offset": int(day) * 10, "settled": int(day) * 10, "session_id": "s",
                                           "log_path": str(other)}
        stranger = Path(self.temporary.name) / "other-home" / "sessions" / "2026" / "10" / "20" / NAME
        entry = self.state.entry(self.archive / (NAME + ".zst"))
        self.assertTrue(entry.get("identity_conflicts"))
        self.assertEqual(2, len(self.state.logs))
        self.assertEqual(0, self.state.entry(stranger)["offset"])   # another Codex home is another transcript


class ReadingTests(StoredLogTestCase):
    def test_size_comes_from_the_frame_and_the_bytes_are_the_transcript(self) -> None:
        packed = self.compressed()
        self.assertEqual(len(PLAIN), codex_store.frame_size(packed))
        self.use(fake_decoder())
        status = codex_store.status(self.plain)
        self.assertEqual((len(PLAIN), packed, True), (status.st_size, status.physical, status.compressed))
        with codex_store.open_log(self.plain) as stream:
            self.assertEqual(PLAIN, stream.read())

    def test_a_real_decoder_on_this_computer_decodes_the_fixture(self) -> None:
        found = codex_store.decoder()
        if found is None:
            self.skipTest("no zstd decoder here")
        self.compressed()
        with codex_store.open_log(self.plain) as stream:
            data = stream.read()
        self.assertEqual(hashlib.sha256(PLAIN).hexdigest(), hashlib.sha256(data).hexdigest(), found[0])

    def test_a_compressed_log_reads_like_its_plain_original(self) -> None:
        self.use(fake_decoder())
        self.compressed()
        stored, end = read_codex_session(self.plain.with_name(NAME + ".zst"))
        self.plain.write_bytes(PLAIN)
        original, plain_end = read_codex_session(self.plain)
        self.assertEqual((plain_end, [(e.kind, e.text, e.start, e.end) for e in original.events]),
                         (end, [(e.kind, e.text, e.start, e.end) for e in stored.events]))
        self.assertEqual(self.plain, stored.path)
        self.assertIn("Backups live in /srv/backups", stored.events[0].text)

    def test_without_a_decoder_or_with_a_damaged_frame_reading_fails_and_never_reads_empty(self) -> None:
        self.compressed()
        for decoder in (None, fake_decoder(PLAIN[:-10])):
            with self.subTest(decoder=decoder and decoder[0]):
                self.use(decoder)
                self.assertEqual(len(PLAIN), codex_store.status(self.plain).st_size)
                with self.assertRaises(codex_store.UnreadableLog):
                    read_codex_session(self.plain)
        self.plain.with_name(NAME + ".zst").write_bytes(b"not zstd at all")
        with self.assertRaises(codex_store.UnreadableLog):
            codex_store.status(self.plain)

    def test_a_recovery_hash_of_a_compressed_log_is_its_transcripts_hash(self) -> None:
        from knowitall2.learning.recovery import prefix_hash

        self.use(fake_decoder())
        self.compressed()
        self.assertEqual(hashlib.sha256(PLAIN).hexdigest(), prefix_hash(self.plain, len(PLAIN)))


class LearningTests(StoredLogTestCase):
    def setUp(self) -> None:
        super().setUp()
        from knowitall2.memory import Memory
        from knowitall2.store import Store
        from test_learning import FakeExtractor

        self.store = Store.in_memory()
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store, agent="learner")
        self.settings = LearnerSettings(enabled=True)
        self.extractor_type = FakeExtractor

    def run_learner(self, extractor):
        return learn(logs=[self.plain], state=self.state, settings=self.settings,
                     memory_factory=lambda: self.memory, extractor=extractor, dry_run=False)

    def test_a_never_started_compressed_log_older_than_two_weeks_is_set_aside_not_read(self) -> None:
        # One learning knew and missed is stamped (doctor reports it); one first seen already that old is history
        # from before KnowItAll2 (2026-11-13: Codex logs compressed before it was installed) and is not.
        # Whether learning knew it comes from when it first saw it (review 2026-11-13), not from whether an entry
        # existed when a run began: a run out of budget makes entries for logs it does not read.
        self.use(fake_decoder())
        self.compressed(days=30)
        seen_while_recent = (datetime.now(timezone.utc) - timedelta(days=25)).isoformat()
        for case, seen in (("first seen now", "make"), ("seen while recent", seen_while_recent),
                           ("entry from before seen_at", None)):
            with self.subTest(case=case):
                self.state.logs.clear()
                if seen != "make":
                    entry = self.state.entry(self.plain)
                    entry.pop("seen_at", None)
                    if seen:
                        entry["seen_at"] = seen
                else:
                    self.state.entry(self.plain)   # as a run out of budget would, before this one
                report = self.run_learner(self.extractor_type([[]]))
                self.assertEqual((1, 0), (report.set_aside, report.calls))
                entry = self.state.entry(self.plain)
                self.assertEqual((len(PLAIN), "baseline"), (entry["settled"], entry["status"]))
                self.assertEqual(case != "first seen now", "set_aside_at" in entry)

    def test_a_started_log_resumes_from_its_place_instead_of_being_set_aside(self) -> None:
        # Review 2026-11-13: setting aside a partly learned log lost its place, and catching up read it from 0.
        self.use(fake_decoder())
        self.compressed(days=30)
        first_line = PLAIN.index(b"\n") + 1
        self.state.entry(self.plain).update({"offset": first_line, "settled": first_line, "status": "partial"})
        extractor = self.extractor_type([[]])
        report = self.run_learner(extractor)
        self.assertEqual((0, 1), (report.set_aside, report.calls))
        self.assertEqual(len(PLAIN), self.state.entry(self.plain)["settled"])
        self.assertIn("Backups live in /srv/backups", extractor.dossiers[0].text)
        self.assertEqual(1, len(self.state.logs))

    def test_a_set_aside_log_recovered_from_its_start_is_no_longer_counted_as_set_aside(self) -> None:
        # 2026-11-13: 18 chats recovered by exact ranges were still reported as set aside unlearned.
        from knowitall2.learning.recovery import queue, range_id
        from test_learning import FakeEngine

        self.use(fake_decoder())
        self.compressed(days=30)
        self.state.entry(self.plain).pop("seen_at", None)   # known to learning before: a stamped miss
        self.run_learner(self.extractor_type([[]]))
        self.assertEqual(1, self.state.backlog(quiet_before=time.time())["set_aside"])
        item = {"path": str(self.plain), "session": "01a15911-0000-7000-8000-000000000001", "start": 0,
                "event_index": 0, "end": len(PLAIN), "end_event_index": 0,
                "sha256": hashlib.sha256(PLAIN).hexdigest(), "scope": "exact_range"}
        item["id"] = range_id(item)
        self.assertEqual(1, queue({"format": 1, "ranges": [item]}, self.state))
        report = learn(logs=[self.plain], state=self.state, settings=self.settings,
                       memory_factory=lambda: self.memory, extractor=FakeEngine(), dry_run=False)
        self.assertIsNone(report.blocked)
        entry = self.state.entry(self.plain)
        self.assertEqual([item["id"]], entry["recovery_completed"])
        self.assertIn("caught_up_at", entry)
        self.assertEqual(0, self.state.backlog(quiet_before=time.time())["set_aside"])

    def test_an_unreadable_queued_recovery_waits_and_other_logs_are_still_learned(self) -> None:
        # Review 2026-11-13: the recovery's hash check raised before the unreadable handling and ended the run.
        from test_learning import LogBuilder, text

        self.use(None)
        self.compressed(days=3)
        queued = [{"id": "r", "start": 0, "end": len(PLAIN), "sha256": hashlib.sha256(PLAIN).hexdigest(),
                   "cursor": 0, "cursor_event_index": 0}]
        self.state.entry(self.plain).update({"offset": 0, "settled": 0, "recovery_ranges": queued})
        healthy = (LogBuilder(Path(self.temporary.name)).user("check the switch")
                   .assistant(text("The switch is fine.")).write(Path(self.temporary.name) / "claude" / "chat.jsonl"))
        report = learn(logs=[self.plain, healthy], state=self.state, settings=self.settings,
                       memory_factory=lambda: self.memory, extractor=self.extractor_type([[]]), dry_run=False)
        self.assertIsNone(report.blocked)
        self.assertEqual((1, 1), (report.unreadable, report.calls))
        self.assertEqual(queued, self.state.entry(self.plain)["recovery_ranges"])

    def test_an_unreadable_compressed_log_keeps_its_progress_and_is_reported(self) -> None:
        from knowitall2 import doctor

        self.use(None)
        self.compressed(days=3)
        self.state.entry(self.plain).update({"offset": 10, "settled": 0, "status": "partial", "session_id": "s"})
        report = self.run_learner(self.extractor_type([[]]))
        self.assertEqual((1, 0), (report.unreadable, report.calls))
        self.assertIn("cannot be read now", report.describe(dry_run=False))
        entry = self.state.entry(self.plain)
        self.assertEqual((10, 0, "partial"), (entry["offset"], entry["settled"], entry["status"]))
        self.state.save()
        found = LearnerState(self.state.path).backlog(quiet_before=time.time() - 3600)
        self.assertEqual((1, 0), (found["unreadable"], found["waiting"]))
        with mock.patch("knowitall2.learning.state.LearnerState", return_value=LearnerState(self.state.path)), \
                mock.patch("knowitall2.learning.state.load_settings", return_value=self.settings):
            check = doctor._learning_backlog_check()
        self.assertFalse(check.ok)
        self.assertIn("compressed cannot be read", check.detail)
        self.assertIn("zstd", check.fix)


if __name__ == "__main__":
    unittest.main()
