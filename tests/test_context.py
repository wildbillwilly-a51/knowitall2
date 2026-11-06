import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository
from knowitall2 import context
from knowitall2.memory import Memory
from knowitall2.store import Store


class MessageContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.alpha = make_repository(self.root / "alpha", "https://git.example/alpha.git")
        self.beta = make_repository(self.root / "beta", "https://git.example/beta.git")
        self.store = Store.in_memory()
        self.memory = Memory(self.store, agent="codex", clock=Clock())
        self.store.upsert_system(system_id="sys-brave", name="Brave", area="Workstation",
                                 kind="application", aliases=[], now=self.memory.now())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def save(self, body, **kwargs):
        kwargs.setdefault("source", "observed")
        return self.memory.remember(body, project_path=self.alpha, **kwargs).record

    def get(self, prompt="Use the Brave browser extension controls", seen=()):
        return context.for_message(self.memory, prompt, project_path=str(self.alpha), seen=list(seen))

    def test_details_arrive_without_agent_recall_or_a_knowledge_folder(self):
        row = self.save("The Brave browser extension is already connected. Select the existing browser inventory.",
                        source="observed")
        unrelated = self.save("A browser extension plugin can be installed for another application.")
        text, seen = self.get()
        self.assertIn(row.id, text)
        self.assertIn("already connected", text)
        self.assertIn("observed via codex", text)
        self.assertNotIn(unrelated.id, text)
        self.assertEqual(1, len(seen))
        self.assertFalse((self.alpha / "knowitall2-known").exists())
        [event] = self.store.events(kinds=["context"], limit=5)
        self.assertEqual("offered", event["outcome"])
        self.assertEqual(0, self.store._connection.execute(
            "SELECT COUNT(*) FROM usage WHERE operation NOT IN ('briefing', 'recall')").fetchone()[0])

    def test_full_excerpt_is_deduplicated_but_changed_content_is_delivered(self):
        self.save("The Brave browser extension connects to the existing inventory.")
        text, seen = self.get()
        self.assertTrue(text)
        self.assertEqual(("", []), self.get(seen=seen))
        self.save("The Brave browser extension connects through a new inventory endpoint.")
        self.assertIn("new inventory endpoint", self.get(seen=seen)[0])

    def test_no_context_for_generic_or_ambient_messages(self):
        self.save("The Brave browser extension is connected.")
        for prompt in ("continue", "ok do it", "<environment_context>Use Brave browser extension</environment_context>"):
            with self.subTest(prompt=prompt):
                self.assertEqual(("", []), self.get(prompt))

    def test_exact_error_phrase_beats_incidental_policy_words(self):
        row = self.save("A blocked by policy denial can come from the Windows command classifier.", source="observed")
        other = self.save("The Authelia policy keeps blocked domains in the active list.")
        text, _ = self.get("why does blocked by policy keep coming up?")
        self.assertIn(row.id, text)
        self.assertNotIn(other.id, text)

    def test_scope_and_retired_records_are_respected(self):
        foreign = self.memory.remember("The Brave browser extension uses a private foreign project route.",
                                       project_path=self.beta, scope="project", source="observed").record
        retired = self.save("The Brave browser extension has a retired connection.")
        self.memory.forget(retired.id)
        good = self.save("The Brave browser extension has an active shared connection.")
        text, _ = self.get()
        self.assertIn(good.id, text)
        self.assertNotIn(foreign.id, text)
        self.assertNotIn(retired.id, text)

    def test_status_notes_do_not_displace_durable_procedures(self):
        row = self.save("The Brave browser extension has a transient browser control status.")
        self.store.set_note(row.id, headline="Brave status", system_id="sys-brave", facet="status",
                            written_by="catalog", now=self.memory.now())
        self.assertEqual(("", []), self.get())

    def test_context_is_bounded_without_cutting_caveats(self):
        for i in range(8):
            self.save(f"Brave browser extension setup procedure {i}. " + "Keep this caveat. " * 50)
        text, seen = self.get()
        self.assertLessEqual(len(seen), context.MAX_MEMORIES)
        self.assertLessEqual(len(text), context.MAX_CHARACTERS)
        self.assertIn("observed", text)
        self.assertTrue(text.endswith("Keep this caveat."))

    def test_unchecked_learning_is_not_automatically_promoted(self):
        row = self.save("The Brave browser extension needs a new unverified installation.", source="inferred")
        self.assertEqual(("", []), self.get())
        self.assertIn(row.id, self.memory.recall("Brave extension", project_path=self.alpha))
        for tag in ("provisional", "disputed"):
            checked = self.save(f"The Brave browser extension has {tag} routing evidence.")
            self.store.set_tags(checked.id, [tag], now=self.memory.now())
        self.assertEqual(("", []), self.get())

    def test_pasted_documents_do_not_cause_unbounded_frequency_queries(self):
        with mock.patch.object(self.store, "count_matching", side_effect=AssertionError("scanned the whole store")), \
                mock.patch.object(self.store, "context_candidates", wraps=self.store.context_candidates) as search:
            self.get("Brave browser extension " + " ".join(f"topic{i}" for i in range(2000)))
        self.assertEqual(1, search.call_count)
        self.assertLessEqual(search.call_args.args[0].count(" OR ") + 1, context.MAX_TERMS)

    def test_hook_can_reuse_project_identity_without_another_git_process(self):
        self.save("The Brave browser extension uses the connected inventory.")
        project = self.memory.project_for(self.alpha)
        with mock.patch.object(self.memory, "project_for", side_effect=AssertionError("rediscovered project")):
            text, _ = context.for_message(self.memory, "Brave browser extension controls", project=project, seen=[])
        self.assertIn("connected inventory", text)

    def test_ineligible_candidates_cannot_hide_established_evidence(self):
        # A bounded FTS pool must be bounded *after* eligibility filtering.
        # Otherwise short unchecked records can occupy every candidate slot.
        for i in range(context.MAX_CANDIDATES + 5):
            self.save(f"Brave browser extension controls variant {i}.", source="inferred")
        good = self.save("Brave browser extension controls use an existing connection. " +
                         "Verify its current identity before navigation. " * 20)
        self.assertIn(good.id, self.get()[0])

    def test_explicit_related_project_is_available_but_other_projects_are_not(self):
        wanted = self.memory.remember("Beta browser extension routing uses its established inventory.",
                                      project_path=self.beta, scope="project", source="observed").record
        self.assertIn(wanted.id, self.get("Check beta browser extension routing")[0])
        self.assertNotIn(wanted.id, self.get("Check Brave browser extension routing")[0])

    def test_a_known_worked_in_project_is_not_lost_to_the_original_cwd(self):
        wanted = self.memory.remember("The browser extension routing uses its established inventory.",
                                      project_path=self.beta, scope="project", source="observed").record
        beta = self.memory.project_for(self.beta)
        text, _ = context.for_message(self.memory, "Check browser extension routing inventory",
                                      project_path=str(self.alpha), related_projects=[beta], seen=[])
        self.assertIn(wanted.id, text)

    def test_historical_selection_does_not_use_later_evidence(self):
        old = self.save("Brave browser extension controls use the existing inventory.")
        self.memory._clock.value = "2026-09-28T08:00:00Z"
        later = self.save("Brave browser extension controls changed to a newer route.")
        text, _ = context.for_message(self.memory, "Brave browser extension controls",
                                      project_path=str(self.alpha), seen=[], as_of="2026-09-27T23:59:59Z")
        self.assertIn(old.id, text)
        self.assertNotIn(later.id, text)

    def test_known_url_host_is_context_but_a_name_in_an_unrelated_url_path_is_not(self):
        self.store.upsert_system(system_id="sys-files", name="FileService", area="Workstation",
                                 kind="service", aliases=["files.example.test"], now=self.memory.now())
        row = self.save("FileService browser connection diagnostics use the existing inventory.")
        self.assertIn(row.id, self.get("Check browser connection at https://files.example.test")[0])
        self.assertNotIn(row.id, self.get("Check browser connection at https://elsewhere.test/files.example.test")[0])
