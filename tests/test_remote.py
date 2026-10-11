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

from _support import SystemProxy

from knowitall2 import connected, doctor
from knowitall2.app import api
from knowitall2.paths import database_path
from knowitall2.remote import RemoteClient, RemoteError, bypasses_proxy
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
                self.assertEqual(str(error), f"cannot reach the KnowItAll2 server at {self.address}: a proxy "
                                             f"between here and the server says it is not answering ({status})")
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
                          f"{self.address}: a proxy between here and the server says it is not answering (502))",
                          " ".join(lines))
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
            self.assertIn("a proxy between here and the server says it is not answering (502)", report.describe())
            self.assertEqual(connected.read_status()["problem_status"], 502)

            health = {"level": "ok", "headline": "Everything is working", "items": []}
            api._add_sharing_health(health, api._sharing())
            self.assertEqual(health["items"][0]["level"], "attention")
            self.assertIn("could not be reached at the last try", health["items"][0]["text"])


class Moved(BaseHTTPRequestHandler):
    """Answers every request with its server's redirect status, to its server's ``to``."""

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def _answer(self) -> None:
        self.server.seen.append(self.headers.get("Authorization"))  # type: ignore[attr-defined]
        # Read what was sent: Windows resets a connection closed with it unread, sometimes before the client reads the answer.
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(self.server.status)  # type: ignore[attr-defined]
        self.send_header("Location", self.server.to)  # type: ignore[attr-defined]
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


class Redirects(unittest.TestCase):
    """A redirect is refused: urllib would send the key, an ordinary header, to wherever it points."""

    def serve(self) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Moved)
        server.seen, server.status, server.to = [], 200, ""  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_a_redirect_to_another_server_is_refused_and_the_key_stays_here(self) -> None:
        here, elsewhere = self.serve(), self.serve()
        port = elsewhere.server_address[1]
        here.to = f"http://localhost:{port}/login?rd=somewhere"  # type: ignore[attr-defined]
        key = "kia_" + "x" * 43
        for status in (301, 302, 303, 307, 308):
            here.status = status  # type: ignore[attr-defined]
            for call in (lambda client: client.hello(), lambda client: client.push([])):
                with self.subTest(status=status), self.assertRaises(RemoteError) as caught:
                    call(RemoteClient(f"http://127.0.0.1:{here.server_address[1]}", key=key))
                self.assertEqual(status, caught.exception.status)
                self.assertIn(f"answered with a redirect to http://localhost:{port} ({status})", str(caught.exception))
                self.assertNotIn("rd=somewhere", str(caught.exception))
        self.assertEqual([f"Bearer {key}"] * 10, here.seen)  # type: ignore[attr-defined]
        self.assertEqual([], elsewhere.seen)  # type: ignore[attr-defined]


class SystemProxyTests(BehindAProxy):
    """A system proxy is for the internet: a server on this computer or its own network is reached directly."""

    def test_a_server_here_is_reached_without_the_system_proxy(self) -> None:
        self.answer(200, "application/json", json.dumps({"ok": True}).encode())
        with SystemProxy() as proxy:
            self.assertEqual({"ok": True}, RemoteClient(self.address, key="secret-key").hello())
        self.assertEqual([], proxy.seen)

    def test_which_servers_skip_the_system_proxy(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1", "10.1.2.3", "172.20.0.5", "192.168.1.20", "100.101.102.103",
                     "169.254.10.1", "fe80::1", "fd12::5", "nas", "nas.local", "knowitall2.localhost"):
            with self.subTest(host=host):
                self.assertTrue(bypasses_proxy(host))
        for host in ("knowitall2.example.com", "8.8.8.8", "100.128.0.1", "2001:4860::8888"):
            with self.subTest(host=host):
                self.assertFalse(bypasses_proxy(host))


if __name__ == "__main__":
    unittest.main()
