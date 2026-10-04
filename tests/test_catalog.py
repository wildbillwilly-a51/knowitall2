import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository

from knowitall2 import catalog, review
from knowitall2.learning import catchup, cataloguer
from knowitall2.learning.extractor import ExtractionError
from knowitall2.learning.state import LearnerSettings, LearnerState, save_settings
from knowitall2.mcp_server import McpServer
from knowitall2.memory import Memory
from knowitall2.store import Store


class FakeEngine:
    """Answers each catalog step from scripted replies, keyed by which prompt it was given."""

    def __init__(self, *, during=None, **replies) -> None:
        self.replies = {name: list(values) for name, values in replies.items()}
        self.inputs: list[tuple[str, str]] = []
        self.last_usage = {"input_tokens": 100, "output_tokens": 10}
        self.during = during or {}  # what changes in the store while the model works on a step

    def run(self, text, *, schema, system_prompt):
        step = {cataloguer.FILE_PROMPT: "file", cataloguer.PROFILE_PROMPT: "profile",
                cataloguer.REVIEW_PROMPT: "review"}[system_prompt]
        self.inputs.append((step, text))
        if step in self.during:
            self.during.pop(step)()
        reply = self.replies.get(step, []).pop(0) if self.replies.get(step) else {step_key(step): []}
        if isinstance(reply, Exception):
            raise reply
        return reply


def step_key(step: str) -> str:
    return {"file": "notes", "profile": "systems", "review": "questions"}[step]


def note(record, headline, *, system_id="", name="", area="Other", kind="other", aliases=(), facet="about"):
    return {"id": record.id, "headline": headline, "system_id": system_id, "system_name": name, "system_area": area,
            "system_kind": kind, "system_aliases": list(aliases), "facet": facet}


class CatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "home")})
        self.environment.start()
        self.project = make_repository(self.root / "homelab", "https://gitlab.example.com/me/homelab.git")
        self.store = Store.in_memory()
        self.clock = Clock("2026-09-28T12:00:00Z")
        self.memory = Memory(self.store, agent="codex", clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.environment.stop()
        self.temporary.cleanup()

    def save(self, text, **options):
        return self.memory.remember(text, project_path=self.project, **options).record

    def run_catalog(self, engine, *, budget=10):
        return cataloguer.run_catalog(memory=Memory(self.store, agent="catalog", clock=self.clock), engine=engine,
                                      budget=budget)


class FilingTests(CatalogTestCase):
    def test_memories_are_filed_under_systems_with_plain_headlines(self) -> None:
        address = self.save("vCenter vc01 is at 10.9.15.16 and manages two ESXi hosts.", source="observed")
        route = self.save("Use govc through jump01 for read-only vCenter inventory.")
        vault = self.save("Vaultwarden at https://vault.example.test holds homelab sign-ins.", source="user")
        stray = self.save("The office printer is on the second floor.")
        engine = FakeEngine(file=[{"notes": [
            note(address, "vCenter's address and the hosts it manages", name="vCenter", area="Servers and virtual machines",
                 kind="service", aliases=["vc01"], facet="where"),
            note(route, "Agents look at vCenter through another server", name="VMware vCenter", kind="service",
                 aliases=["vc01"], facet="access"),
            note(vault, "Vaultwarden keeps the homelab sign-ins", name="Vaultwarden", area="Accounts and sign-in",
                 kind="service", facet="signin"),
        ]}])
        report = self.run_catalog(engine)
        self.assertEqual((4, 2), (report.filed + 1, report.new_systems))
        systems = {system["name"]: system for system in self.store.systems_list()}
        self.assertEqual({"vCenter", "Vaultwarden"}, set(systems))
        self.assertIn("VMware vCenter", systems["vCenter"]["aliases"] + ["VMware vCenter"])
        notes = self.store.notes_for([address.id, route.id, stray.id])
        self.assertEqual(notes[address.id]["system_id"], notes[route.id]["system_id"])
        self.assertEqual((None, "other"), (notes[stray.id]["system_id"], notes[stray.id]["facet"]))
        self.assertEqual(0, self.store.count_unnoted())
        self.assertIn("[" + systems["vCenter"]["id"] + "] vCenter", engine.inputs[-1][1] if engine.inputs[-1][0] == "file"
                      else engine.inputs[1][1])

    def test_systems_get_a_summary_and_gaps_once_until_their_memories_change(self) -> None:
        address = self.save("vCenter vc01 is at 10.9.15.16.", source="observed")
        system_id = catalog.system_id_for("vCenter")
        engine = FakeEngine(
            file=[{"notes": [note(address, "Where vCenter is", name="vCenter", kind="service", facet="where")]}],
            profile=[{"systems": [{"id": system_id, "summary": "Runs your homelab virtual machines.",
                                   "gaps": ["Where the vCenter sign-in is kept"], "aliases": ["vc01"]}]}],
        )
        report = self.run_catalog(engine)
        self.assertEqual((1, 2), (report.profiled, report.calls))
        system = self.store.system(system_id)
        self.assertEqual(("Runs your homelab virtual machines.", ["Where the vCenter sign-in is kept"], ["vc01"]),
                         (system["summary"], system["gaps"], system["aliases"]))
        self.assertEqual(0, self.run_catalog(FakeEngine()).calls)

    def test_what_changed_while_the_model_filed_memories_is_kept(self) -> None:
        told = self.save("vCenter vc01 is at 10.9.15.16.", source="observed")
        forgotten = self.save("The old NAS is nas00 in the basement.")
        plain = self.save("The office printer is on the second floor.")
        system_id = catalog.system_id_for("vCenter")

        def meanwhile():
            # The user files one memory and forgets another while the model works.
            self.store.upsert_system(system_id=system_id, name="vCenter", area="Servers and virtual machines",
                                     kind="service", aliases=[], now=self.clock())
            self.store.set_note(told.id, headline="Where the user says vCenter is", system_id=system_id,
                                facet="where", written_by="user", now=self.clock())
            self.memory.forget(forgotten.id, by_user=True)

        engine = FakeEngine(file=[{"notes": [
            note(told, "Something else", facet="other"),
            note(forgotten, "The old NAS", name="NAS", kind="device", area="Storage and backups", facet="where"),
        ]}], during={"file": meanwhile})
        report = self.run_catalog(engine)
        notes = self.store.notes_for([told.id, forgotten.id, plain.id])
        self.assertEqual(("user", "where", system_id),
                         (notes[told.id]["written_by"], notes[told.id]["facet"], notes[told.id]["system_id"]))
        self.assertNotIn(forgotten.id, notes)
        self.assertEqual("other", notes[plain.id]["facet"])  # skipped by the model: filed as general
        self.assertEqual(["vCenter"], [system["name"] for system in self.store.systems_list()])
        self.assertEqual((0, 0), (report.filed, report.new_systems))

    def test_the_budget_limits_calls_and_a_backend_problem_stops_the_pass(self) -> None:
        for number in range(30):
            self.save(f"Host server{number:02d} runs service number {number}.")
        report = self.run_catalog(FakeEngine(file=[{"notes": []}, {"notes": []}]), budget=1)
        self.assertEqual((1, 1, 25), (report.calls, report.deferred, 30 - self.store.count_unnoted()))
        blocked = self.run_catalog(FakeEngine(file=[ExtractionError("Not logged in", blocking=True)]))
        self.assertIn("Not logged in", blocked.blocked)

    def test_calls_count_toward_the_daily_budget(self) -> None:
        self.save("vCenter vc01 is at 10.9.15.16.")
        state = LearnerState(self.root / "state.json")
        cataloguer.run_catalog(memory=self.memory, engine=FakeEngine(file=[{"notes": []}]), budget=5, state=state)
        self.assertEqual(1, LearnerState(self.root / "state.json").calls_since(datetime(2026, 1, 1, tzinfo=timezone.utc)))


class ProfileTests(CatalogTestCase):
    def file(self, record, facet, system="vCenter", kind="service"):
        system_id = catalog.system_id_for(system)
        self.store.upsert_system(system_id=system_id, name=system, area="Servers and virtual machines", kind=kind,
                                 aliases=[], now=self.clock())
        self.store.set_note(record.id, headline=record.text[:40], system_id=system_id, facet=facet,
                            written_by="catalog", now=self.clock())
        return self.store.system(system_id)

    def test_a_profile_shows_what_is_known_missing_and_last_seen_working(self) -> None:
        self.file(self.save("vCenter is at 10.9.15.16.", source="observed"), "where")
        system = self.file(self.save("Agents use govc through jump01.", source="observed"), "access")
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        shown = catalog.profile(self.store, system, now=now)
        self.assertEqual(["Where the sign-in is kept"], [item["label"] for item in shown["missing"]])
        self.assertEqual(("partial", "2026-09-28T12:00:00Z"), (shown["status"]["key"], shown["last_seen_working"]))
        self.file(self.save("The vCenter sign-in is in Vaultwarden item vcenter-admin.", source="user"), "signin")
        self.assertEqual("ready", catalog.profile(self.store, system, now=now)["status"]["key"])
        stale = datetime(2027, 6, 1, tzinfo=timezone.utc)
        self.assertEqual("partial", catalog.profile(self.store, system, now=stale)["status"]["key"])
        listed = catalog.overview(self.store, now=now)
        [area] = listed["areas"]
        self.assertEqual(("Servers and virtual machines", "vCenter", 3), (area["area"], area["systems"][0]["name"],
                                                                         area["systems"][0]["memories"]))

    def test_a_profile_changed_while_the_model_skipped_its_system_is_kept(self) -> None:
        system = self.file(self.save("vCenter is at 10.9.15.16.", source="observed"), "where")
        self.store.set_system_profile(system["id"], summary="Runs the lab's virtual machines.",
                                      gaps=["Its license key", "How to renew its certificate"], profiled="old",
                                      now=self.clock())

        def meanwhile():
            # The finder fills a gap while the model describes the system.
            self.store.set_system_gaps(system["id"], gaps=["Its license key"], now=self.clock())

        self.run_catalog(FakeEngine(profile=[{"systems": []}], during={"profile": meanwhile}))
        described = self.store.system(system["id"])
        self.assertEqual(("Runs the lab's virtual machines.", ["Its license key"]),
                         (described["summary"], described["gaps"]))
        # Marked as described all the same, so it is not sent again until its memories change.
        self.assertEqual(catalog.profile_fingerprint(self.store.system_records(system["id"])), described["profiled"])
        self.assertEqual(0, self.run_catalog(FakeEngine()).calls)

    def test_projects_have_no_readiness_and_the_user_can_fill_a_gap(self) -> None:
        system = self.file(self.save("Releases are cut from main.", kind="decision"), "decision", system="Homelab",
                           kind="project")
        self.assertEqual("Project", catalog.profile(self.store, system)["status"]["label"])
        vcenter = self.file(self.save("vCenter is at 10.9.15.16."), "where")
        result = catalog.tell(self.memory, vcenter, "signin", "The vCenter sign-in is in Vaultwarden item vcenter-admin.")
        self.assertEqual(("user_stated", ["vCenter"]), (result.record.verification, list(result.record.subjects)))
        self.assertEqual("signin", self.store.notes_for([result.record.id])[result.record.id]["facet"])


class QuestionFlowTests(CatalogTestCase):
    def setUp(self) -> None:
        super().setUp()
        save_settings(LearnerSettings(enabled=True))
        self.now = datetime.fromisoformat(self.clock().replace("Z", "+00:00"))

    def conflict(self, *, older_source="observed"):
        older = self.save("The Cisco access points reject the controller certificate at depth 0.", source=older_source)
        newer = self.save("The Cisco access points were tested with a two-certificate chain without logs.")
        review.ask_conflict(self.memory, older, newer)
        [question] = self.store.open_questions(limit=5)
        return older, newer, question

    def decide(self, question, decision, *, certain, reason="The newer note records a later test.", plain=None):
        return {"questions": [{
            "id": question["id"], "decision": decision, "certain": certain, "reason": reason,
            "plain_question": plain or "Two notes about your Cisco access points disagree. Which is right?",
            "labels": [{"key": "use_new", "label": "The later test"}],
        }]}

    def test_a_clear_conflict_is_settled_without_anyone(self) -> None:
        older, newer, question = self.conflict()
        self.assertEqual([], review.for_user(self.store, now=self.now))
        report = self.run_catalog(FakeEngine(review=[self.decide(question, "use_new", certain=True)]))
        self.assertEqual({"settled": 1}, dict(report.questions))
        self.assertEqual("superseded", self.store.get(older.id).status)
        [event] = self.store.events(kinds=["settle"])
        self.assertEqual(("KnowItAll2", "use_new"), (event["agent"], event["outcome"]))

    def test_an_unclear_conflict_goes_to_the_next_agent_and_then_is_settled_by_it(self) -> None:
        older, newer, question = self.conflict()
        self.run_catalog(FakeEngine(review=[self.decide(question, "unsure", certain=False)]))
        self.assertEqual("checking", review.stage(self.store, question["id"]))
        [task] = self.store.tasks(kind="check")
        briefing = self.memory.briefing(project_path=self.project)
        self.assertIn(f"[{task['id']}] Two memories disagree", briefing)
        self.assertIn("optional", briefing)
        server = McpServer(memory_factory=lambda agent: self.memory, cwd=self.project)
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "settle", "arguments": {
            "task": task["id"], "choice": "keep_mine", "what_you_saw": "syslog shows Verify Cert FAILED at 0 depth",
            "certain": True}}})["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(("active", "retired"), (self.store.get(older.id).status, self.store.get(newer.id).status))
        self.assertEqual(0, self.store.count_open_questions())
        self.assertEqual("done", self.store.task(task["id"])["status"])

    def test_an_agent_that_cannot_tell_hands_it_to_the_user_in_plain_words(self) -> None:
        older, newer, question = self.conflict()
        self.run_catalog(FakeEngine(review=[self.decide(question, "unsure", certain=False)]))
        [task] = self.store.tasks(kind="check")
        review.settle_task(Memory(self.store, agent="claude-code", clock=self.clock), task["id"], choice="keep_both",
                           found="The logs from that test are gone.", certain=False)
        [waiting] = review.for_user(self.store, now=self.now)
        self.assertEqual("Two notes about your Cisco access points disagree. Which is right?", waiting["plain"])
        self.assertEqual("The later test", waiting["labels"][0]["label"])
        self.assertEqual("keep_both", waiting["not_sure"])
        self.assertIn("claude-code could not tell: The logs from that test are gone.", waiting["findings"])
        self.assertIn("1 question for the user", self.memory.briefing(project_path=self.project))

    def test_the_users_own_words_and_rules_only_go_to_the_user(self) -> None:
        older, newer, question = self.conflict(older_source="user")
        self.run_catalog(FakeEngine(review=[self.decide(question, "use_new", certain=True)]))
        self.assertEqual("ask_user", review.stage(self.store, question["id"]))
        self.assertEqual("active", self.store.get(older.id).status)
        note = self.save(review.UNCONFIRMED_RULE_PREFIX + "Never downgrade the camera controller mid-run.", kind="note")
        review.ask_rule(self.memory, note)
        rule = [item for item in self.store.open_questions(limit=5) if item["kind"] == "confirm_rule"][0]
        self.run_catalog(FakeEngine(review=[self.decide(rule, "use_new", certain=True,
                                                        plain="Should agents never downgrade the camera controller?")]))
        waiting = {item["id"]: item for item in review.for_user(self.store, now=self.now)}
        self.assertEqual("Should agents never downgrade the camera controller?", waiting[rule["id"]]["plain"])

    def test_checks_nobody_can_do_reach_the_user_and_old_unreviewed_questions_too(self) -> None:
        older, newer, question = self.conflict()
        self.run_catalog(FakeEngine(review=[self.decide(question, "unsure", certain=False)]))
        for _ in range(review.CHECK_OFFERS):
            self.memory.briefing(project_path=self.project)
        self.run_catalog(FakeEngine())
        self.assertEqual("ask_user", review.stage(self.store, question["id"]))
        self.assertIn("none could tell", " ".join(review.for_user(self.store, now=self.now)[0]["findings"]))
        stale = self.save("Backups run nightly at 02:00.")
        other = self.save("Backups run nightly at 04:00.")
        review.ask_conflict(self.memory, stale, other)
        later = datetime(2026, 10, 5, tzinfo=timezone.utc)
        self.assertEqual(2, len(review.for_user(self.store, now=later)))

    def test_without_learning_questions_go_straight_to_the_user(self) -> None:
        save_settings(LearnerSettings(enabled=False))
        _, _, question = self.conflict()
        self.assertEqual("ask_user", review.stage(self.store, question["id"]))


class FindOutTests(CatalogTestCase):
    def test_an_agent_is_asked_to_find_something_out_while_looking_at_that_system(self) -> None:
        address = self.save("vCenter vc01 is at 10.9.15.16.", source="observed")
        system_id = catalog.system_id_for("vCenter")
        self.store.upsert_system(system_id=system_id, name="vCenter", area="Servers and virtual machines",
                                 kind="service", aliases=[], now=self.clock())
        self.store.set_note(address.id, headline="Where vCenter is", system_id=system_id, facet="where",
                            written_by="catalog", now=self.clock())
        task_id = review.ask_to_find_out(Memory(self.store, agent="app", clock=self.clock), self.store.system(system_id),
                                         "signin", "Where the sign-in is kept")
        found = self.memory.recall("vcenter", project_path=self.project)
        self.assertIn(f"[{task_id}] KnowItAll2 does not know where the sign-in is kept for vCenter", found)
        answer = review.settle_task(self.memory, task_id, choice="found", certain=True,
                                    found="The vCenter sign-in is in Vaultwarden item vcenter-admin.")
        self.assertIn("Saved", answer)
        shown = catalog.profile(self.store, self.store.system(system_id))
        self.assertEqual([], shown["missing"][:0] + [item for item in shown["missing"] if item["facet"] == "signin"])
        self.assertEqual("done", self.store.task(task_id)["status"])

    def test_a_note_the_user_wrote_stays_when_an_agent_finds_the_same_thing(self) -> None:
        system_id = catalog.system_id_for("vCenter")
        self.store.upsert_system(system_id=system_id, name="vCenter", area="Servers and virtual machines",
                                 kind="service", aliases=[], now=self.clock())
        system = self.store.system(system_id)
        told = catalog.tell(self.memory, system, "other", "The vCenter sign-in is in Vaultwarden item vcenter-admin.")
        task_id = review.ask_to_find_out(Memory(self.store, agent="app", clock=self.clock), system, "signin",
                                         "Where the sign-in is kept")
        answer = review.settle_task(self.memory, task_id, choice="found", certain=True,
                                    found="The vCenter sign-in is in Vaultwarden item vcenter-admin.")
        self.assertIn(f"Saved [{told.record.id}]", answer)
        note = self.store.notes_for([told.record.id])[told.record.id]
        self.assertEqual(("other", "user"), (note["facet"], note["written_by"]))
        self.assertEqual("done", self.store.task(task_id)["status"])


class CatchUpTests(unittest.TestCase):
    def test_skipped_sessions_of_one_folder_are_marked_to_be_learned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex" / "sessions" / "2026" / "09" / "01"
            codex.mkdir(parents=True)
            logs = {}
            for name, folder in (("vcenter", root / "Homelab" / "projects" / "vcenter-lab"),
                                 ("homelab", root / "Homelab"), ("other", root / "Elsewhere")):
                session_id = f"01a0e45f-3ee3-7951-b9e3-6691dd0f0a6{len(logs)}"
                log = codex / f"rollout-2026-09-01T10-00-00-{session_id}.jsonl"
                meta = {"timestamp": "2026-09-01T10:00:00Z", "type": "session_meta",
                        "payload": {"id": session_id, "cwd": str(folder), "source": "vscode"}}
                log.write_text(json.dumps(meta) + "\n", encoding="utf-8")
                old = time.time() - 3600
                os.utime(log, (old, old))
                logs[name] = log
            environment = {"KNOWITALL2_HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"),
                           "CLAUDE_CONFIG_DIR": str(root / "claude")}
            with mock.patch.dict(os.environ, environment):
                state = LearnerState()
                for log in logs.values():
                    state.entry(log).update({"offset": log.stat().st_size, "status": "baseline"})
                state.save()
                self.assertEqual(3, sum(group["sessions"] for group in catchup.groups()))
                self.assertEqual(2, catchup.estimate(str(root / "Homelab"))["sessions"])
                self.assertEqual(0, catchup.catch_up(str(root / "Homelab"), since="2099-01-01"))
                self.assertEqual(2, catchup.catch_up(str(root / "Homelab")))
                statuses = {name: LearnerState().entry(log)["status"] for name, log in logs.items()}
        self.assertEqual({"vcenter": "new", "homelab": "new", "other": "baseline"}, statuses)

    def test_a_set_aside_session_stays_set_aside_when_a_little_is_added_and_is_repaired_if_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex" / "sessions" / "2026" / "09" / "01"
            codex.mkdir(parents=True)
            logs = []
            for number in range(2):
                session_id = f"01a0e45f-3ee3-7951-b9e3-6691dd0f0a7{number}"
                log = codex / f"rollout-2026-09-01T10-00-0{number}-{session_id}.jsonl"
                meta = {"timestamp": "2026-09-01T10:00:00Z", "type": "session_meta",
                        "payload": {"id": session_id, "cwd": str(root / "Homelab"), "source": "vscode"}}
                log.write_text(json.dumps(meta) + "\n", encoding="utf-8")
                logs.append((log, session_id))
            environment = {"KNOWITALL2_HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"),
                           "CLAUDE_CONFIG_DIR": str(root / "claude")}
            with mock.patch.dict(os.environ, environment):
                state = LearnerState()
                (first, _), (second, lost_id) = logs
                size = first.stat().st_size
                state.entry(first).update({"offset": size, "status": "baseline", "baseline_to": size})
                # As an older version left it: marked done by a run that learned nothing from it.
                state.entry(second).update({"offset": second.stat().st_size, "status": "done", "session_id": lost_id})
                state.entry(first).update({"status": "done", "session_id": "later-part-learned"})
                state.save()
                self.assertEqual(1, sum(group["sessions"] for group in catchup.groups()))
                store = Store.in_memory()
                try:
                    self.assertEqual(1, catchup.repair_once(store))
                    self.assertEqual(0, catchup.repair_once(store))  # once per installation
                finally:
                    store.close()
                self.assertEqual(2, sum(group["sessions"] for group in catchup.groups()))
                # Sessions that need no model call cost nothing, so a cap of 0 still takes them.
                self.assertEqual(2, catchup.catch_up(str(root / "Homelab"), max_calls=0))


if __name__ == "__main__":
    unittest.main()
