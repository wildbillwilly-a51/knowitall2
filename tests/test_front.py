import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401  (puts src on the path)

from knowitall2.front import Front, WorkerError

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-code"}, "capabilities": {}}}


class FakeWorker:
    def __init__(self) -> None:
        self.requests = []
        self.tools = "v1"
        self.fail = False

    def __call__(self, request):
        self.requests.append(request)
        if self.fail:
            raise WorkerError("it could not start")
        message = request["message"]
        return {"response": {"jsonrpc": "2.0", "id": message["id"], "result": {"method": message["method"]}},
                "tools": self.tools}


class FrontTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = FakeWorker()
        self.front = Front(worker=self.worker)

    def test_every_request_goes_to_a_fresh_worker_with_the_agents_greeting(self) -> None:
        self.front.handle(INITIALIZE)
        self.assertEqual([], self.front.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        [reply] = self.front.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "recall"}})
        self.assertEqual({"method": "tools/call"}, reply["result"])
        self.assertEqual(INITIALIZE["params"], self.worker.requests[-1]["initialize"])
        self.assertEqual({"jsonrpc": "2.0", "id": 3, "result": {}}, self.front.handle({"jsonrpc": "2.0", "id": 3,
                                                                                       "method": "ping"})[0])
        self.assertEqual(2, len(self.worker.requests))  # ping and notifications need no worker

    def test_a_changed_tool_list_is_announced_once(self) -> None:
        self.front.handle(INITIALIZE)
        self.front.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.worker.tools = "v2"  # an update landed
        replies = self.front.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}})
        self.assertEqual(["tools/call", "notifications/tools/list_changed"],
                         [reply.get("result", {}).get("method") or reply.get("method") for reply in replies])
        # An agent that does not list the tools again (Codex) is told once, not on every call.
        self.assertEqual(1, len(self.front.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {}})))

    def test_a_failing_worker_never_breaks_the_session(self) -> None:
        self.worker.fail = True
        [connected] = self.front.handle(INITIALIZE)
        self.assertEqual("knowitall2", connected["result"]["serverInfo"]["name"])
        [call] = self.front.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}})
        self.assertTrue(call["result"]["isError"])
        self.assertIn("KnowItAll2 is unavailable right now (it could not start)", call["result"]["content"][0]["text"])
        [listed] = self.front.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual(-32603, listed["error"]["code"])

    def test_the_front_loads_nothing_but_the_standard_library(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src"
        code = ("import sys; import knowitall2.front; "
                "print(sorted(name for name in sys.modules if name.startswith('knowitall2')))")
        done = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PYTHONPATH": str(source)})
        self.assertEqual("['knowitall2', 'knowitall2.front']", done.stdout.strip(), done.stderr)


class OpenSessionUpdateTests(unittest.TestCase):
    """A real front, on a copy of the code that is updated while the session is open."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "src"
        shutil.copytree(Path(__file__).resolve().parents[1] / "src" / "knowitall2", self.source / "knowitall2",
                        ignore=shutil.ignore_patterns("__pycache__"))
        environment = {**os.environ, "PYTHONPATH": str(self.source), "KNOWITALL2_HOME": str(root / "home")}
        self.process = subprocess.Popen([sys.executable, "-B", "-m", "knowitall2", "serve"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, cwd=str(root), env=environment)

    def tearDown(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=30)
        self.process.stdout.close()
        self.temporary.cleanup()

    def send(self, message: dict, *, replies: int = 1) -> list[dict]:
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.process.stdin.flush()
        return [json.loads(self.process.stdout.readline()) for _ in range(replies)]

    def test_an_update_reaches_the_open_session_on_its_next_request(self) -> None:
        self.send(INITIALIZE)
        [listed] = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        recall = next(tool for tool in listed["result"]["tools"] if tool["name"] == "recall")
        self.assertNotIn("UPDATED", recall["description"])
        # Update the installed code under the running session.
        module = self.source / "knowitall2" / "mcp_server.py"
        text = module.read_text(encoding="utf-8")
        marker = '"name": "recall",'
        self.assertIn(marker, text)
        start = text.index('"description": (', text.index(marker))
        module.write_text(text[:start] + '"description": ("UPDATED " ' + text[start + len('"description": ('):],
                          encoding="utf-8")
        replies = self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "recall", "arguments": {"query": "anything"}}}, replies=2)
        self.assertEqual([3, "notifications/tools/list_changed"],
                         [replies[0].get("id"), replies[1].get("method")])
        [listed] = self.send({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        recall = next(tool for tool in listed["result"]["tools"] if tool["name"] == "recall")
        self.assertTrue(recall["description"].startswith("UPDATED "))


if __name__ == "__main__":
    unittest.main()
