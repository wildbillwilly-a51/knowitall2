"""A KnowItAll2 server behind a proxy: when the server is stopped, the proxy answers for it."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from knowitall2 import connected, doctor
from knowitall2.app import api
from knowitall2.paths import database_path
from knowitall2.remote import RemoteClient, RemoteError
from knowitall2.store import Store

BAD_GATEWAY = b"<html><head><title>502 Bad Gateway</title></head><body>Bad Gateway</body></html>"


class StandIn(BaseHTTPRequestHandler):
    """Answers every request with the status and body its server was given."""

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def _answer(self) -> None:
        status, kind, body = self.server.answer  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class BehindAProxy(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
        self.server.answer = (502, "text/html", BAD_GATEWAY)  # type: ignore[attr-defined]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"

    def answer(self, status: int, kind: str, body: bytes) -> None:
        self.server.answer = (status, kind, body)  # type: ignore[attr-defined]

    def failure(self) -> RemoteError:
        with self.assertRaises(RemoteError) as caught:
            RemoteClient(self.address, key="k").hello()
        return caught.exception

    def test_a_proxy_saying_the_server_is_down_reads_as_unreachable(self) -> None:
        for status in (502, 503, 504):
            with self.subTest(status=status):
                self.answer(status, "text/html", BAD_GATEWAY)
                error = self.failure()
                self.assertEqual(str(error), f"cannot reach the KnowItAll2 server at {self.address}: "
                                             f"its proxy says it is not answering ({status})")
                self.assertEqual(error.status, status)

    def test_the_servers_own_error_is_still_its_own_words(self) -> None:
        self.answer(503, "application/json", json.dumps({"error": "the server is starting"}).encode())
        error = self.failure()
        self.assertEqual(str(error), "the KnowItAll2 server said: the server is starting")
        self.assertEqual(error.status, 503)
        self.answer(500, "text/html", b"<html>oops</html>")
        self.assertEqual(str(self.failure()), "the KnowItAll2 server said: Internal Server Error")

    def test_status_sync_doctor_and_the_app_call_it_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"KNOWITALL2_HOME": home}):
            connected.folder().mkdir(parents=True)
            (connected.folder() / "connection.json").write_text(json.dumps({"address": self.address}),
                                                                encoding="utf-8")
            connected.save_key("codex", "k")

            healthy, lines = connected.describe_status()
            self.assertFalse(healthy)
            self.assertIn("The server cannot be reached right now (cannot reach the KnowItAll2 server at "
                          f"{self.address}: its proxy says it is not answering (502))", " ".join(lines))
            self.assertNotIn("server said", " ".join(lines))

            check = doctor._sharing_check()
            self.assertFalse(check.ok)
            self.assertIn("Check that the KnowItAll2 server is running and reachable", check.fix)

            store = Store.open(database_path())
            try:
                report = connected.sync_now(store)
            finally:
                store.close()
            self.assertEqual(report.status, 502)
            self.assertIn("its proxy says it is not answering (502)", report.describe())
            self.assertEqual(connected.read_status()["problem_status"], 502)

            health = {"level": "ok", "headline": "Everything is working", "items": []}
            api._add_sharing_health(health, api._sharing())
            self.assertEqual(health["items"][0]["level"], "attention")
            self.assertIn("could not be reached at the last try", health["items"][0]["text"])


if __name__ == "__main__":
    unittest.main()
