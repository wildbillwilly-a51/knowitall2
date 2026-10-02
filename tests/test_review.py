import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository

from knowitall2 import review
from knowitall2.cli import main
from knowitall2.mcp_server import McpServer
from knowitall2.memory import Memory, MemoryInputError, keywords
from knowitall2.store import Store


class ReviewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = make_repository(Path(self.temporary.name) / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.store = Store.in_memory()
        self.clock = Clock("2026-09-27T12:00:00Z")
        self.memory = Memory(self.store, agent="learner", clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def save(self, text: str, **options):
        return self.memory.remember(text, project_path=self.project, **options).record

    def conflict(self):
        stated = self.save("The build server is build01.", source="user")
        newer = self.save("The build server is build02.", source="observed")
        self.assertTrue(review.ask_conflict(self.memory, stated, newer))
        [question] = self.store.open_questions(limit=5)
        return stated, newer, question["id"]

    def proposed_rule(self):
        note = self.save(review.UNCONFIRMED_RULE_PREFIX + "Always tag releases.", kind="note")
        self.assertTrue(review.ask_rule(self.memory, note))
        [question] = self.store.open_questions(limit=5)
        return note, question["id"]


class AnswerTests(ReviewTestCase):
    def test_keep_mine_retires_the_newer_memory(self) -> None:
        stated, newer, question_id = self.conflict()
        self.assertIn("retired", review.answer(self.memory, question_id, "keep_mine"))
        self.assertEqual("retired", self.store.get(newer.id).status)
        self.assertEqual("active", self.store.get(stated.id).status)
        self.assertEqual(0, self.store.count_open_questions())

    def test_keep_both_keeps_each_memorys_own_verification(self) -> None:
        stated, newer, question_id = self.conflict()
        self.clock.value = "2026-09-28T09:00:00Z"
        self.assertIn("as they are", review.answer(self.memory, question_id, "keep_both"))
        for record_id, verification in ((stated.id, "user_stated"), (newer.id, "observed")):
            record = self.store.get(record_id)
            self.assertEqual(("active", verification), (record.status, record.verification))
            self.assertEqual("2026-09-28T09:00:00Z", record.confirmed_at)

    def test_declining_a_proposed_rule(self) -> None:
        note, question_id = self.proposed_rule()
        self.assertIn("as a note", review.answer(self.memory, question_id, "keep_note"))
        self.assertEqual(("active", "note"), (self.store.get(note.id).status, self.store.get(note.id).kind))
        other, other_id = self.proposed_rule_about("Always sign commits.")
        review.answer(self.memory, other_id, "forget")
        self.assertEqual("retired", self.store.get(other.id).status)

    def proposed_rule_about(self, text: str):
        note = self.save(review.UNCONFIRMED_RULE_PREFIX + text, kind="note")
        review.ask_rule(self.memory, note)
        question = [item for item in self.store.open_questions(limit=5) if note.id in item["record_ids"]][0]
        return note, question["id"]

    def test_a_question_whose_memories_changed_does_nothing(self) -> None:
        stated, newer, question_id = self.conflict()
        self.memory.forget(newer.id)
        self.assertIn("no longer applies", review.answer(self.memory, question_id, "use_new"))
        self.assertEqual("active", self.store.get(stated.id).status)
        self.assertEqual(0, self.store.count_open_questions())

    def test_bad_answers_are_input_errors(self) -> None:
        _, _, question_id = self.conflict()
        with self.assertRaisesRegex(MemoryInputError, "no question"):
            review.answer(self.memory, "q-00000000", "use_new")
        with self.assertRaisesRegex(MemoryInputError, "use one of: use_new, keep_mine, keep_both"):
            review.answer(self.memory, question_id, "make_rule")
        review.answer(self.memory, f"[{question_id}]", "keep_both")
        with self.assertRaisesRegex(MemoryInputError, "already answered"):
            review.answer(self.memory, question_id, "keep_both")

    def test_the_same_question_is_not_queued_twice(self) -> None:
        stated, newer, _ = self.conflict()
        self.assertFalse(review.ask_conflict(self.memory, stated, newer))
        self.assertEqual(1, self.store.count_open_questions())

    def test_a_failed_answer_changes_nothing(self) -> None:
        note, question_id = self.proposed_rule()
        with mock.patch.object(self.memory.store, "close_question", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                review.answer(self.memory, question_id, "make_rule")
        self.assertEqual("active", self.store.get(note.id).status)
        self.assertEqual(0, self.store.stats()["by_kind"].get("rule", 0))
        self.assertEqual(1, self.store.count_open_questions())


class NoticeTests(ReviewTestCase):
    def test_the_briefing_mentions_questions_at_most_every_few_days(self) -> None:
        self.conflict()
        self.assertIn("1 question for the user", self.memory.briefing(project_path=self.project))
        listed = review.list_questions(self.memory)
        self.assertIn("use_new: The newer one is right", listed)
        self.assertNotIn("question for the user", self.memory.briefing(project_path=self.project))
        self.clock.value = "2026-09-30T12:00:00Z"
        self.assertIn("1 question for the user", self.memory.briefing(project_path=self.project))

    def test_no_questions(self) -> None:
        self.assertEqual("KnowItAll2 has no questions for the user.", review.list_questions(self.memory))
        self.assertNotIn("question", self.memory.briefing(project_path=self.project))


class InterfaceTests(ReviewTestCase):
    def test_agents_list_and_answer_questions_through_mcp(self) -> None:
        stated, newer, question_id = self.conflict()
        server = McpServer(memory_factory=lambda agent: self.memory, cwd=self.project)

        def call(name: str, **arguments) -> dict:
            return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})["result"]

        listed = call("questions")["content"][0]["text"]
        self.assertIn(f"[{question_id}] You said", listed)
        self.assertIn("record each choice with the answer tool", listed)
        wrong = call("answer", id=question_id, choice="maybe")
        self.assertTrue(wrong["isError"])
        answered = call("answer", id=question_id, choice="use_new")
        self.assertFalse(answered["isError"])
        self.assertEqual("superseded", self.store.get(stated.id).status)

    def test_the_user_answers_from_the_command_line(self) -> None:
        home = Path(self.temporary.name) / "home"
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(home)}):
            store = Store.open(home / "knowitall2.db")
            try:
                memory = Memory(store, agent="learner")
                note = memory.remember(review.UNCONFIRMED_RULE_PREFIX + "Always tag releases.", kind="note").record
                review.ask_rule(memory, note)
            finally:
                store.close()
            code, output = self.run_cli("questions")
            self.assertEqual(0, code)
            self.assertIn("Answer with: knowitall2 answer <id> <choice>", output)
            question_id = re.search(r"\[(q-[0-9a-f]+)\]", output).group(1)
            code, output = self.run_cli("answer", question_id, "make_rule")
            self.assertEqual(0, code)
            self.assertIn("Saved your rule", output)
            code, output = self.run_cli("stats")
            self.assertIn("Open questions for you: 0", output)

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = main(list(arguments))
        return code, output.getvalue()


class HelperTests(unittest.TestCase):
    def test_a_nested_transaction_joins_the_outer_one(self) -> None:
        store = Store.in_memory()
        memory = Memory(store, clock=Clock())
        try:
            with self.assertRaises(RuntimeError), store.transaction():
                memory.remember("The NAS is nas01.")
                raise RuntimeError("abort")
            self.assertEqual(0, store.stats()["active"])
        finally:
            store.close()

    def test_keywords_prefer_frequent_meaningful_words(self) -> None:
        text = "router router router dnsmasq dnsmasq the the the 10.0.0.1 abc a1b2c3d4 about"
        self.assertEqual(["router", "dnsmasq"], keywords(text, limit=5))
        self.assertEqual(["router"], keywords(text, limit=1))


if __name__ == "__main__":
    unittest.main()
