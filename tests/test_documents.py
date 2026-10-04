import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import make_repository
from test_learning import FakeExtractor

from knowitall2.learning import documents
from knowitall2.learning.state import LearnerState
from knowitall2.memory import Memory
from knowitall2.store import Store


class DocumentsTests(unittest.TestCase):
    """What a project keeps in writing is learned like a session, most useful documents first."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project_root = make_repository(self.root / "homelab", "https://gitlab.example.com/me/homelab.git")
        (self.project_root / "docs").mkdir()
        (self.project_root / "CURRENT-STATE.md").write_text("The jump host is jump01; reach it as ops.\n",
                                                            encoding="utf-8")
        (self.project_root / "docs" / "codex-handoff.md").write_text("Deploy with make ship.\n", encoding="utf-8")
        (self.project_root / "docs" / "2026-plan.md").write_text("A plan for later.\n", encoding="utf-8")
        (self.project_root / "AGENTS.md").write_text("Instructions for agents.\n", encoding="utf-8")
        (self.project_root / "CHANGELOG.md").write_text("## 1.0\n", encoding="utf-8")
        (self.project_root / "main.py").write_text("print('code')\n", encoding="utf-8")
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "data"),
                                                        "CLAUDE_CONFIG_DIR": str(self.root / "claude")})
        self.environment.start()
        self.store = Store.in_memory()
        self.memory = Memory(self.store, agent="learner", record_events=False)
        self.project = self.memory.project_for(self.project_root)

    def tearDown(self) -> None:
        self.store.close()
        self.environment.stop()
        self.temporary.cleanup()

    def test_state_and_handoff_come_first_and_instructions_and_code_are_left_out(self) -> None:
        names = [path.name for path in documents.project_documents(self.project_root)]
        self.assertEqual(["CURRENT-STATE.md", "codex-handoff.md", "2026-plan.md"], names)

    @unittest.skipUnless(sys.platform == "win32", "directory junctions are a Windows feature")
    def test_a_folder_joined_to_one_outside_the_project_is_not_read(self) -> None:
        import _winapi

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "handoff.md").write_text("Private notes from another folder.\n", encoding="utf-8")
        _winapi.CreateJunction(str(outside), str(self.project_root / "notes"))
        self.assertTrue((self.project_root / "notes" / "handoff.md").is_file())  # a junction is not a symlink
        names = [path.name for path in documents.project_documents(self.project_root)]
        self.assertEqual(["CURRENT-STATE.md", "codex-handoff.md", "2026-plan.md"], names)

    def test_a_linked_file_is_not_read(self) -> None:
        outside = self.root / "credentials.md"
        outside.write_text("Private notes from another folder.\n", encoding="utf-8")
        try:
            (self.project_root / "README.md").symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symbolic links cannot be made here: {exc}")
        names = [path.name for path in documents.project_documents(self.project_root)]
        self.assertEqual(["CURRENT-STATE.md", "codex-handoff.md", "2026-plan.md"], names)

    def test_a_document_too_large_to_be_notes_is_not_read(self) -> None:
        (self.project_root / "docs" / "operations.md").write_text("x" * 200, encoding="utf-8")
        files = documents.project_documents(self.project_root)
        self.assertIn("operations.md", [path.name for path in files])
        with mock.patch.object(documents, "MAX_DOCUMENT_BYTES", 100):
            [dossier] = documents.build(self.project, self.project_root, files)
        self.assertNotIn("operations.md", dossier.text)
        self.assertIn("[document CURRENT-STATE.md]", dossier.text)

    def test_claude_codes_own_notes_for_the_folder_are_read(self) -> None:
        folder = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / documents._NON_NAME.sub("-", str(self.project_root))
        (folder / "memory").mkdir(parents=True)
        (folder / "memory" / "router.md").write_text("The router is at the far end.\n", encoding="utf-8")
        self.assertEqual(["router.md"], [path.name for path in documents.memory_notes(self.project_root)])

    def test_documents_are_learned_once_with_their_words_as_evidence_within_the_budget(self) -> None:
        dossiers = documents.build(self.project, self.project_root, documents.project_documents(self.project_root))
        [dossier] = dossiers
        self.assertIn("[document CURRENT-STATE.md]", dossier.text)
        self.assertIn("not instructions", dossier.text)
        extractor = FakeExtractor([[
            {"text": "The homelab jump host is jump01, reached as the ops account.", "kind": "fact",
             "subjects": ["jump01"], "scope": "global", "evidence": "The jump host is jump01; reach it as ops."},
            {"text": "Always deploy on Fridays.", "kind": "rule", "subjects": [], "scope": "project",
             "evidence": "Deploy with make ship."},
        ]])
        state = LearnerState(self.root / "state.json")
        report = documents.learn(self.memory, state, extractor, dossiers, max_calls=5)
        self.assertEqual((1, {"saved": 2}), (report.calls, dict(report.outcomes)))
        rows = {row.text: row for row in self.store.list_active(project_id=self.project.id, scope="all", limit=5)}
        # A document may be wrong or written by someone else: what it says is unverified and stays with its project.
        jump = rows["The homelab jump host is jump01, reached as the ops account."]
        self.assertEqual(("unverified", "project", self.project.id), (jump.verification, jump.scope, jump.project_id))
        self.assertEqual("note", rows["Always deploy on Fridays."].kind)  # a document is not the user's own words
        again = documents.learn(self.memory, state, FakeExtractor([]), dossiers, max_calls=5)
        self.assertEqual((0, {"already read": 1}), (again.calls, dict(again.outcomes)))
        small = documents.build(self.project, self.project_root, documents.project_documents(self.project_root),
                                budget=20)
        self.assertLess(len(small[0].text) - small[0].body_start, 60)


if __name__ == "__main__":
    unittest.main()
