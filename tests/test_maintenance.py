import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
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


class StatusTests(MaintenanceTestCase):
    def test_a_status_lasts_a_week_unless_it_is_confirmed(self) -> None:
        # What the catalog files as a status (a current state that will change) goes out of date: the 2026-10-12
        # review found one in six new memories was status. It is kept out of briefings, and goes after a week.
        from knowitall2.learning.maintenance import retire_old_status

        def status(text: str, **options):
            record = self.memory.remember(text, project_path=self.project, scope="project", **options).record
            self.store.set_note(record.id, headline=text, system_id=None, facet="status", written_by="catalog",
                                now=self.clock())
            return record

        old = status("The alpha nightly build is failing on the lint step.")
        said = status("The alpha release is frozen until the audit finishes.", kind="decision", source="user")
        self.clock.value = "2026-10-01T12:00:00Z"
        fresh = status("The alpha staging server is down for upgrades.")
        renewed = status("The alpha queue holds 40 jobs.")
        self.clock.value = "2026-10-03T12:00:00Z"
        self.memory.confirm(renewed.id)
        self.clock.value = "2026-10-05T12:00:00Z"
        self.assertEqual(1, retire_old_status(self.memory))
        self.assertEqual(["retired", "active", "active", "active"],
                         [self.store.get(record.id).status for record in (old, said, fresh, renewed)])
        self.assertIn("status", self.store.get(old.id).retired_reason)


class StatusKindTests(MaintenanceTestCase):
    def test_only_a_fact_or_note_filed_as_status_goes_after_a_week(self) -> None:
        # Candidate review, C-L9: decisions, procedures, and lessons are never statuses.
        from knowitall2.learning.maintenance import retire_old_status

        kept = self.memory.remember("To restart alpha, run make restart.", project_path=self.project, scope="project",
                                    kind="procedure").record
        gone = self.memory.remember("The alpha build is red today.", project_path=self.project, scope="project").record
        for record in (kept, gone):
            self.store.set_note(record.id, headline=record.text, system_id=None, facet="status", written_by="catalog",
                                now=self.clock())
        self.clock.value = "2026-10-10T12:00:00Z"
        self.assertEqual(1, retire_old_status(self.memory))
        self.assertEqual(("active", "retired"), (self.status(kept.id), self.status(gone.id)))
        self.assertEqual(1, len(self.store.events(kinds=["change"])))

    def test_memory_reviews_never_count_as_group_reviews(self) -> None:
        # Candidate review, C-L1: they set the groups' 6-hour throttle and hid a stalled maintenance.
        from knowitall2.learning.maintenance import mark_reviewed

        record = self.memory.remember("The NAS is nas01.").record
        mark_reviewed(self.memory, [record])
        self.assertIsNone(self.store.last_review("global"))


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
        short = self.save("The NAS is nas01 at 10.0.0.5 and is reached over SSH.")
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

    def test_near_repeats_that_each_add_a_detail_are_combined_without_losing_one(self) -> None:
        # 2026-10-12: the gate turned down about 400 duplicate findings, each copy holding a detail the kept one
        # lacked; they stayed as repeats. Combining keeps every detail in one memory.
        first = self.save("The NAS nas01 shares media over SMB.", subjects=["NAS"])
        second = self.save("nas01 exports /volume1/media read-only.", subjects=["nas01"])
        other = self.save("The router is rtr01.")
        whole = "The NAS nas01 shares media over SMB, exporting /volume1/media read-only."
        combine = {"kind": "combine", "keep_id": "", "ids": [first.id, second.id], "text": whole, "reason": "Same share."}
        report = self.run_maintenance(FakeReviewer([[combine]]))
        self.assertEqual({"combined": 2}, dict(report.changes))
        holder = self.store.get(self.store.get(first.id).superseded_by)
        self.assertEqual((whole, "unverified", ["NAS", "nas01"]), (holder.text, holder.verification,
                                                                   list(holder.subjects)))
        self.assertEqual(holder.id, self.store.get(second.id).superseded_by)
        self.assertEqual("active", self.status(other.id))
        self.assertIn("combined into", self.store.get(second.id).retired_reason)

    def test_the_review_steers_near_repeats_that_each_hold_a_detail_to_combine(self) -> None:
        # The first live catch-up (2026-10-14): 303 duplicate findings were refused as "not the same statement",
        # because each copy held a detail; only 45 were combined.
        prompt = " ".join(REVIEW_PROMPT.split())
        self.assertIn('Before you report a "duplicate", check each copy against the one you keep', prompt)
        self.assertIn('report "combine" instead, never "duplicate"', prompt)
        self.assertIn("A duplicate whose copy holds such a detail is refused and changes nothing", prompt)

    def test_a_duplicate_refused_because_the_copies_differ_asks_nothing_and_is_counted(self) -> None:
        first = self.save("The NAS nas01 shares media over SMB.")
        second = self.save("nas01 exports /volume1/media read-only.")
        self.save("The router is rtr01.")
        reviewer = FakeReviewer([[{"kind": "duplicate", "keep_id": first.id, "ids": [second.id], "reason": "NAS."}]])
        report = self.run_maintenance(reviewer)
        self.assertEqual((1, 1), (report.calls, report.near_repeats))
        self.assertEqual(("active", "active"), (self.status(first.id), self.status(second.id)))
        self.assertEqual([], self.store.open_questions(limit=5))
        self.assertIn("Near-repeats found as duplicates but each holding a detail, left for combining: 1.",
                      report.describe(dry_run=False))

    def test_a_combination_that_loses_a_detail_or_mixes_evidence_is_refused(self) -> None:
        first = self.save("The NAS nas01 shares media over SMB.")
        second = self.save("nas01 exports /volume1/media read-only.")
        observed = self.save("nas01 answers on port 445.", source="observed")
        said = self.save("Never expose nas01 to the internet.", kind="decision", source="user", scope="global")
        cases = [
            ([first.id, second.id], "The NAS nas01 shares media over SMB, read-only.", "drops a name"),
            ([first.id, observed.id], "The NAS nas01 shares media over SMB on port 445.", "verified differently"),
            ([first.id, said.id], "The NAS nas01 shares media over SMB and is never exposed to the internet.",
             "the user's own words"),
            ([first.id, second.id], "", "missing or too long"),
        ]
        for ids, text, why in cases:
            with self.subTest(why=why):
                finding = {"kind": "combine", "keep_id": "", "ids": ids, "text": text, "reason": ""}
                report = self.run_maintenance(FakeReviewer([[finding]]))
                self.assertTrue(any(why in item for item in report.left_alone), report.left_alone)
                self.assertTrue(all(self.status(item) == "active" for item in ids))
                self.store.connection.execute("DELETE FROM reviews")

    def test_a_combination_keeps_the_words_that_carry_meaning(self) -> None:
        # Candidate review, C-M8: "nightly", "encrypted", "restic", "offsite" could vanish.
        from knowitall2.learning.maintenance import keeps_everything

        sources = ["NAS backups run nightly at 02:00 to /volume1/backups and are encrypted with restic.",
                   "NAS backups are kept for 30 days on the offsite bucket."]
        self.assertFalse(keeps_everything(sources, "NAS backups run at 02:00 to /volume1/backups, kept 30 days."))
        self.assertTrue(keeps_everything(sources, "NAS backups run nightly at 02:00 to /volume1/backups, encrypted "
                                                  "with restic, and are kept for 30 days on the offsite bucket."))

    def test_a_combined_memory_is_dated_like_its_newest_input_and_drops_a_dispute(self) -> None:
        first = self.save("The NAS nas01 shares media over SMB.", subjects=["NAS"])
        self.clock.value = "2026-09-28T12:00:00Z"
        second = self.save("nas01 exports /volume1/media read-only.", subjects=["nas01"])
        self.store.set_tags(second.id, ["disputed"], now=self.clock())
        self.clock.value = "2026-10-01T12:00:00Z"
        whole = "The NAS nas01 shares media over SMB, exporting /volume1/media read-only."
        self.run_maintenance(FakeReviewer([[{"kind": "combine", "keep_id": "", "ids": [first.id, second.id],
                                             "text": whole, "reason": ""}]]))
        holder = self.store.get(self.store.get(first.id).superseded_by)
        self.assertEqual(("2026-09-28T12:00:00Z", ()), (holder.created_at, tuple(holder.tags)))

    def test_a_better_verified_copy_is_never_retired_by_a_fuller_one(self) -> None:
        full = self.save("The NAS is nas01 at 10.0.0.5, reached over SSH as admin.")
        stated = self.save("The NAS is nas01.", source="user")
        also = self.save("The NAS is nas01 at 10.0.0.5; reached over SSH as admin.")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": full.id, "ids": [stated.id, also.id], "reason": "Same NAS."},
        ]]))
        self.assertEqual({"kept (better verified)": 1, "merged duplicate": 1}, dict(report.changes))
        self.assertEqual(("active", "active", "superseded"), tuple(map(self.status, (full.id, stated.id, also.id))))

    def test_outdated_memories_follow_the_evidence_gates(self) -> None:
        old_seen = self.save("The CI runner is ci01.", source="observed")
        old_guess = self.save("The wiki is at wiki-old.example.test.")
        self.clock.value = "2026-09-27T13:00:00Z"
        new_guess = self.save("The CI runner moved to ci02.")
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

    def test_a_status_the_user_mentioned_goes_but_what_they_want_stays(self) -> None:
        # What the user said once is context, not law (the user, 2026-10-11): a passing status goes like any other,
        # with no question; what the user wants is not a status.
        stated = self.save("I am rebuilding the NAS this week.", source="user")
        wanted = self.save("Rebuild the NAS only on weekends.", kind="note", source="user", tags=["preference"])
        status = self.save("The VPN is currently disconnected.")
        lesson = self.save("Restarting dnsmasq fixed DHCP.", kind="lesson")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "snapshot", "keep_id": "", "ids": [stated.id, wanted.id, status.id, lesson.id],
             "reason": "Temporary."},
        ]]))
        self.assertEqual({"retired snapshot": 2, "kept (the user's own)": 1, "kept (lesson)": 1}, dict(report.changes))
        self.assertEqual(("retired", "active", "retired", "active"),
                         tuple(map(self.status, (stated.id, wanted.id, status.id, lesson.id))))
        self.assertEqual(0, self.store.count_open_questions())

    def test_merged_copies_of_a_preference_count_each_chat_once(self) -> None:
        # Review 2026-10-11, R-L3: the user's preference said in two chats counted one chat once merged, and when
        # the newer words took over the merge, the older words' chat dropped out. A copy from the same chat, or one
        # saved without a chat (an agent's remember), still adds none (N-M2).
        def said(text: str, session: str | None):
            return self.memory.remember(text, kind="note", tags=["preference"], source="user",
                                        project_path=self.project, session=session).record

        self.clock.value = "2026-09-01T10:00:00Z"
        first = said("Keep deploy notes short.", "chat-1")
        same_chat = said("Keep deploy notes: short.", "chat-1")
        no_chat = said("Keep deploy notes, short.", None)
        empty = said("Keep deploy notes; short.", "")
        second = said("Keep deploy-notes short.", "chat-2")
        older = said("Squash fixup commits before merging.", "chat-4")
        self.clock.value = "2026-10-01T10:00:00Z"
        newer = said("Squash fixup commits, before merging.", "chat-5")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": first.id, "ids": [same_chat.id, no_chat.id, empty.id, second.id],
             "reason": "Same preference."},
            {"kind": "duplicate", "keep_id": older.id, "ids": [newer.id], "reason": "Same preference."},
        ]]))
        self.assertEqual({"merged duplicate": 5}, dict(report.changes))
        self.assertEqual(("active", "superseded"), (self.status(first.id), self.status(older.id)))
        self.assertEqual({"chat-1", "chat-2"}, self.store.said_in_sessions(first.id))
        self.assertEqual({"chat-4", "chat-5"}, self.store.said_in_sessions(newer.id))

    def test_the_users_rules_merge_and_a_newer_one_replaces_an_older(self) -> None:
        tag = self.save("Always tag releases before deploying them.", kind="rule", source="user")
        tag_again = self.save("Always tag releases, before deploying them.", kind="rule", source="user")
        old_host = self.save("Deploy from the build01 host.", kind="rule", source="user")
        self.clock.value = "2026-09-27T13:00:00Z"
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
        self.assertEqual({"merged duplicate": 3, "replaced outdated": 1}, dict(report.changes))
        for record in (tag, new_host, signed_tags, sign):
            self.assertEqual("active", self.status(record.id))
        # A note that says what a rule says goes into the rule, never the other way round.
        for record in (tag_again, old_host, noted, sign_again):
            self.assertEqual("superseded", self.status(record.id))
        self.assertEqual(signed_tags.id, self.store.get(noted.id).superseded_by)
        self.assertEqual(0, self.store.count_open_questions())

    def test_a_memory_is_outdated_only_by_a_newer_one_at_least_as_strong(self) -> None:
        # Review 2026-10-11, L-M4 and N-M5: the model took the September preference for the newer one, and the
        # user's October words, which say the opposite, were replaced by it.
        self.clock.value = "2026-09-01T10:00:00Z"
        september = self.save("Use merge commits for feature branches in the homelab repo.", kind="note",
                              source="user", tags=["preference"])
        stated = self.save("The build server is build01.", source="user")
        self.clock.value = "2026-10-10T10:00:00Z"
        october = self.save("Use rebase, not merge commits, for feature branches in the homelab repo.", kind="note",
                            source="user", tags=["preference"])
        seen = self.save("The build server is build02.", source="observed")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "outdated", "keep_id": september.id, "ids": [october.id], "reason": "The older one is current."},
            {"kind": "outdated", "keep_id": seen.id, "ids": [stated.id], "reason": "Moved."},
        ]]))
        self.assertEqual({"kept (not newer)": 1, "question": 1}, dict(report.changes))
        self.assertEqual(["active"] * 4, [self.status(item.id) for item in (september, october, stated, seen)])
        self.assertEqual([f"outdated [{october.id}] by [{september.id}] left alone: not newer "
                          "(the model said: The older one is current.)"], report.left_alone)

    def test_duplicates_merge_only_when_they_say_the_same_thing(self) -> None:
        # Review 2026-10-11, L-M4: two different rules of the user's were merged as duplicates, and one was lost.
        force = self.save("Always ask before force-pushing any branch.", kind="rule", source="user")
        main = self.save("Never push directly to the main branch of a repository with CI.", kind="rule",
                         source="user")
        early = self.save("The nightly backup of the build server to the NAS share starts at 02:00 every day, "
                          "keeps fourteen daily copies, and mails a short report to the admin mailbox afterwards.")
        late = self.save("The nightly backup of the build server to the NAS share starts at 04:00 every day, "
                         "keeps fourteen daily copies, and mails a short report to the admin mailbox afterwards.")
        squash = self.save("In the homelab repo, squash fixup commits before opening a merge request, so the "
                           "history stays short and readable for every reviewer who looks at it later.",
                           kind="note", source="user", tags=["preference"])
        never = self.save("In the homelab repo, never squash fixup commits before opening a merge request, so the "
                          "history stays short and readable for every reviewer who looks at it later.",
                          kind="note", source="user", tags=["preference"])
        sign = self.save("Sign every commit with GPG.", kind="rule", source="user")
        sign_again = self.save("Sign every commit, with GPG", kind="rule", source="user")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": force.id, "ids": [main.id], "reason": "Both are about pushing."},
            {"kind": "duplicate", "keep_id": early.id, "ids": [late.id], "reason": "Same backup."},
            {"kind": "duplicate", "keep_id": squash.id, "ids": [never.id], "reason": "Same preference."},
            {"kind": "duplicate", "keep_id": sign.id, "ids": [sign_again.id], "reason": "Same rule."},
        ]]))
        self.assertEqual({"kept (not the same statement)": 3, "merged duplicate": 1}, dict(report.changes))
        self.assertEqual(["active"] * 7, [self.status(item.id) for item in (force, main, early, late, squash, never,
                                                                              sign)])
        self.assertEqual("superseded", self.status(sign_again.id))
        self.assertEqual([f"duplicate [{main.id}] of [{force.id}]", f"duplicate [{late.id}] of [{early.id}]",
                          f"duplicate [{never.id}] of [{squash.id}]"],
                         [item.split(" left alone")[0] for item in report.left_alone])

    def test_learned_copies_in_other_words_merge_unless_a_number_or_a_not_differs(self) -> None:
        # One quirk was learned 66 times in different words (2026-10-11): learned copies merge on the model's word,
        # as long as the copy's numbers are in the kept memory and neither turns the other around.
        full = self.save("The build host's Docker daemon listens on port 2376 with TLS; connect with "
                         "`docker --tlsverify -H build1.example.test:2376`.")
        terse = self.save("On build1.example.test, Docker listens on port 2376 with TLS.")
        other_port = self.save("Docker on build1.example.test is reached over TLS on port 2377.")
        turned = self.save("The build host's Docker daemon does not listen on port 2376 with TLS.")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": full.id, "ids": [terse.id, other_port.id, turned.id],
             "reason": "Same daemon."},
        ]]))
        self.assertEqual({"merged duplicate": 1, "kept (not the same statement)": 2}, dict(report.changes))
        self.assertEqual(["active", "superseded", "active", "active"],
                         [self.status(item.id) for item in (full, terse, other_port, turned)])

    def test_copies_naming_another_product_are_no_duplicates(self) -> None:
        # Review 2026-10-11, R-L2: learned copies naming PostgreSQL and MySQL merged on the model's word, so a fact
        # that contradicts another was retired as a duplicate instead of raising a conflict. A copy's proper names
        # must all be in the kept memory, in any case.
        postgres = self.save("The inventory service stores its data in PostgreSQL on the shared database host.")
        mysql = self.save("The inventory service keeps its data in MySQL.")
        lower = self.save("The inventory service stores its data in postgresql.")
        coral = self.save("The NVR runs Frigate with the Coral TPU detector.")
        openvino = self.save("The NVR runs Frigate with the OpenVINO detector.")
        # In nearly the same words too: the copy names another product.
        long_postgres = self.save("The inventory service in the homelab stores all of its order and customer data in "
                                  "PostgreSQL on the shared database host, with nightly dumps kept for a week.")
        long_mysql = self.save("The inventory service in the homelab stores all of its order and customer data in "
                               "MySQL on the shared database host, with nightly dumps kept for a week.")
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": postgres.id, "ids": [mysql.id, lower.id], "reason": "Same database."},
            {"kind": "duplicate", "keep_id": coral.id, "ids": [openvino.id], "reason": "Same detector."},
            {"kind": "duplicate", "keep_id": long_postgres.id, "ids": [long_mysql.id], "reason": "Same database."},
        ]]))
        self.assertEqual({"merged duplicate": 1, "kept (not the same statement)": 3}, dict(report.changes))
        self.assertEqual(["active", "active", "superseded", "active", "active", "active", "active"],
                         [self.status(item.id) for item in (postgres, mysql, lower, coral, openvino, long_postgres,
                                                            long_mysql)])

    def test_the_users_newer_words_hold_a_duplicate_of_theirs(self) -> None:
        self.clock.value = "2026-09-01T10:00:00Z"
        older = self.save("Keep deploy notes short.", kind="note", source="user", tags=["preference"])
        self.clock.value = "2026-10-01T10:00:00Z"
        newer = self.save("Keep deploy-notes short.", kind="note", source="user", tags=["preference"])
        report = self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": older.id, "ids": [newer.id], "reason": "Same preference."},
        ]]))
        self.assertEqual({"merged duplicate": 1}, dict(report.changes))
        self.assertEqual(("superseded", newer.id), (self.status(older.id), self.store.get(older.id).superseded_by))

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

    def test_only_groups_with_a_new_or_changed_memory_are_reviewed_again(self) -> None:
        # 2026-10-13: groups were rebuilt from scratch, so one change reshuffled many and nearly everything looked
        # due every day (148 groups waiting against 8 calls). Each memory's review is remembered instead.
        saved = [self.save(f"The homelab host {name} runs service number {number} for the lab.")
                 for number, name in enumerate(f"h{index:02d}" for index in range(45))]
        first = self.run_maintenance(FakeReviewer([[]] * 5))
        self.assertEqual(2, first.calls)
        self.memory.forget(saved[3].id, reason="gone")
        self.assertEqual({"unchanged"}, {status for _, status in plan(self.memory)})
        self.clock.value = "2026-09-28T12:00:00Z"
        self.save("The homelab host n99 runs the new metrics service for the lab.")
        statuses = [status for _, status in plan(self.memory)]
        self.assertEqual((1, len(statuses) - 1), (statuses.count("review"), statuses.count("unchanged")))
        self.run_maintenance(FakeReviewer([[]]))
        self.assertEqual({"unchanged"}, {status for _, status in plan(self.memory)})
        changed = self.memory.correct(saved[10].id, "The homelab host h10 runs service number 10 and 11 for the lab.")
        self.clock.value = "2026-09-29T12:00:00Z"
        due = [group for group, status in plan(self.memory) if status == "review"]
        self.assertEqual([[changed.record.id]], [[record.id for record in group.members if record.id == changed.record.id]
                                                 for group in due])

    def test_a_combined_memory_counts_as_reviewed_so_its_group_is_not_due_again(self) -> None:
        # After the first live catch-up (2026-10-14), 22 groups were due again at once: each held a combined
        # memory, new and so never reviewed, although it was made from memories reviewed in that same call.
        first = self.save("The NAS nas01 shares media over SMB.", subjects=["NAS"])
        second = self.save("nas01 exports /volume1/media read-only.", subjects=["nas01"])
        self.save("The router is rtr01.")
        whole = "The NAS nas01 shares media over SMB, exporting /volume1/media read-only."
        self.run_maintenance(FakeReviewer([[{"kind": "combine", "keep_id": "", "ids": [first.id, second.id],
                                             "text": whole, "reason": "Same share."}]]))
        self.assertEqual("superseded", self.status(first.id))
        self.clock.value = "2026-09-28T12:00:00Z"
        self.assertEqual(["unchanged"], [status for _, status in plan(self.memory)])

    def test_the_keeper_of_a_merge_or_a_replacement_is_not_due_again(self) -> None:
        # The keepers were in the group the call reviewed, unchanged, so they were reviewed with it.
        full = self.save("The NAS is nas01 at 10.0.0.5, reached over SSH.", source="observed")
        short = self.save("The NAS is nas01 at 10.0.0.5 and is reached over SSH.")
        old = self.save("The wiki is at wiki-old.example.test.")
        self.clock.value = "2026-09-27T13:00:00Z"
        new = self.save("The wiki is at wiki.example.test.", source="observed")
        self.run_maintenance(FakeReviewer([[
            {"kind": "duplicate", "keep_id": full.id, "ids": [short.id], "reason": "Same NAS."},
            {"kind": "outdated", "keep_id": new.id, "ids": [old.id], "reason": "Moved."},
        ]]))
        self.assertEqual(("superseded", "superseded"), (self.status(short.id), self.status(old.id)))
        self.clock.value = "2026-09-28T12:00:00Z"
        self.assertEqual(["unchanged"], [status for _, status in plan(self.memory)])

    def test_a_slow_sweep_looks_at_each_group_again_after_a_month(self) -> None:
        from knowitall2.learning.maintenance import SWEEP_DAYS

        for text in ("The NAS is nas01.", "The router is rtr01."):
            self.save(text)
        self.run_maintenance(FakeReviewer([[]]))
        self.clock.value = (datetime(2026, 9, 27, 12, tzinfo=timezone.utc) + timedelta(days=SWEEP_DAYS - 1)
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(["unchanged"], [status for _, status in plan(self.memory)])
        self.clock.value = (datetime(2026, 9, 27, 12, tzinfo=timezone.utc) + timedelta(days=SWEEP_DAYS + 1)
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(["review"], [status for _, status in plan(self.memory)])

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

    def test_stats_shows_when_maintenance_last_reviewed_and_not_when_commands_were_noted(self) -> None:
        # 2026-10-11: stats showed the command-tagging pass of that morning; maintenance last reviewed on 09-28.
        from knowitall2 import journal
        from knowitall2.paths import database_path

        self.run_cli("remember", "The NAS is nas01.", "--source", "inferred")
        store = Store.open(database_path())
        try:
            journal.record(store, "learning", "run", details={"maintenance": {"calls": 2, "groups": {"review": 3}}},
                           at="2026-09-28T19:18:27Z")
            journal.record(store, "learning", "run", details={"maintenance": {"calls": 0, "groups": {"review": 9}}},
                           at="2026-10-11T05:00:00Z")
            store.record_review("f" * 64, scope_key="commands", outcome="reviewed", now="2026-10-11T04:45:09Z")
        finally:
            store.close()
        code, output = self.run_cli("stats")
        self.assertIn("last review 2026-09-28 19:18 UTC", output)

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

    def enable_learning(self) -> None:
        self.run_cli("remember", "The NAS is nas01.", "--source", "inferred")
        self.run_cli("remember", "The router is rtr01.", "--source", "inferred")
        with mock.patch("knowitall2.learning.command.find_claude_cli", return_value=Path("claude.exe")), \
                mock.patch("knowitall2.learning.command.find_codex_cli", return_value=None):
            self.run_cli("learn", "--enable")

    @staticmethod
    def calls_made(count: int, origin: str) -> None:
        from datetime import datetime, timedelta, timezone

        state = LearnerState()
        moment = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state.calls.extend({"at": moment, "session": f"s-{number}", "outcome": "ok", "origin": origin}
                           for number in range(count))
        state.save()

    def run_with_reviewer(self, *arguments: str, reviewer) -> str:
        with mock.patch("knowitall2.learning.command.find_claude_cli", return_value=Path("claude.exe")), \
                mock.patch("knowitall2.learning.command.EngineReviewer", return_value=reviewer):
            code, output = self.run_cli(*arguments)
        self.assertEqual(0, code)
        return output

    def test_a_day_of_work_learning_still_leaves_maintenance_its_calls(self) -> None:
        # 2026-10-11: learning from the user's work used up the day's 60 calls, and maintenance made none in 13 days.
        self.enable_learning()
        self.calls_made(60, "work")
        output = self.run_with_reviewer("learn", reviewer=FakeReviewer([[]]))
        self.assertIn("Review calls: 1.", output)

    def test_background_maintenance_stops_at_its_own_daily_allowance(self) -> None:
        self.enable_learning()
        self.calls_made(8, "maintenance")
        output = self.run_with_reviewer("learn", reviewer=FakeReviewer([]))
        self.assertIn("Review calls: 0; groups deferred by the budget: 1.", output)

    def test_maintain_is_not_held_back_by_the_day_and_leaves_the_background_allowance(self) -> None:
        self.enable_learning()
        self.calls_made(60, "work")
        self.calls_made(8, "maintenance")
        output = self.run_with_reviewer("maintain", reviewer=FakeReviewer([[]]))
        self.assertIn("Review calls: 1.", output)
        self.assertEqual("user", LearnerState().calls[-1].get("origin"))

    def test_maintain_takes_as_many_calls_as_the_user_allows(self) -> None:
        # Catching up on 181 waiting groups should not need 19 runs of 10 calls.
        from knowitall2.learning import command
        from knowitall2.learning.maintenance import MaintenanceReport

        self.enable_learning()
        budgets = []

        def review(**keywords):
            budgets.append(keywords["budget"])
            return MaintenanceReport()

        with mock.patch.object(command, "maintain", side_effect=review):
            self.run_with_reviewer("maintain", "--max-calls", "40", reviewer=FakeReviewer([]))
            self.run_with_reviewer("maintain", reviewer=FakeReviewer([]))
        self.assertEqual([40, 10], budgets)

    def test_a_finding_the_gates_leave_alone_is_kept_in_the_journal(self) -> None:
        from knowitall2.paths import database_path

        self.enable_learning()
        store = Store.open(database_path())
        try:
            first, second = (record.id for record in store.list_active(project_id=None, scope="global", limit=5))
        finally:
            store.close()
        finding = {"kind": "duplicate", "keep_id": first, "ids": [second], "reason": "Both are hosts."}
        output = self.run_with_reviewer("maintain", reviewer=FakeReviewer([[finding]]))
        self.assertIn("kept (not the same statement) 1", output)
        store = Store.open(database_path())
        try:
            [event] = store.events(kinds=["maintenance"])
        finally:
            store.close()
        self.assertEqual([f"duplicate [{second}] of [{first}] left alone: not the same statement "
                          "(the model said: Both are hosts.)"], event["details"]["left_alone"])

    def test_what_the_user_asks_the_catalog_for_waits_for_no_limit_and_leaves_the_allowance(self) -> None:
        from knowitall2.learning.cataloguer import FILE_PROMPT

        self.enable_learning()
        self.run_cli("remember", "The deploy script hangs unless --no-prompt is passed; always pass it.", "--kind",
                     "lesson")
        self.calls_made(60, "work")
        self.calls_made(2, "catalog")
        self.calls_made(2, "commands")
        prompts = []

        class Engine:
            def run(self, text, *, schema, system_prompt):
                prompts.append(system_prompt)
                return {"notes": []} if system_prompt == FILE_PROMPT else {"lessons": []}

        with mock.patch("knowitall2.learning.command.build_engine", return_value=Engine()):
            self.assertEqual(0, self.run_cli("catalog", "--commands", "3")[0])
            self.assertEqual(0, self.run_cli("catalog")[0])
        self.assertEqual(2, len(prompts))
        self.assertEqual(["user", "user"], [call.get("origin") for call in LearnerState().calls[64:]])

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
