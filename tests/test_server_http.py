"""The HTTP edges of both web servers: kept-alive connections, odd lengths, and what an error tells the caller."""

import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_app import KEY, AppTestCase

from knowitall2 import journal
from knowitall2.app import server as app_server
from knowitall2.server import web
from knowitall2.server.web import KnowItAll2Server, ServerHandler
from knowitall2.store import StoreError

# Deeper than older JSON parsers' recursion allowance, and well under the body cap.
DEEP_JSON = b"[" * 100_000 + b"]" * 100_000
# Newer Pythons (3.14) may parse that deep, and a list is then simply not an object: a 400 either way.
DEEP_JSON_ERRORS = ({"error": "the request was not valid JSON"}, {"error": "the request must be a JSON object"})


def answer(sock: socket.socket, method: str) -> tuple[int, bytes]:
    """The status and body of the response waiting on a raw socket; a test failure if none comes in time."""

    response = http.client.HTTPResponse(sock, method=method)
    try:
        response.begin()
        return response.status, response.read()
    except TimeoutError:
        raise AssertionError("the server did not answer") from None
    finally:
        response.close()


def closed_by_server(sock: socket.socket) -> bool:
    try:
        return sock.recv(1) == b""
    except ConnectionError:
        return True
    except TimeoutError:
        return False


class SyncServerTestCase(unittest.TestCase):
    """A real sync server on a free port, with its own problems journal."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "client-home")})
        environment.start()
        self.addCleanup(environment.stop)
        journal._last_problem.clear()

    def start(self) -> KnowItAll2Server:
        server = KnowItAll2Server(self.root / "server-home", port=0, housekeeping_seconds=3600)
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.stop)
        return server

    def raw(self, server: KnowItAll2Server) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def post(self, server: KnowItAll2Server, path: str, body: bytes) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


class KeepAliveTests(SyncServerTestCase):
    def test_a_head_request_leaves_the_connection_ready_for_the_next_request(self) -> None:
        server = self.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("HEAD", "/api/v1/health")
        head = connection.getresponse()
        self.assertEqual((head.status, head.read()), (200, b""))
        kept = connection.sock
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        try:
            body = response.read()
        except (TimeoutError, http.client.IncompleteRead):
            self.fail("the GET after a HEAD on the same connection got its headers but no body")
        self.assertIs(connection.sock, kept)  # the same kept-alive connection, as a proxy's pool would use it
        self.assertEqual(len(body), int(response.getheader("Content-Length")))
        self.assertEqual(json.loads(body)["product"], "knowitall2")

    def test_an_idle_connection_is_closed_after_the_handler_timeout(self) -> None:
        self.assertGreaterEqual(ServerHandler.timeout or 0, 120)  # above the 90 s Traefik keeps one idle
        with mock.patch.object(ServerHandler, "timeout", 0.3):
            server = self.start()
            sock = self.raw(server)
            sock.sendall(f"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n\r\n".encode("ascii"))
            self.assertEqual(answer(sock, "GET")[0], 200)
            self.assertTrue(closed_by_server(sock), "the server kept an idle connection open")


class ContentLengthTests(SyncServerTestCase):
    def test_a_negative_length_is_refused_at_once(self) -> None:
        server = self.start()
        sock = self.raw(server)
        sock.sendall(b"POST /api/v1/push HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     b"Content-Length: -1\r\n\r\n")
        status, body = answer(sock, "POST")
        self.assertEqual((status, json.loads(body)), (400, {"error": "bad Content-Length"}))


class EarlyRefusalTests(SyncServerTestCase):
    def test_a_post_refused_before_its_body_is_read_leaves_the_connection_ready_for_the_next_request(self) -> None:
        server = self.start()
        body = json.dumps({"operations": []}).encode("utf-8")
        for name, path, kind, status in (("unknown route", "/api/v1/not-here", "application/json", 404),
                                         ("wrong method", "/api/v1/hello", "application/json", 405),
                                         ("not JSON", "/api/v1/push", "text/plain", 415)):
            with self.subTest(name):
                connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                self.addCleanup(connection.close)
                connection.request("POST", path, body=body, headers={"Content-Type": kind})
                refused = connection.getresponse()
                refused.read()
                self.assertEqual(refused.status, status)
                kept = connection.sock
                connection.request("GET", "/api/v1/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["product"], "knowitall2")
                self.assertIs(connection.sock, kept)  # the same kept-alive connection, as a proxy's pool would use it

    def test_a_body_too_large_still_gets_its_answer_and_the_connection_closes(self) -> None:
        server = self.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/v1/push", body=b" " * (web.MAX_BODY_BYTES + 1),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        self.assertIn("too large", json.loads(response.read())["error"])
        self.assertEqual(response.getheader("Connection"), "close")


class AppDeepJsonTests(AppTestCase):
    def test_deeply_nested_json_is_not_valid_json(self) -> None:
        deep = b"[" * 20_000 + b"]" * 20_000  # within the app's smaller body cap
        self.assertLess(len(deep), app_server.MAX_BODY_BYTES)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/doctor", body=deep, headers={
            "Host": f"127.0.0.1:{self.server.port}", app_server.KEY_HEADER: KEY, "Content-Type": "application/json"})
        response = connection.getresponse()
        # A 400 either way, never a 500 that repeats the parser's error.
        self.assertEqual(response.status, 400)
        self.assertIn(json.loads(response.read()), DEEP_JSON_ERRORS)


class AppEarlyRefusalTests(AppTestCase):
    def test_a_request_refused_before_its_body_is_read_leaves_the_connection_ready_for_the_next_request(self) -> None:
        body = json.dumps({"query": "router"}).encode("utf-8")
        json_type = {"Content-Type": "application/json"}
        key = {app_server.KEY_HEADER: KEY}
        for name, method, path, headers, status in (
            ("no key", "POST", "/api/doctor", json_type, 401),
            ("another host", "POST", "/api/doctor", {**json_type, **key, "Host": "evil.example.com"}, 403),
            ("another origin", "POST", "/api/doctor", {**json_type, **key, "Origin": "https://evil.example.com"}, 403),
            ("not JSON", "POST", "/api/doctor", {**key, "Content-Type": "text/plain"}, 415),
            ("not the API", "POST", "/elsewhere", json_type, 404),
            ("cross-origin", "OPTIONS", "/api/doctor", json_type, 403),
            ("a GET with a body", "GET", "/api/ping", {**json_type, **key}, 200),
        ):
            with self.subTest(name):
                connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
                self.addCleanup(connection.close)
                connection.request(method, path, body=body, headers=headers)
                refused = connection.getresponse()
                refused.read()
                self.assertEqual(refused.status, status)
                kept = connection.sock
                connection.request("GET", "/api/ping", headers=key)
                try:
                    response = connection.getresponse()
                    self.assertEqual((response.status, json.loads(response.read())["ok"]), (200, True))
                except (TimeoutError, ValueError, http.client.HTTPException):
                    self.fail("the request after the refusal on the same connection was not answered")
                self.assertIs(connection.sock, kept)  # the same kept-alive connection

    def test_a_body_too_large_still_gets_its_answer_and_the_connection_closes(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/doctor", body=b" " * (app_server.MAX_BODY_BYTES + 1), headers={
            app_server.KEY_HEADER: KEY, "Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        self.assertIn("too large", json.loads(response.read())["error"])
        self.assertEqual(response.getheader("Connection"), "close")


class AppContentLengthTests(AppTestCase):
    def test_a_negative_length_is_refused_at_once(self) -> None:
        host = f"127.0.0.1:{self.server.port}"
        requests = {
            # The goodbye beacon reads its body (which carries the key) before any key check.
            "goodbye beacon": f"POST /api/bye HTTP/1.1\r\nHost: {host}\r\nContent-Type: text/plain\r\n",
            "change": f"POST /api/doctor HTTP/1.1\r\nHost: {host}\r\n{app_server.KEY_HEADER}: {KEY}\r\n"
                      f"Content-Type: application/json\r\n",
        }
        for name, head in requests.items():
            with self.subTest(name):
                sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
                try:
                    sock.sendall((head + "Content-Length: -1\r\n\r\n").encode("ascii"))
                    status, body = answer(sock, "POST")
                finally:
                    sock.close()
                self.assertEqual((status, json.loads(body)), (400, {"error": "bad Content-Length"}))


class ErrorMessageTests(SyncServerTestCase):
    def test_deeply_nested_json_is_not_valid_json(self) -> None:
        server = self.start()
        status, body = self.post(server, "/api/v1/push", DEEP_JSON)
        self.assertEqual(status, 400)
        self.assertIn(body, DEEP_JSON_ERRORS)

    def test_an_unexpected_failure_says_little_and_the_journal_has_the_detail(self) -> None:
        def broken(handler, query, body):
            raise RuntimeError("cannot open /srv/knowitall2/private-detail")

        server = self.start()
        with mock.patch.dict(web.ROUTES, {("POST", "/api/v1/usage"): broken}):
            status, body = self.post(server, "/api/v1/usage", b"{}")
        self.assertEqual(status, 500)
        self.assertNotIn("private-detail", body["error"])
        self.assertIn("could not do that", body["error"])
        problems = journal.read_problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("RuntimeError: cannot open /srv/knowitall2/private-detail", problems[0]["message"])

    def test_a_store_failure_still_says_what_failed(self) -> None:
        def unavailable(handler, query, body):
            raise StoreError("the database is locked")

        server = self.start()
        with mock.patch.dict(web.ROUTES, {("POST", "/api/v1/usage"): unavailable}):
            status, body = self.post(server, "/api/v1/usage", b"{}")
        self.assertEqual((status, body), (503, {"error": "the server's store failed: the database is locked"}))


if __name__ == "__main__":
    unittest.main()
