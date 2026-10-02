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


if __name__ == "__main__":
    unittest.main()
