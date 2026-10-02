"""Setting up agents with a KnowItAll2 server, through the command line: setup, server status, and back again."""

import io
import json
import os
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _support import Clock

from knowitall2 import connected
from knowitall2.app import api
from knowitall2.cli import main
from knowitall2.memory import Memory
from knowitall2.paths import database_path
from knowitall2.server import accounts
from knowitall2.server.web import KnowItAll2Server
from knowitall2.store import Store


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServerSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.user_home = self.root / "user"
        (self.user_home / ".codex").mkdir(parents=True)
        (self.user_home / ".claude").mkdir()
        (self.user_home / ".claude.json").write_text(json.dumps({"numStartups": 1}) + "\n", encoding="utf-8")
        self.plain = self.root / "plain"
        self.plain.mkdir()
        environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "data")})
        environment.start()
        self.addCleanup(environment.stop)
        self.port = free_port()
        self.address = f"http://127.0.0.1:{self.port}"
        self.server = KnowItAll2Server(self.root / "server", port=self.port)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.stop()
        self.thread.join(timeout=5)

    def code(self, name: str = "Workstation - Codex") -> str:
        store = self.server.open()
        try:
            return accounts.new_join_code(store, name, now=self.server.clock())["code"]
        finally:
            store.close()

    def run_cli(self, *arguments: str, user_home: bool = True) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        extra = ["--user-home", str(self.user_home)] if user_home else []
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([*arguments, *extra])
        return code, output.getvalue(), errors.getvalue()

    def remember(self, text: str) -> None:
        store = Store.open(database_path())
        try:
            Memory(store, agent="codex", clock=Clock()).remember(text, project_path=self.plain)
        finally:
            store.close()

    def test_setting_up_an_agent_with_the_server(self) -> None:
        code, output, errors = self.run_cli("setup", "codex", "--server", self.address, "--join-code", self.code())
        self.assertEqual(code, 0, errors)
        self.assertIn(f"connected codex to the KnowItAll2 server at {self.address}", output)
        self.assertIn("registered the knowitall2 MCP server", output)
        code, output, _ = self.run_cli("setup", "claude-code", "--join-code", self.code("Workstation - Claude Code"))
        self.assertEqual(code, 0, output)
        self.assertIn('as "Workstation - Claude Code"', output)
        code, output, _ = self.run_cli("server", "status", user_home=False)
        self.assertEqual(code, 0, output)
        self.assertIn("Agents here with a key: claude-code, codex.", output)
        self.assertIn("The server answers", output)
        code, output, _ = self.run_cli("doctor")
        self.assertIn("OK   shared memory: Connected to the KnowItAll2 server", output)

    def test_a_bad_code_changes_nothing(self) -> None:
        code, _, errors = self.run_cli("setup", "codex", "--server", self.address, "--join-code", "AAAA-BBBB-CCCC-DDDD")
        self.assertEqual(code, 1)
        self.assertIn("join code does not work", errors)
        self.assertFalse(connected.is_connected())
        self.assertFalse((self.user_home / ".codex" / "config.toml").exists())

    def test_the_first_agent_needs_the_servers_address_and_a_code_needs_a_server(self) -> None:
        code, _, errors = self.run_cli("setup", "codex", "--join-code", self.code())
        self.assertEqual(code, 1)
        self.assertIn("--server", errors)
        code, _, errors = self.run_cli("setup", "codex", "--server", self.address)
        self.assertEqual(code, 1)
        self.assertIn("--join-code", errors)

    def test_a_computer_with_memories_must_say_what_happens_to_them(self) -> None:
        self.remember("The build server is build-1.")
        join = self.code()
        code, _, errors = self.run_cli("setup", "codex", "--server", self.address, "--join-code", join)
        self.assertEqual(code, 1)
        self.assertIn("already has 1 memories", errors)
        self.assertIn("--send-memories", errors)
        self.assertFalse(connected.is_connected())
        code, output, errors = self.run_cli("setup", "codex", "--server", self.address, "--join-code", join,
                                            "--send-memories")
        self.assertEqual(code, 0, errors)
        self.assertIn("sent this computer's memory to the server (1 items)", output)
        store = self.server.open()
        try:
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1)
        finally:
            store.close()

    def test_replacing_this_computers_memories_keeps_a_backup(self) -> None:
        self.remember("The build server is build-1.")
        code, output, errors = self.run_cli("setup", "codex", "--server", self.address, "--join-code", self.code(),
                                            "--replace-memories")
        self.assertEqual(code, 0, errors)
        self.assertIn("set this computer's memories aside", output)
        backup = database_path().with_name("knowitall2.pre-server-backup.db")
        self.assertTrue(backup.exists())
        store = Store.open(database_path())
        try:
            self.assertEqual(connected.local_memories(store), 0)
        finally:
            store.close()

    def test_uninstalling_the_last_agent_disconnects_the_computer(self) -> None:
        self.run_cli("setup", "codex", "--server", self.address, "--join-code", self.code())
        self.run_cli("setup", "claude-code", "--join-code", self.code("Workstation - Claude Code"))
        code, output, _ = self.run_cli("uninstall", "claude-code")
        self.assertIn("removed claude-code's key", output)
        self.assertTrue(connected.is_connected())
        code, output, _ = self.run_cli("uninstall", "codex")
        self.assertIn("keeps its memory here only again", output)
        self.assertFalse(connected.is_connected())

    def test_connecting_later_and_disconnecting(self) -> None:
        self.run_cli("setup", "codex")
        code, output, errors = self.run_cli("server", "connect", "codex", "--server", self.address,
                                            "--join-code", self.code(), user_home=False)
        self.assertEqual(code, 0, errors)
        self.assertIn("connected codex", output)
        code, output, _ = self.run_cli("server", "disconnect", user_home=False)
        self.assertIn("keeps its memory here only again", output)
        code, output, _ = self.run_cli("server", "status", user_home=False)
        self.assertIn("not connected", output)

    def test_doctor_and_the_app_say_when_the_server_cannot_be_reached(self) -> None:
        self.run_cli("setup", "codex", "--server", self.address, "--join-code", self.code())
        self.stop_server()
        store = Store.open(database_path())
        try:
            connected.sync_now(store)
        finally:
            store.close()
        code, output, _ = self.run_cli("doctor", "--agent", "codex")
        self.assertIn("FAIL shared memory", output)
        self.assertIn("cannot be reached", output)
        sharing = api._sharing()
        health = {"level": "ok", "headline": "", "items": []}
        api._add_sharing_health(health, sharing)
        self.assertEqual(health["level"], "attention")
        self.assertIn("could not be reached", health["items"][0]["text"])


class PlainHttpTests(unittest.TestCase):
    def test_plain_http_is_noted_unless_it_stays_on_a_private_network(self) -> None:
        for address in ("http://127.0.0.1:4191", "http://10.0.0.5:4191", "http://localhost:4191",
                        "https://kia.example.com"):
            self.assertIsNone(connected.plain_http_warning(address), address)
        self.assertIn("fine on your own network", connected.plain_http_warning("http://kia.lan"))
        self.assertIn("public address", connected.plain_http_warning("http://8.8.8.8"))


if __name__ == "__main__":
    unittest.main()
