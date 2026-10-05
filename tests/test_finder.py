import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository

from knowitall2 import catalog
from knowitall2.learning import finder
from knowitall2.learning.extractor import ClaudeCliExtractor, CodexCliExtractor, ExtractionError
from knowitall2.learning.state import LearnerState
from knowitall2.memory import Memory
from knowitall2.store import Store

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class FakeEngine:
    def __init__(self, payload, *, during=None) -> None:
        self.payload = payload
        self.calls = []
        self.during = during  # what changes in the store while the agent looks

    def explore(self, text, *, folder, schema, system_prompt):
        self.calls.append((text, folder))
        if self.during is not None:
            self.during()
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FinderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "home")})
        self.environment.start()
        self.project = make_repository(self.root / "homelab", "https://gitlab.example.com/me/homelab.git")
        (self.project / "inventory.yml").write_text("vcenter:\n  host: vcsa01.lab.local\n  port: 443\n", encoding="utf-8")
        (self.project / "README.md").write_text(
            "# Lab\n\nThe vCenter admin sign-in is kept in Vaultwarden, in the item vcenter-admin.\n", encoding="utf-8")
        self.store = Store.in_memory()
        self.clock = Clock("2026-09-29T12:00:00Z")
        self.memory = Memory(self.store, agent="codex", clock=self.clock)
        record = self.memory.remember("vCenter manages the lab's two ESXi hosts.", scope="project",
                                      project_path=self.project, subjects=["vCenter"]).record
        self.system_id = catalog.system_id_for("vCenter")
        self.store.upsert_system(system_id=self.system_id, name="vCenter", area="Servers and virtual machines",
                                 kind="service", aliases=[], now=self.clock())
        self.store.set_note(record.id, headline="vCenter manages the lab's ESXi hosts", system_id=self.system_id,
                            facet="about", written_by="catalog", now=self.clock())

    def tearDown(self) -> None:
        self.store.close()
        self.environment.stop()
        self.temporary.cleanup()

    def profile(self) -> dict:
        return catalog.profile(self.store, self.store.system(self.system_id))


class SearchTests(FinderTestCase):
    def test_saves_only_what_a_file_shows_and_leaves_the_rest_to_agents(self) -> None:
        engine = FakeEngine({
            "found": [
                {"part": "p1", "text": "vCenter runs at vcsa01.lab.local on port 443.", "file": "inventory.yml",
                 "quote": "host: vcsa01.lab.local"},
                {"part": "P3", "text": "The vCenter admin sign-in is kept in Vaultwarden, item vcenter-admin.",
                 "file": "README.md", "quote": "kept in Vaultwarden, in the item vcenter-admin"},
                {"part": "p2", "text": "Agents reach vCenter with govc from jump01.", "file": "README.md",
                 "quote": "agents use govc from jump01"},
            ],
            "not_found": [],
        })
        result = finder.run(self.system_id, engine=engine, store=self.store)
        [(text, folder)] = engine.calls
        self.assertEqual(self.project.resolve(), Path(folder).resolve())
        self.assertIn("Find these missing parts", text)
        self.assertIn("- p3: Where the sign-in is kept", text)
        self.assertIn("vCenter manages the lab's ESXi hosts", text)
        self.assertEqual(["Where it is", "Where the sign-in is kept"], [item["label"] for item in result["found"]])
        self.assertEqual("inventory.yml", result["found"][0]["file"])
        self.assertEqual([("How agents reach it", "the quote is not in that file")],
                         [(item["label"], item["reason"]) for item in result["turned_down"]])
        self.assertEqual(["How agents reach it"], result["not_found"])
        # What was found fills the profile, unverified: a file says so, which may be wrong or out of date.
        profile = self.profile()
        self.assertEqual(["access"], [item["facet"] for item in profile["missing"]])
        where = next(item for item in profile["facets"] if item["facet"] == "where")["memories"][0]
        self.assertEqual("unverified", where["verification"])
        # What was not found waits for an agent that works with vCenter.
        [task] = self.store.tasks(status="open", kind="find_out", system_id=self.system_id)
        self.assertEqual("access", task["facet"])
        # The call counts toward the daily total, and the page can show the result.
        state = LearnerState(self.root / "home" / "learner" / "state.json")
        self.assertEqual(1, state.calls_since(datetime.now(timezone.utc) - timedelta(minutes=5)))
        self.assertEqual("done", finder.status(self.system_id)["status"])

    def test_a_filled_gap_leaves_the_profile_and_closes_its_request(self) -> None:
        self.store.set_system_gaps(self.system_id, gaps=["How to renew the vCenter certificate", "Its license key"],
                                   now=self.clock())
        from knowitall2 import review

        review.ask_to_find_out(self.memory, self.store.system(self.system_id), "other",
                               "How to renew the vCenter certificate")
        (self.project / "RUNBOOK.md").write_text("Renew the vCenter certificate with certificate-manager.\n",
                                                 encoding="utf-8")
        engine = FakeEngine({"found": [{"part": "p4", "text": "The vCenter certificate is renewed with "
                                        "certificate-manager.", "file": "RUNBOOK.md",
                                        "quote": "Renew the vCenter certificate with certificate-manager."}],
                             "not_found": []})
        result = finder.run(self.system_id, engine=engine, store=self.store)
        self.assertEqual(["How to renew the vCenter certificate"], [item["label"] for item in result["found"]])
        self.assertEqual(["Its license key"], self.store.system(self.system_id)["gaps"])
        asked = [task["prompt"] for task in self.store.tasks(status="open", kind="find_out", system_id=self.system_id)]
        self.assertFalse(any("renew the vcenter certificate" in prompt.lower() for prompt in asked))
        howto = next(item for item in self.profile()["facets"] if item["facet"] == "howto")
        self.assertEqual(1, len(howto["memories"]))

    def test_a_turned_down_answer_keeps_nothing_of_a_secret(self) -> None:
        engine = FakeEngine({"found": [
            {"part": "p9", "text": f"vCenter automation signs in with {TOKEN}.", "file": "README.md", "quote": TOKEN},
            {"part": "p1", "text": "vCenter runs at vcsa01.lab.local.", "file": "inventory.yml",
             "quote": f"host: vcsa01.lab.local {TOKEN}"},
        ], "not_found": []})
        result = finder.run(self.system_id, engine=engine, store=self.store)
        self.assertEqual(["secret", "secret"], [item["reason"] for item in result["turned_down"]])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn(TOKEN, finder.status_path(self.system_id).read_text(encoding="utf-8"))

    def test_a_profile_written_while_the_agent_looks_is_kept(self) -> None:
        self.store.set_system_gaps(self.system_id, gaps=["How to renew the vCenter certificate", "Its license key"],
                                   now=self.clock())
        (self.project / "RUNBOOK.md").write_text("Renew the vCenter certificate with certificate-manager.\n",
                                                 encoding="utf-8")

        def meanwhile():
            # The catalog describes vCenter again while the agent looks.
            self.store.set_system_profile(
                self.system_id, summary="Runs the lab's virtual machines.",
                gaps=["How to renew the vCenter certificate", "Its license key", "Its backup schedule"],
                profiled="new", now=self.clock())

        engine = FakeEngine({"found": [{"part": "p4", "text": "The vCenter certificate is renewed with "
                                        "certificate-manager.", "file": "RUNBOOK.md",
                                        "quote": "Renew the vCenter certificate with certificate-manager."}],
                             "not_found": []}, during=meanwhile)
        result = finder.run(self.system_id, engine=engine, store=self.store)
        self.assertEqual(["How to renew the vCenter certificate"], [item["label"] for item in result["found"]])
        system = self.store.system(self.system_id)
        self.assertEqual(("Runs the lab's virtual machines.", ["Its license key", "Its backup schedule"]),
                         (system["summary"], system["gaps"]))

    def test_a_note_the_user_wrote_is_kept(self) -> None:
        system = self.store.system(self.system_id)
        # The user filed this fact under another part of the profile, before the search and while the agent looks.
        before = catalog.tell(self.memory, system, "about", "vCenter runs at vcsa01.lab.local on port 443.").record

        def meanwhile():
            catalog.tell(self.memory, system, "other",
                         "The vCenter admin sign-in is kept in Vaultwarden, item vcenter-admin.")

        engine = FakeEngine({"found": [
            {"part": "p1", "text": "vCenter runs at vcsa01.lab.local on port 443.", "file": "inventory.yml",
             "quote": "host: vcsa01.lab.local"},
            {"part": "p3", "text": "The vCenter admin sign-in is kept in Vaultwarden, item vcenter-admin.",
             "file": "README.md", "quote": "kept in Vaultwarden, in the item vcenter-admin"},
        ], "not_found": []}, during=meanwhile)
        result = finder.run(self.system_id, engine=engine, store=self.store)
        self.assertEqual(before.id, result["found"][0]["id"])
        notes = self.store.notes_for([item["id"] for item in result["found"]])
        self.assertEqual([("about", "user", "vCenter runs at vcsa01.lab.local on port 443."),
                          ("other", "user", "The vCenter admin sign-in is kept in Vaultwarden, item vcenter-admin.")],
                         [(notes[item["id"]]["facet"], notes[item["id"]]["written_by"], notes[item["id"]]["headline"])
                          for item in result["found"]])

    def test_a_second_search_does_not_ask_agents_twice(self) -> None:
        engine = FakeEngine({"found": [], "not_found": ["where", "access", "signin"]})
        finder.run(self.system_id, engine=engine, store=self.store)
        finder.run(self.system_id, engine=engine, store=self.store)
        self.assertEqual(3, len(self.store.tasks(status="open", kind="find_out", system_id=self.system_id)))

    def test_an_engine_problem_is_reported_and_asks_no_one(self) -> None:
        result = finder.run(self.system_id, engine=FakeEngine(ExtractionError("rate limit")), store=self.store)
        self.assertEqual("failed", result["status"])
        self.assertIn("rate limit", result["message"])
        self.assertEqual([], self.store.tasks(status="open", kind="find_out", system_id=self.system_id))

    def test_without_a_project_folder_agents_are_asked_instead(self) -> None:
        other = catalog.system_id_for("NAS")
        record = self.memory.remember("The NAS stores the lab backups.", scope="global", subjects=["NAS"],
                                      detect_project=False).record
        self.store.upsert_system(system_id=other, name="NAS", area="Storage and backups", kind="device",
                                 aliases=[], now=self.clock())
        self.store.set_note(record.id, headline="The NAS stores backups", system_id=other, facet="about",
                            written_by="catalog", now=self.clock())
        engine = FakeEngine({"found": [], "not_found": []})
        result = finder.run(other, engine=engine, store=self.store)
        self.assertEqual([], engine.calls)
        self.assertIn("No project folder for NAS", result["message"])
        self.assertEqual(3, result["asked"])


class CheckTests(FinderTestCase):
    def check(self, **item):
        answer = {"part": "p1", "text": "vCenter runs at vcsa01.lab.local on port 443.", "file": "inventory.yml",
                  "quote": "host: vcsa01.lab.local", **item}
        parts = {"p1": {"facet": "where", "label": "Where it is"}, "p2": {"facet": "signin", "label": "Sign-in"}}
        return finder.check(answer, folder=self.project, parts=parts)

    def test_rejects_what_cannot_be_shown(self) -> None:
        self.assertIsNotNone(self.check()[0])
        outside = self.root / "secret-notes.txt"
        outside.write_text("host: vcsa01.lab.local", encoding="utf-8")
        for item, reason in (
            ({"file": "../secret-notes.txt"}, "the file is outside the project folder"),
            ({"file": str(outside)}, "the file is outside the project folder"),
            ({"file": "missing.yml"}, "the file was not found"),
            ({"quote": "host: vcsa99.lab.local"}, "the quote is not in that file"),
            ({"text": "vCenter is backed up nightly to the NAS share."}, "the quote does not say that"),
            ({"part": "where"}, "not one of the missing parts"),
            ({"text": "short"}, "length"),
            ({"text": "The vCenter password is hunter22 for the admin account."}, "secret"),
            # The secret check comes first, so an answer wrong in other ways too is still turned down as a secret.
            ({"part": "where", "text": f"vCenter automation signs in with {TOKEN}."}, "secret"),
            ({"text": "token", "quote": TOKEN}, "secret"),
        ):
            with self.subTest(item=item):
                self.assertEqual((None, reason), self.check(**item))


class StartTests(FinderTestCase):
    def test_starts_one_detached_search_at_a_time(self) -> None:
        launches = []
        result = finder.start(self.system_id, launcher=lambda command, **options: launches.append((command, options)))
        self.assertTrue(result["started"])
        [(command, options)] = launches
        self.assertEqual([sys.executable, "-B", "-P", "-m", "knowitall2", "find-out", self.system_id], command)
        self.assertEqual("looking", finder.status(self.system_id)["status"])
        self.assertFalse(finder.start(self.system_id, launcher=lambda *a, **k: launches.append(a))["started"])
        self.assertEqual(1, len(launches))
        later = datetime.now(timezone.utc) + timedelta(minutes=finder.STALE_MINUTES + 1)
        self.assertEqual("failed", finder.status(self.system_id, now=later)["status"])


class EngineCommandTests(unittest.TestCase):
    def test_claude_reads_only_inside_the_folder_where_it_can_be_confined(self) -> None:
        # Review 2026-10-04, L-M4: Read, Grep, and Glob read anywhere unless Claude Code is in restricted mode.
        from knowitall2.learning import extractor

        for offered in (True, False):
            with self.subTest(offered=offered), mock.patch.object(extractor, "offers_restricted_mode",
                                                                  return_value=offered):
                command = ClaudeCliExtractor(Path("claude.exe")).explore_command(schema=finder.FIND_SCHEMA,
                                                                                 system_prompt="x")
                self.assertEqual(offered, "--restricted" in command)

    def test_whether_claude_offers_restricted_mode_comes_from_its_help(self) -> None:
        from knowitall2.learning import extractor

        for help_text, offered in ((b"  --restricted   Restricted mode", True), (b"  --safe-mode", False)):
            with self.subTest(offered=offered), mock.patch.dict(extractor._RESTRICTED_SUPPORT, clear=True), \
                    mock.patch.object(extractor, "run_bounded",
                                      return_value=subprocess.CompletedProcess([], 0, help_text, b"")):
                self.assertEqual(offered, extractor.offers_restricted_mode(Path("claude.exe")))

    def test_claude_gets_only_the_tools_that_read_files(self) -> None:
        from knowitall2.learning import extractor

        with mock.patch.object(extractor, "offers_restricted_mode", return_value=True):
            command = ClaudeCliExtractor(Path("claude.exe")).explore_command(schema=finder.FIND_SCHEMA,
                                                                             system_prompt="x")
        self.assertEqual("Read,Grep,Glob", command[command.index("--tools") + 1])
        self.assertEqual("Read,Grep,Glob", command[command.index("--allowedTools") + 1])
        self.assertEqual("dontAsk", command[command.index("--permission-mode") + 1])
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-session-persistence", command)

    def test_codex_looks_in_the_folder_in_its_read_only_sandbox(self) -> None:
        seen = {}

        def runner(command, **options):
            seen.update(command=command, cwd=options["cwd"])
            answer = Path(command[command.index("-o") + 1])
            answer.write_text(json.dumps({"found": [], "not_found": []}), encoding="utf-8")
            return mock.Mock(returncode=0, stdout=b"", stderr=b"tokens used 1,234")

        with tempfile.TemporaryDirectory() as folder:
            engine = CodexCliExtractor(Path("codex.exe"), model="light", runner=runner)
            self.assertEqual({"found": [], "not_found": []},
                             engine.explore("x", folder=Path(folder), schema=finder.FIND_SCHEMA, system_prompt="y"))
            command = seen["command"]
            self.assertEqual(folder, command[command.index("-C") + 1])
            self.assertEqual(folder, seen["cwd"])
            self.assertEqual("read-only", command[command.index("--sandbox") + 1])
            self.assertIn("looking facts up in the files", " ".join(command))


if __name__ == "__main__":
    unittest.main()
