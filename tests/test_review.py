import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Iterator
from unittest import mock

from _support import SOURCE_ROOT, Clock, make_repository

from knowitall2 import review, sync
from knowitall2.cli import main
from knowitall2.mcp_server import McpServer
from knowitall2.memory import Memory, MemoryInputError, keywords
from knowitall2.store import APPLYING_KEY, Store


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


@contextmanager
def stale_first(store: Store, snapshots: dict[str, object]) -> Iterator[None]:
    """``store.get`` and ``store.question`` give each snapshot once, as a read made before another writer
    committed would, and the database as it is after that."""

    real = {"get": store.get, "question": store.question}
    given: set[str] = set()

    def reader(name: str):
        def read(key: str):
            if key in snapshots and key not in given:
                given.add(key)
                return snapshots[key]
            return real[name](key)
        return read

    with mock.patch.object(store, "get", side_effect=reader("get")), \
            mock.patch.object(store, "question", side_effect=reader("question")):
        yield


class RaceTests(ReviewTestCase):
    """Two answers, or an answer and an agent's report, that read the same question before either wrote."""

    def test_a_second_answer_from_a_stale_read_changes_nothing(self) -> None:
        stated, newer, question_id = self.conflict()
        snapshots = {question_id: self.store.question(question_id), stated.id: stated, newer.id: newer}
        review.answer(self.memory, question_id, "keep_mine")
        with stale_first(self.store, snapshots), self.assertRaisesRegex(MemoryInputError, "already answered"):
            review.answer(self.memory, question_id, "use_new")
        self.assertEqual(("active", "user_stated"), (self.store.get(stated.id).status,
                                                     self.store.get(stated.id).verification))
        self.assertEqual("retired", self.store.get(newer.id).status)
        self.assertEqual("keep_mine", self.store.question(question_id)["answer"])
        self.assertFalse(self.store.close_question(question_id, answer="use_new", now=self.clock()))

    def test_an_agents_stale_report_never_changes_what_the_user_just_confirmed(self) -> None:
        older = self.save("The wiki runs on host wiki01.")
        newer = self.save("The wiki moved to host wiki02.")
        review.ask_conflict(self.memory, older, newer)
        [question] = self.store.open_questions(limit=5)
        task_id = review.start_check(self.memory, question, [older, newer], plain=None, labels=None, reason=None)
        self.memory.confirm(older.id)  # the user vouches for the older one while the agent is checking
        agent = Memory(self.store, agent="codex", clock=self.clock)
        with stale_first(self.store, {older.id: older, newer.id: newer}):
            reply = review.settle_task(agent, task_id, choice="use_new", found="saw wiki02 in DNS", certain=True)
        self.assertIn("the user decides", reply)
        self.assertEqual(("active", "user_stated"), (self.store.get(older.id).status,
                                                     self.store.get(older.id).verification))
        self.assertEqual("active", self.store.get(newer.id).status)
        self.assertEqual("open", self.store.question(question["id"])["status"])

    def test_a_report_on_an_answered_question_changes_nothing(self) -> None:
        older = self.save("The CI runner is ci01.")
        newer = self.save("The CI runner moved to ci02.")
        review.ask_conflict(self.memory, older, newer)
        [question] = self.store.open_questions(limit=5)
        review.answer(self.memory, question["id"], "keep_both")
        outcome = review.settle(self.memory, question, "use_new", by="KnowItAll2", evidence="ci02 answers")
        self.assertIn("already answered", outcome)
        self.assertEqual(["active", "active"], [self.store.get(item.id).status for item in (older, newer)])
        self.assertEqual("keep_both", self.store.question(question["id"])["answer"])


class ConcurrentWriteTests(unittest.TestCase):
    """Two processes on one database: a check and the write it allows must not let the other in between."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.alpha = make_repository(root / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.gamma = make_repository(root / "gamma", "https://gitlab.example.com/team/gamma.git")
        self.beta = make_repository(root / "beta", "https://gitlab.example.com/team/beta.git")
        self.first = Store.open(root / "knowitall2.db")
        self.addCleanup(self.first.close)
        self.second = Store.open(root / "knowitall2.db")
        self.addCleanup(self.second.close)
        self.second.connection.execute("PRAGMA busy_timeout = 100")  # the other process gives up quickly
        self.memory = Memory(self.first, agent="codex", clock=Clock())
        self.other = Memory(self.second, agent="claude-code", clock=Clock())

    def test_two_moves_of_the_same_memory_leave_one_copy(self) -> None:
        text = "Beta ships with make release."
        mine = self.memory.remember(text, scope="project", project_path=self.alpha).record
        theirs = self.memory.remember(text, scope="project", project_path=self.gamma).record
        beta = self.memory.project_for(self.beta)
        waited = []
        check = self.first.find_active_duplicate

        def the_other_moves_meanwhile(content_hash: str):
            found = check(content_hash)
            try:
                self.other.move(theirs.id, beta)
            except sqlite3.OperationalError:
                waited.append(True)  # it has to wait until this move is done
            return found

        with mock.patch.object(self.first, "find_active_duplicate", side_effect=the_other_moves_meanwhile):
            self.memory.move(mine.id, beta)
        self.assertEqual([True], waited)
        with self.assertRaisesRegex(MemoryInputError, "already has this memory"):
            self.other.move(theirs.id, beta)
        copies = [row for row in self.first.list_active(project_id=beta.id, scope="project", limit=10)]
        self.assertEqual([mine.id], [row.id for row in copies])

    def test_a_memory_retired_during_a_move_is_not_moved(self) -> None:
        saved = self.memory.remember("Beta ships with make release.", scope="project", project_path=self.alpha)
        beta = self.memory.project_for(self.beta)
        with mock.patch.object(self.first, "move_record", return_value=False), \
                self.assertRaisesRegex(MemoryInputError, "no longer active"):
            self.memory.move(saved.record.id, beta)
        self.assertEqual([], self.first.events(kinds=["move"], limit=5))

    def test_two_processes_asking_the_same_question_ask_it_once(self) -> None:
        older = self.memory.remember("The build server is build01.", detect_project=False).record
        newer = self.memory.remember("The build server is build02.", detect_project=False).record
        waited = []

        class AskingMeanwhile:
            """The first process's connection; the other one asks just before this one adds its question."""

            def __init__(self, connection) -> None:
                self.connection = connection

            def execute(self, sql, *parameters):
                if sql.startswith("INSERT INTO questions") and not waited:
                    try:
                        waited.append(not review.ask_conflict(test.other, older, newer))
                    except sqlite3.OperationalError:
                        waited.append(True)  # it has to wait until this question is added
                return self.connection.execute(sql, *parameters)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        test = self
        with mock.patch.object(self.first, "_connection", AskingMeanwhile(self.first.connection)):
            review.ask_conflict(self.memory, older, newer)
        self.assertEqual([True], waited)
        self.assertFalse(review.ask_conflict(self.other, older, newer))
        self.assertEqual(1, self.first.count_open_questions())


class StuckCheckTests(ReviewTestCase):
    def test_a_check_no_agent_did_reaches_the_user_without_a_learning_run(self) -> None:
        # Review 2026-10-04, U-M2: only a learning run moved a stale check to the user.
        older = self.save("The wiki runs on host wiki01.")
        newer = self.save("The wiki moved to host wiki02.")
        review.ask_conflict(self.memory, older, newer)
        [question] = self.store.open_questions(limit=5)
        review.start_check(self.memory, question, [older, newer], plain=None, labels=None, reason=None)
        self.assertEqual([], review.for_user(self.store, now=review._parse("2026-09-29T12:00:00Z")))
        later = "2026-10-27T12:00:00Z"
        self.assertEqual([question["id"]], [item["id"] for item in review.for_user(self.store,
                                                                                  now=review._parse(later))])
        self.clock.value = later
        self.assertIn("1 question for the user", self.memory.briefing(project_path=self.project))
        self.assertEqual([], review.in_progress(self.store, now=review._parse(later)))

    def test_a_conflict_with_the_users_own_words_goes_to_the_user(self) -> None:
        # Review 2026-10-04, U-L4: an unsure model sent it to an agent unless it leaned towards the newer one.
        from knowitall2.learning.cataloguer import _user_must_decide

        stated, newer, _ = self.conflict()
        for decision in ("use_new", "keep_mine", "keep_both", None):
            with self.subTest(decision=decision):
                self.assertTrue(_user_must_decide([stated, newer], decision))
        plain = [self.save("The wiki runs on host wiki01."), self.save("The wiki moved to host wiki02.")]
        self.assertFalse(_user_must_decide(plain, "keep_both"))


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


# Opens a new database in step with the other copies of itself: says "ready", waits for the go file of
# each round, then opens that round's database and says how it went.
_OPEN_TOGETHER = """
import sys
from pathlib import Path
from knowitall2.store import Store

folder, rounds = Path(sys.argv[1]), int(sys.argv[2])
for number in range(rounds):
    print("ready", flush=True)
    go = folder / f"go-{number}"
    while not go.exists():
        pass
    try:
        Store.open(folder / f"round-{number}.db").close()
        print("ok", flush=True)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", flush=True)
"""


class StoreSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def test_a_commit_that_fails_is_rolled_back_and_frees_the_database(self) -> None:
        path = self.folder / "knowitall2.db"
        store = Store.open(path)
        self.addCleanup(store.close)
        other = sqlite3.connect(str(path), timeout=0.5, isolation_level=None)
        self.addCleanup(other.close)
        # A memory fetched before its project: links are checked at COMMIT, and this one fails there.
        row = {"id": "r-1", "kind": "fact", "text": "The NAS is nas01.", "subjects": "[]", "tags": "[]",
               "scope": "project", "project_id": "prj-missing", "status": "active", "verification": "observed",
               "source_kind": "agent", "content_hash": "h-1", "created_at": "2026-10-01T12:00:00Z",
               "updated_at": "2026-10-01T12:00:00Z"}
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
            sync.apply_pulled(store, [{"table": "records", "key": "r-1", "op": "upsert", "row": row, "seq": 7}],
                              cursor=7)
        self.assertFalse(store.connection.in_transaction)
        self.assertIsNone(store.get_meta(APPLYING_KEY))
        self.assertIsNone(store.get_meta(sync.CURSOR_KEY))
        other.execute("BEGIN IMMEDIATE")  # another process can write again
        other.execute("ROLLBACK")
        with store.transaction():
            store.set_meta("later.write", "1")
        self.assertEqual(("1",), other.execute("SELECT value FROM meta WHERE key = 'later.write'").fetchone())

    def test_several_processes_can_create_a_new_database_together(self) -> None:
        processes, rounds = 8, 3
        environment = {**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}
        children = [subprocess.Popen([sys.executable, "-c", _OPEN_TOGETHER, str(self.folder), str(rounds)],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment)
                    for _ in range(processes)]
        results = []
        try:
            for number in range(rounds):
                for child in children:
                    self.assertEqual("ready", child.stdout.readline().strip())
                (self.folder / f"go-{number}").touch()
                results += [child.stdout.readline().strip() for child in children]
        finally:
            for number in range(rounds):
                (self.folder / f"go-{number}").touch()  # every child finishes, even after a failure here
            for child in children:
                try:
                    child.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                child.stdout.close()
        self.assertEqual(processes * rounds, len(results))
        self.assertEqual([], [result for result in results if result != "ok"])


if __name__ == "__main__":
    unittest.main()
