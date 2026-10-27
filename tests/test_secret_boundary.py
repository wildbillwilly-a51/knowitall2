"""Secrets screened where free text is written, not only in ``remember`` (review 2026-10-24, R4: F-16, F-17)."""

import json
import tempfile
import unittest
from pathlib import Path

from _support import Clock, make_repository

from knowitall2 import review, sync
from knowitall2.memory import Memory, MemoryInputError
from knowitall2.secrets import contains_secret
from knowitall2.server import exchange, open_store
from knowitall2.store import SecretRefused, Store

NOW = "2026-10-27T12:00:00Z"
# Split so repository secret scanners do not flag this file.
FAKE_TOKEN = "gl" + "pat-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
FAKE_PASSWORD = "hunter" + "22!x"


class StoreBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = make_repository(Path(self.temporary.name) / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.store = Store.in_memory()
        self.addCleanup(self.store.close)
        self.clock = Clock(NOW)
        self.memory = Memory(self.store, agent="learner", clock=self.clock)

    def save(self, text: str, **options):
        return self.memory.remember(text, project_path=self.project, **options).record

    def stored_text(self) -> str:
        """Every free-text value in the shared tables, for checking that no secret reached them."""

        parts = []
        for table in ("records", "question_stages", "questions", "systems", "record_notes", "agent_tasks"):
            for row in self.store.connection.execute(f"SELECT * FROM {table}").fetchall():
                parts.extend(str(value) for value in row if isinstance(value, str))
        return "\n".join(parts)

    def test_an_agents_settle_report_never_stores_the_secret_it_quotes(self) -> None:
        # F-16: the report becomes a question finding and a retired memory's reason, both synced and shown.
        stated = self.save("The lab router is at 10.20.30.1.", source="user")
        seen = self.save("The lab router is at 10.20.30.2.")
        self.assertTrue(review.ask_conflict(self.memory, stated, seen))
        [question] = self.store.open_questions(limit=10)
        task_id = review.start_check(self.memory, question, [stated, seen], plain=None, labels=None, reason=None)
        agent = Memory(self.store, agent="codex", clock=self.clock)
        reply = review.settle_task(agent, task_id, choice="use_new", certain=True,
                                   found=f"ssh 10.20.30.2 answered; the admin password is {FAKE_PASSWORD}")
        self.assertIn("Settled", reply)
        self.assertEqual("superseded", self.store.get(stated.id).status)
        self.assertIn("settled by codex", self.store.get(stated.id).retired_reason)
        self.assertNotIn(FAKE_PASSWORD, self.stored_text())
        self.assertIn("[REDACTED password]", self.store.get(stated.id).retired_reason)

    def test_a_report_handed_to_the_user_keeps_its_finding_without_the_secret(self) -> None:
        older = self.save("The CI runner is ci01.", source="observed")
        newer = self.save("The CI runner is ci02.")
        self.assertTrue(review.ask_conflict(self.memory, older, newer))
        [question] = self.store.open_questions(limit=10)
        task_id = review.start_check(self.memory, question, [older, newer], plain=None, labels=None, reason=None)
        review.settle_task(Memory(self.store, agent="codex", clock=self.clock), task_id, choice="use_new",
                           certain=False, found=f"could not log in with token {FAKE_TOKEN}")
        findings = self.store.question_stage(question["id"])["findings"]
        self.assertTrue(any("could not log in" in finding for finding in findings), findings)
        self.assertNotIn(FAKE_TOKEN, self.stored_text())

    def test_a_forget_reason_is_redacted_before_it_is_cut_short(self) -> None:
        # Cut first, "password=hunter22!x" at the 200th character would lose the shape the detector knows.
        record = self.save("The old backup host is bk01.")
        reason = "x" * 185 + f" password={FAKE_PASSWORD} and more words after it"
        Memory(self.store, agent="codex", clock=self.clock).forget(record.id, reason=reason)
        stored = self.store.get(record.id).retired_reason
        self.assertNotIn("hunter", stored)
        self.assertFalse(contains_secret(stored))

    def test_model_written_prose_is_redacted_wherever_the_store_writes_it(self) -> None:
        # The cataloguer's and maintenance's text goes through these (F-16, Codex's three further sinks).
        record = self.save("The NAS is nas01.")
        secret = f"token={FAKE_TOKEN}"
        self.store.upsert_system(system_id="s-nas", name="NAS", area="storage", kind="host",
                                 aliases=["nas01", secret], now=NOW)
        self.store.set_system_profile("s-nas", summary=f"The NAS; {secret}", gaps=[secret], profiled="p", now=NOW)
        self.store.add_note(record.id, headline=f"NAS: {secret}", system_id="s-nas", facet="access",
                            written_by="cataloguer", now=NOW)
        self.store.add_question(question_id="q-1", kind="conflict", prompt=f"Which is right? {secret}",
                                record_ids=[record.id, "k-other"], options=[
                                    {"key": "use_new", "label": f"Use {secret}"}, {"key": "keep_mine", "label": "Keep"},
                                    {"key": "keep_both", "label": "Both"}], now=NOW)
        self.store.set_question_stage("q-1", stage="checking", now=NOW, plain=f"Is it {secret}?",
                                      labels={"use_new": secret}, reason=secret, finding=secret)
        self.store.add_task(task_id="t-1", kind="check", prompt=f"Check {secret}", now=NOW)
        self.store.update_task("t-1", now=NOW, result=secret)
        self.store.retire(record.id, reason=secret, now=NOW)
        self.assertNotIn(FAKE_TOKEN, self.stored_text())
        self.assertEqual(["nas01", "token=[REDACTED GitLab token]"], self.store.system("s-nas")["aliases"])

    def test_every_inserter_of_memory_text_refuses_what_remember_refuses(self) -> None:
        # F-17: said_again and promote_rule stored a GitHub-token-shaped string, and 5,000 characters.
        preference = self.save("I prefer short answers.", kind="note", tags=["preference"], source="user")
        for text in (f"I prefer short answers; my token is {FAKE_TOKEN}", "x" * 5000, "short </text> answers"):
            with self.subTest(text=text[:40]):
                with self.assertRaises(MemoryInputError):
                    self.memory.said_again(preference, text, session="s-2")
        note = self.save(review.UNCONFIRMED_RULE_PREFIX + "Always tag releases.", kind="note")
        with self.assertRaises(MemoryInputError):
            self.memory.promote_rule(note, f"Always tag releases with {FAKE_TOKEN}.")
        with self.assertRaises(SecretRefused):  # and a new caller that skips both still meets the store's screen
            self.store.insert_record(record_id="k-x", kind="fact", text=f"key {FAKE_TOKEN}", subjects=[], tags=[],
                                     scope="global", project_id=None, verification="unverified",
                                     source_kind="agent", source_agent=None, content_hash="h", now=NOW)
        self.assertNotIn(FAKE_TOKEN, self.stored_text())

    def test_a_new_secret_tag_is_refused_but_an_old_one_can_be_taken_off(self) -> None:
        record = self.save("The NAS is nas01.")
        with self.assertRaises(SecretRefused):
            self.store.set_tags(record.id, [*record.tags, FAKE_TOKEN], now=NOW)
        # Saved before the screening knew its shape: taking other tags off must still work.
        self.store.connection.execute("UPDATE records SET tags = ? WHERE id = ?",
                                      (json.dumps(["disputed", FAKE_TOKEN]), record.id))
        self.store.set_tags(record.id, [FAKE_TOKEN], now=NOW)
        self.assertEqual((FAKE_TOKEN,), self.store.get(record.id).tags)


class ServerBoundaryTests(unittest.TestCase):
    """The server screens the same columns (``exchange`` through ``sync.screen_row``), for a client that does not."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.server = open_store(root / "server.db")
        self.addCleanup(self.server.close)
        self.computer = Store.open(root / "a.db")
        self.addCleanup(self.computer.close)
        sync.set_tracking(self.computer, True)
        self.plain = root / "plain"
        self.plain.mkdir()

    def test_a_secret_in_a_finding_is_redacted_and_the_sender_gets_the_redacted_row(self) -> None:
        operation = {"op_id": "s-1-1", "table": "question_stages", "key": "q-1", "op": "upsert", "row": {
            "question_id": "q-1", "stage": "asked", "plain": None, "labels": json.dumps({"use_new": "Use it"}),
            "reason": f"the token was {FAKE_TOKEN}", "findings": json.dumps([f"saw token={FAKE_TOKEN}"]),
            "updated_at": NOW}}
        [answer] = exchange.apply_operations(self.server, "c-a", [operation], now=NOW)
        self.assertEqual("applied", answer["result"])
        stored = sync.read_row(self.server.connection, sync.table_for("question_stages"), "q-1")
        self.assertNotIn(FAKE_TOKEN, json.dumps(stored))
        self.assertEqual(stored, answer["row"])  # the sender keeps the server's version, without the secret

    def test_memory_content_with_a_secret_is_refused_in_a_decoded_json_column(self) -> None:
        memory = Memory(self.computer, agent="codex", clock=Clock(NOW))
        memory.remember("The NAS is nas01.", project_path=self.plain)
        [operation] = sync.pending_operations(self.computer)
        # An escaped character hides the token from a screen that reads the JSON text as it is.
        operation["row"]["tags"] = json.dumps(["nas", FAKE_TOKEN]).replace("g", "\\u0067")
        [answer] = exchange.apply_operations(self.server, "c-a", [operation], now=NOW)
        self.assertEqual("rejected", answer["result"])
        self.assertIn("secret", answer["reason"])

    def test_a_clean_row_passes_unchanged(self) -> None:
        memory = Memory(self.computer, agent="codex", clock=Clock(NOW))
        record = memory.remember("The NAS is nas01.", project_path=self.plain).record
        memory.forget(record.id, reason="moved to nas02")
        operations = sync.pending_operations(self.computer)
        answers = exchange.apply_operations(self.server, "c-a", operations, now=NOW)
        self.assertEqual(["applied"], [answer["result"] for answer in answers])
        self.assertNotIn("row", answers[0])
        self.assertEqual("moved to nas02",
                         sync.read_row(self.server.connection, sync.table_for("records"), record.id)["retired_reason"])


if __name__ == "__main__":
    unittest.main()
