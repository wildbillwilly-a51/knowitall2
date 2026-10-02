import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository

from knowitall2.cli import main
from knowitall2.identity import identify, identify_remote
from knowitall2.importer import import_file
from knowitall2.memory import Memory
from knowitall2.store import Store

REMOTE = "https://gitlab.example.com/team/alpha.git"


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = make_repository(self.root / "alpha", REMOTE)
        self.store = Store.in_memory()
        self.memory = Memory(self.store, agent="import:test", clock=Clock("2026-09-28T12:00:00Z"))

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def write(self, *items) -> Path:
        path = self.root / "memories.jsonl"
        lines = [item if isinstance(item, str) else json.dumps(item) for item in items]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_imports_kinds_projects_and_original_dates(self) -> None:
        path = self.write(
            {"text": "Frigate is at https://frigate.example.test.", "subjects": ["Frigate"],
             "created_at": "2026-08-14T10:00:00Z"},
            {"text": "Always keep router backups in /srv/backups.", "kind": "rule", "source": "user"},
            {"text": "Releases are cut from main.", "kind": "decision", "scope": "project",
             "project": {"path": str(self.project), "remote": "https://old.example.com/alpha.git"}},
            {"text": "The beta service deploys with Helm.", "kind": "procedure", "scope": "project",
             "project": {"remote": "https://gitlab.example.com/team/beta.git", "name": "Beta"}},
        )
        report = import_file(self.memory, path, dry_run=False)
        self.assertEqual({"imported": 4}, dict(report.outcomes))
        found = self.memory.recall("frigate", project_path=self.project)
        self.assertIn("unverified via import:test, 2026-08-14", found)
        self.assertIn("Releases are cut from main.", self.memory.briefing(project_path=self.project))
        beta = make_repository(self.root / "beta-copy", "git@gitlab.example.com:team/beta.git")
        self.assertEqual(identify_remote("https://gitlab.example.com/team/beta.git").id, identify(beta).id)
        self.assertIn("The beta service deploys with Helm.", self.memory.briefing(project_path=beta))
        self.assertIn("Always keep router backups", self.memory.briefing(project_path=beta))

    def test_bad_lines_are_rejected_one_by_one(self) -> None:
        path = self.write(
            "not json",
            {"text": "The admin password is hunter22 for now."},
            {"text": "Always reboot on Fridays.", "kind": "rule"},
            {"text": "A fact from the future.", "created_at": "2999-01-01T00:00:00Z"},
            {"text": "A project fact.", "scope": "project", "project": {"path": str(self.root / "missing")}},
            {"text": "The NAS is nas01."},
        )
        report = import_file(self.memory, path, dry_run=False)
        self.assertEqual({"imported": 1, "rejected": 5}, dict(report.outcomes))
        described = report.describe(dry_run=False, verbose=False)
        for reason in ("not a JSON object", "contains a secret", "rules must come from the user's own words",
                       "is in the future", "its project was not found"):
            self.assertIn(reason, described)

    def test_importing_twice_confirms_instead_of_copying(self) -> None:
        path = self.write({"text": "The NAS is nas01."}, {"text": "The router is rtr01."})
        import_file(self.memory, path, dry_run=False)
        report = import_file(self.memory, path, dry_run=False)
        self.assertEqual({"already known": 2}, dict(report.outcomes))
        self.assertEqual(2, self.store.stats()["active"])

    def test_conflicts_become_questions(self) -> None:
        path = self.write(
            {"text": "Backups run at 02:00.", "origin": "a", "conflicts_with": ["b"], "created_at": "2026-08-01T00:00:00Z"},
            {"text": "Backups run at 04:00.", "origin": "b", "conflicts_with": ["a"], "created_at": "2026-08-02T00:00:00Z"},
        )
        report = import_file(self.memory, path, dry_run=False)
        self.assertEqual(1, report.questions)
        [question] = self.store.open_questions(limit=5)
        first, second = (self.store.get(record_id).text for record_id in question["record_ids"])
        self.assertEqual(("Backups run at 02:00.", "Backups run at 04:00."), (first, second))

    def test_a_dry_run_reports_everything_and_saves_nothing(self) -> None:
        path = self.write(
            {"text": "Backups run at 02:00.", "origin": "a", "conflicts_with": ["b"]},
            {"text": "Backups run at 04:00.", "origin": "b"},
            {"text": "Releases are cut from main.", "kind": "decision", "scope": "project",
             "project": {"path": str(self.project)}},
        )
        report = import_file(self.memory, path, dry_run=True)
        self.assertEqual(({"imported": 3}, 1), (dict(report.outcomes), report.questions))
        self.assertIn("- line 1: imported: Backups run at 02:00.", report.describe(dry_run=True, verbose=True))
        stats = self.store.stats()
        self.assertEqual((0, 0, 0), (stats["active"], stats["projects"], self.store.count_open_questions()))


class ImportCommandTests(unittest.TestCase):
    def test_the_command_labels_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "memories.jsonl"
            path.write_text(json.dumps({"text": "The NAS is nas01.", "subjects": ["NAS"]}) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(root / "home")}):
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(io.StringIO()):
                    self.assertEqual(0, main(["import", str(path), "--dry-run"]))
                    self.assertEqual(0, main(["import", str(path), "--label", "oldtool"]))
                    self.assertEqual(0, main(["recall", "nas"]))
        text = output.getvalue()
        self.assertIn("Would import: imported 1", text)
        self.assertIn("Imported: imported 1", text)
        self.assertIn("via import:oldtool", text)


if __name__ == "__main__":
    unittest.main()
