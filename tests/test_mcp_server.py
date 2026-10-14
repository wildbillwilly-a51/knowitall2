import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from _support import Clock, make_repository

from knowitall2.mcp_server import LATEST_PROTOCOL_VERSION, McpServer
from knowitall2.memory import Memory
from knowitall2.store import Store, StoreError


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = make_repository(Path(self.temporary.name) / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.store = Store.in_memory()
        self.server = McpServer(
            memory_factory=lambda agent: Memory(self.store, agent=agent, clock=Clock()), cwd=self.project,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def request(self, method: str, params: dict | None = None, message_id: int = 1) -> dict:
        return self.server.handle({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}})

    def call(self, name: str, **arguments) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def initialize(self, client: str = "codex-mcp-client") -> dict:
        return self.request(
            "initialize",
            {"protocolVersion": LATEST_PROTOCOL_VERSION, "clientInfo": {"name": client}, "capabilities": {}},
        )["result"]

    def test_initialize_negotiates_the_version_and_gives_instructions(self) -> None:
        result = self.request("initialize", {"protocolVersion": "2025-03-26", "clientInfo": {"name": "claude-code"}})
        self.assertEqual("2025-03-26", result["result"]["protocolVersion"])
        self.assertEqual("knowitall2", result["result"]["serverInfo"]["name"])
        self.assertIn("call `briefing`", result["result"]["instructions"])
        newer = self.request("initialize", {"protocolVersion": "2099-01-01"})["result"]
        self.assertEqual(LATEST_PROTOCOL_VERSION, newer["protocolVersion"])

    def test_lists_the_tools_with_schemas(self) -> None:
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual(
            {"briefing", "recall", "remember", "forget", "questions", "answer", "settle", "learn"},
            {tool["name"] for tool in tools},
        )
        self.assertTrue(all(tool["inputSchema"]["type"] == "object" for tool in tools))

    def test_round_trip_records_which_agent_saved_it(self) -> None:
        self.initialize("codex-mcp-client")
        saved = self.call("remember", text="The NAS is nas01.", subjects=["NAS"])
        self.assertFalse(saved["isError"])
        record_id = re.search(r"\[(k-[0-9a-f]+)\]", saved["content"][0]["text"]).group(1)
        found = self.call("recall", query="nas", limit="5")["content"][0]["text"]
        self.assertIn(record_id, found)
        self.assertIn("via codex", found)
        self.assertIn("Forgot", self.call("forget", id=record_id)["content"][0]["text"])

    def test_agents_ask_the_user_before_changing_the_users_statements(self) -> None:
        self.initialize("codex-mcp-client")
        user = Memory(self.store, agent="app", clock=Clock())
        fridays = user.remember("Never deploy to production on Fridays.", kind="rule", source="user",
                                detect_project=False).record
        backups = user.remember("Keep a backup before running database migrations.", kind="rule", source="user",
                                detect_project=False).record
        replaced = self.call("remember", text="Deploying on Fridays is fine.", replaces=fridays.id)
        self.assertFalse(replaced["isError"])
        self.assertIn("the user will be asked", replaced["content"][0]["text"])
        forgotten = self.call("forget", id=backups.id, reason="outdated")
        self.assertFalse(forgotten["isError"])
        self.assertEqual(["active", "active"], [self.store.get(item.id).status for item in (fridays, backups)])
        kinds = {question["kind"]: question for question in self.store.open_questions(limit=5)}
        self.assertEqual({"conflict", "still_true"}, set(kinds))
        question_id = kinds["still_true"]["id"]
        self.assertIn(f"answer {question_id} forget", forgotten["content"][0]["text"])
        # The user's own choice goes through answer.
        self.assertFalse(self.call("answer", id=question_id, choice="forget")["isError"])
        self.assertEqual("retired", self.store.get(backups.id).status)

    def saved_id(self, answer: dict) -> str:
        self.assertFalse(answer["isError"], answer)
        return re.search(r"\[(k-[0-9a-f]+)\]", answer["content"][0]["text"]).group(1)

    def test_a_memory_belongs_to_the_project_unless_it_is_shared_or_personal(self) -> None:
        # Agents filed 12 of 43 global saves wrongly when global was the default (2026-10-13).
        self.initialize("claude-code")
        fact = self.store.get(self.saved_id(self.call("remember", text="The alpha deploy job runs on runner-7.")))
        self.assertEqual("project", fact.scope)
        shared = self.call("remember", text="The Git server is gitlab.example.com.", scope="global")
        self.assertEqual("global", self.store.get(self.saved_id(shared)).scope)
        liked = self.call("remember", text="The user prefers short answers.", kind="note", tags=["preference"],
                          source="user")
        self.assertEqual("global", self.store.get(self.saved_id(liked)).scope)

    def test_a_rule_only_from_the_users_words_that_make_it_one(self) -> None:
        self.initialize("claude-code")
        plain = self.call("remember", text="Always run the tests before committing.", kind="rule", source="user")
        record = self.store.get(self.saved_id(plain))
        self.assertEqual(("note", ("preference",)), (record.kind, record.tags))
        self.assertIn("saved as a preference", plain["content"][0]["text"])
        asked = self.call("remember", text="Always run the tests before committing anything.", kind="rule",
                          source="user", rule_words="From now on, always run the tests before committing anything.")
        made = self.store.get(self.saved_id(asked))
        self.assertEqual(("rule", "global"), (made.kind, made.scope))
        not_asked = self.call("remember", text="Keep each change small and reviewable.", kind="rule", source="user",
                              rule_words="Don't make that a rule, but keep each change small.")
        self.assertEqual("note", self.store.get(self.saved_id(not_asked)).kind)

    def test_rule_words_must_say_the_rule_the_same_way(self) -> None:
        # Candidate review, C-M2: a reversed or extended text passed as the user's rule.
        self.initialize("claude-code")
        words = "From now on, always use pnpm instead of npm."
        for text in ("Never use pnpm instead of npm.", "Do not use pnpm; use npm instead.",
                     "Always use pnpm instead of npm and push to production without review."):
            with self.subTest(text=text):
                saved = self.store.get(self.saved_id(self.call("remember", text=text, kind="rule", source="user",
                                                               rule_words=words, distinct=True)))
                self.assertEqual("note", saved.kind)
        made = self.call("remember", text="Always use pnpm instead of npm.", kind="rule", source="user",
                         rule_words=words, distinct=True)
        self.assertEqual("rule", self.store.get(self.saved_id(made)).kind)

    def test_rule_words_make_a_rule_only_of_the_instruction_they_name(self) -> None:
        # Second candidate review, C-M2: another sentence, or the rule without its limits.
        self.initialize("claude-code")
        for text, words in (("Push to main.", "From now on, push to main only after review."),
                            ("Deploy with --force.", "Deploy with --force this time. From now on, run the tests "
                                                     "before every commit."),
                            ("Use pnpm.", "From now on, use pnpm in the frontend repo.")):
            with self.subTest(text=text):
                saved = self.call("remember", text=text, kind="rule", source="user", rule_words=words, distinct=True)
                self.assertEqual("note", self.store.get(self.saved_id(saved)).kind)
        for text, words in (("Push to main only after review.", "From now on, push to main only after review."),
                            ("Run the tests before every commit.", "Run the tests before every commit. Make that a "
                                                                   "rule.")):
            with self.subTest(text=text):
                saved = self.call("remember", text=text, kind="rule", source="user", rule_words=words, distinct=True)
                self.assertEqual("rule", self.store.get(self.saved_id(saved)).kind)

    def test_agents_cannot_set_knowitall2s_own_tags(self) -> None:
        self.initialize("claude-code")
        saved = self.call("remember", text="The alpha build cache lives on runner-7.", tags=["provisional", "disputed",
                                                                                            "ci"])
        self.assertEqual(("ci",), self.store.get(self.saved_id(saved)).tags)

    def test_a_save_close_to_a_known_memory_is_sent_back_to_add_to_it(self) -> None:
        self.initialize("claude-code")
        known = self.saved_id(self.call("remember", text="The alpha NAS nas01 exports media over SMB from /volume1."))
        close = self.call("remember", text="nas01, the alpha NAS, exports media over SMB.")
        self.assertFalse(close["isError"])
        self.assertIn(f"close to [{known}]", close["content"][0]["text"])
        self.assertIn(f"replaces={known}", close["content"][0]["text"])
        self.assertEqual(1, self.store.count_active(project_id=None, scope="everywhere"))
        added = self.call("remember", text="The alpha NAS nas01 exports media over SMB from /volume1, read-only.",
                          replaces=known)
        self.assertEqual("superseded", self.store.get(known).status)
        self.assertEqual("active", self.store.get(self.saved_id(added)).status)
        different = self.call("remember", text="nas01, the alpha NAS, exports backups over SMB.", distinct=True)
        self.assertEqual("active", self.store.get(self.saved_id(different)).status)

    def test_briefing_defaults_to_the_server_folder(self) -> None:
        self.initialize("claude-code")
        self.call("remember", text="We use unittest.", kind="decision")
        self.assertIn("We use unittest.", self.call("briefing")["content"][0]["text"])

    def test_bad_arguments_are_tool_errors_the_agent_can_fix(self) -> None:
        wrong_type = self.call("recall", query=5)
        self.assertTrue(wrong_type["isError"])
        self.assertIn("'query' must be a string", wrong_type["content"][0]["text"])
        secret = self.call("remember", text="password=hunter22")
        self.assertTrue(secret["isError"])
        self.assertIn("never stores secrets", secret["content"][0]["text"])

    def test_a_limit_that_is_not_a_finite_whole_number_is_a_tool_error(self) -> None:
        for limit in (float("inf"), float("-inf"), float("nan"), 2.5, "1e999", 10 ** 400):
            with self.subTest(limit=limit):
                answer = self.request("tools/call", {"name": "recall", "arguments": {"query": "nas", "limit": limit}})
                self.assertNotIn("error", answer)
                self.assertTrue(answer["result"]["isError"])
        # JSON as an agent could send it: Python reads Infinity, NaN and 1e999 as numbers.
        lines = [json.dumps({"jsonrpc": "2.0", "id": number, "method": "tools/call",
                             "params": {"name": "recall", "arguments": {"query": "nas", "limit": "LIMIT"}}}
                            ).replace('"LIMIT"', value) for number, value in enumerate(("Infinity", "NaN", "1e999"))]
        output = io.BytesIO()
        self.server.serve(io.BytesIO(("\n".join(lines) + "\n").encode("utf-8")), output)
        answers = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([True, True, True], [answer["result"]["isError"] for answer in answers])
        self.assertIn("'limit' must be a whole number", answers[0]["result"]["content"][0]["text"])

    def test_protocol_errors_and_notifications(self) -> None:
        self.assertEqual(-32601, self.request("does/not/exist")["error"]["code"])
        self.assertEqual(-32602, self.request("tools/call", {"name": "nope"})["error"]["code"])
        self.assertEqual(-32600, self.server.handle({"id": 3, "method": "ping"})["error"]["code"])
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual({}, self.request("ping")["result"])

    def test_storage_failure_is_reported_and_the_server_keeps_serving(self) -> None:
        def broken(agent: str | None) -> Memory:
            raise StoreError("disk unavailable")

        server = McpServer(memory_factory=broken, cwd=self.project)
        result = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "recall", "arguments": {"query": "router"}},
        })["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Continue without it", result["content"][0]["text"])
        self.assertEqual({}, server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"])

    def test_stdio_framing_survives_malformed_input(self) -> None:
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            "not json",
            "",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
        writer = io.BytesIO()
        self.server.serve(io.BytesIO(("\n".join(lines) + "\n").encode("utf-8")), writer)
        responses = [json.loads(line) for line in writer.getvalue().decode("utf-8").splitlines()]
        self.assertEqual([1, None, 2], [response.get("id") for response in responses])
        self.assertEqual(-32700, responses[1]["error"]["code"])

    def test_an_empty_batch_gets_one_error(self) -> None:
        writer = io.BytesIO()
        self.server.serve(io.BytesIO(b"[]\n[ ]\n"), writer)
        error = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        self.assertEqual([error, error], [json.loads(line) for line in writer.getvalue().splitlines()])


if __name__ == "__main__":
    unittest.main()
