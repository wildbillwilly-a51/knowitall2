"""The server's admin account and its page: setup, sign-in, recovery, the compose reset, and running agents."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from knowitall2.remote import RemoteClient
from knowitall2.server import accounts, admin, open_store
from knowitall2.server.admin_routes import COOKIE, FORM_HEADER
from knowitall2.server.web import JOIN_FAILURES_PER_ADDRESS, KnowItAll2Server

NOW = "2026-10-01T12:00:00Z"
PASSWORD = "correct horse battery"


class AdminAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = open_store(Path(self.temporary.name) / "server.db")
        self.addCleanup(self.store.close)

    def test_the_first_admin_is_the_only_admin(self) -> None:
        code = admin.create(self.store, "keeper", PASSWORD, now=NOW)
        self.assertRegex(code, r"^[A-Z2-9]{4}(-[A-Z2-9]{4}){4}$")
        with self.assertRaises(admin.AdminError):
            admin.create(self.store, "someone", PASSWORD, now=NOW)
        self.assertEqual(admin.admin(self.store)["username"], "keeper")

    def test_short_passwords_and_empty_names_are_refused(self) -> None:
        for username, password in (("keeper", "short"), ("  ", PASSWORD), ("keeper", None)):
            with self.assertRaises(admin.AdminError):
                admin.create(self.store, username, password, now=NOW)
        self.assertIsNone(admin.admin(self.store))

    def test_signing_in_starts_a_session_that_ends_when_idle(self) -> None:
        admin.create(self.store, "keeper", PASSWORD, now=NOW)
        with self.assertRaises(admin.AdminError):
            admin.sign_in(self.store, "keeper", "not the password", now=NOW)
        with self.assertRaises(admin.AdminError):
            admin.sign_in(self.store, "nobody", PASSWORD, now=NOW)
        started = admin.sign_in(self.store, "Keeper", PASSWORD, now=NOW)
        found = admin.session(self.store, started["token"], now="2026-10-01T23:00:00Z")
        self.assertEqual((found["username"], found["form"]), ("keeper", started["form"]))
        self.assertIsNone(admin.session(self.store, started["token"], now="2026-10-02T11:00:01Z"))

    def test_the_recovery_code_works_once_and_signs_everyone_else_out(self) -> None:
        code = admin.create(self.store, "keeper", PASSWORD, now=NOW)
        other = admin.sign_in(self.store, "keeper", PASSWORD, now=NOW)
        with self.assertRaises(admin.AdminError):
            admin.recover(self.store, "keeper", "AAAA-BBBB-CCCC-DDDD-EEEE", "a brand new password", now=NOW)
        new_code, started = admin.recover(self.store, "keeper", code.lower(), "a brand new password", now=NOW)
        self.assertNotEqual(new_code, code)
        self.assertIsNone(admin.session(self.store, other["token"], now=NOW))
        self.assertIsNotNone(admin.session(self.store, started["token"], now=NOW))
        admin.sign_in(self.store, "keeper", "a brand new password", now=NOW)
        with self.assertRaises(admin.AdminError):
            admin.recover(self.store, "keeper", code, "yet another password", now=NOW)

    def test_changing_the_password_keeps_only_this_session(self) -> None:
        admin.create(self.store, "keeper", PASSWORD, now=NOW)
        mine, other = (admin.sign_in(self.store, "keeper", PASSWORD, now=NOW) for _ in range(2))
        with self.assertRaises(admin.AdminError):
            admin.change_password(self.store, mine["token"], "wrong", "a brand new password", now=NOW)
        admin.change_password(self.store, mine["token"], PASSWORD, "a brand new password", now=NOW)
        self.assertIsNotNone(admin.session(self.store, mine["token"], now=NOW))
        self.assertIsNone(admin.session(self.store, other["token"], now=NOW))

    def test_a_new_recovery_code_replaces_the_old_one(self) -> None:
        old = admin.create(self.store, "keeper", PASSWORD, now=NOW)
        with self.assertRaises(admin.AdminError):
            admin.replace_recovery_code(self.store, "wrong", now=NOW)
        new = admin.replace_recovery_code(self.store, PASSWORD, now=NOW)
        with self.assertRaises(admin.AdminError):
            admin.recover(self.store, "keeper", old, "a brand new password", now=NOW)
        admin.recover(self.store, "keeper", new, "a brand new password", now=NOW)

    def test_the_compose_reset_removes_only_the_admin_and_only_once_per_value(self) -> None:
        admin.create(self.store, "keeper", PASSWORD, now=NOW)
        code = accounts.new_join_code(self.store, "Laptop - Codex", now=NOW)
        accounts.redeem(self.store, code["code"], agent="codex", computer=None, version=None, now=NOW)
        self.assertFalse(admin.reset_from_setting(self.store, "  ", now=NOW))
        self.assertTrue(admin.reset_from_setting(self.store, "2026-10-01", now=NOW))
        self.assertIsNone(admin.admin(self.store))
        self.assertEqual(len(accounts.connections(self.store)), 1)
        admin.create(self.store, "keeper", PASSWORD, now=NOW)
        self.assertFalse(admin.reset_from_setting(self.store, "2026-10-01", now=NOW))
        self.assertIsNotNone(admin.admin(self.store))
        self.assertTrue(admin.reset_from_setting(self.store, "2026-10-02", now=NOW))

    def test_passwords_are_kept_only_as_hashes(self) -> None:
        code = admin.create(self.store, "keeper", PASSWORD, now=NOW)
        stored = self.store.connection.execute("SELECT password_hash, recovery_hash FROM server_admin").fetchone()
        self.assertTrue(stored[0].startswith("scrypt$"))
        self.assertNotIn(PASSWORD, stored[0])
        self.assertNotIn(code.replace("-", ""), stored[1])


class Browser:
    """A minimal browser for the page: keeps the session cookie and sends the form token."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie: str | None = None
        self.form: str | None = None

    def request(self, method: str, path: str, body=None, *, origin: str | None = "same", form: bool = True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        host = f"127.0.0.1:{self.port}"
        headers = {"Host": host}
        if origin == "same":
            headers["Origin"] = f"http://{host}"
        elif origin is not None:
            headers["Origin"] = origin
        if self.cookie:
            headers["Cookie"] = f"{COOKIE}={self.cookie}"
        if form and self.form:
            headers[FORM_HEADER] = self.form
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            cookie = response.getheader("Set-Cookie")
            if cookie:
                value = cookie.split(";", 1)[0].split("=", 1)[1]
                self.cookie = value or None
                self.set_cookie = cookie
            kind = response.getheader("Content-Type") or ""
            data = json.loads(raw) if kind.startswith("application/json") else raw
            if isinstance(data, dict) and data.get("form"):
                self.form = data["form"]
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()


class AdminPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.start()
        self.browser = Browser(self.server.port)

    def start(self, reset: str | None = None) -> None:
        self.server = KnowItAll2Server(self.home, port=0, reset_value=reset)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.stop()
        self.thread.join(timeout=5)

    def tearDown(self) -> None:
        self.stop()

    def set_up_admin(self) -> str:
        status, data, _ = self.browser.request("POST", "/admin/api/setup", {"username": "keeper", "new_password": PASSWORD})
        self.assertEqual(status, 200, data)
        return data["recovery_code"]

    def test_the_page_and_its_files_are_served_with_a_strict_policy(self) -> None:
        for path, kind in (("/", "text/html"), ("/admin.js", "text/javascript"), ("/admin.css", "text/css"),
                           ("/icon.svg", "image/svg+xml")):
            status, _, headers = self.browser.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(kind))
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_the_first_visitor_sets_up_the_admin_and_is_signed_in(self) -> None:
        self.assertTrue(self.browser.request("GET", "/admin/api/state")[1]["setup_needed"])
        code = self.set_up_admin()
        self.assertRegex(code, r"^[A-Z2-9]{4}(-[A-Z2-9]{4}){4}$")
        self.assertIn("HttpOnly", self.browser.set_cookie)
        self.assertIn("SameSite=Strict", self.browser.set_cookie)
        self.assertNotIn("Secure", self.browser.set_cookie)
        state = self.browser.request("GET", "/admin/api/state")[1]
        self.assertEqual((state["setup_needed"], state["signed_in"], state["username"]), (False, True, "keeper"))
        later = Browser(self.server.port)
        status, data, _ = later.request("POST", "/admin/api/setup", {"username": "me", "new_password": PASSWORD})
        self.assertEqual(status, 400)
        self.assertIn("already set up", data["error"])

    def test_signed_out_visitors_see_nothing_of_the_server(self) -> None:
        self.set_up_admin()
        stranger = Browser(self.server.port)
        for method, path in (("GET", "/admin/api/overview"), ("GET", "/admin/api/backup"),
                             ("POST", "/admin/api/agents/add")):
            status, _, _ = stranger.request(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 401, path)

    def test_adding_an_agent_gives_a_code_an_agent_can_spend(self) -> None:
        self.set_up_admin()
        status, made, _ = self.browser.request("POST", "/admin/api/agents/add", {"name": "Laptop - Codex"})
        self.assertEqual(status, 200, made)
        overview = self.browser.request("GET", "/admin/api/overview")[1]
        self.assertEqual([code["name"] for code in overview["codes"]], ["Laptop - Codex"])
        client = RemoteClient(f"http://127.0.0.1:{self.server.port}", agent="codex", computer="laptop")
        client.join(made["code"])
        overview = self.browser.request("GET", "/admin/api/overview")[1]
        self.assertEqual(overview["codes"], [])
        [agent] = overview["agents"]
        self.assertEqual((agent["name"], agent["agent"], agent["computer"]), ("Laptop - Codex", "codex", "laptop"))
        self.assertEqual(overview["server"]["memories"], 0)
        status, _, _ = self.browser.request("POST", "/admin/api/agents/remove", {"id": agent["id"]})
        self.assertEqual(status, 200)
        with self.assertRaises(Exception):
            client.hello()

    def test_changes_need_the_form_token_and_the_pages_own_origin(self) -> None:
        self.set_up_admin()
        status, data, _ = self.browser.request("POST", "/admin/api/agents/add", {"name": "x"}, form=False)
        self.assertEqual(status, 403)
        status, _, _ = self.browser.request("POST", "/admin/api/agents/add", {"name": "x"},
                                            origin="http://evil.example")
        self.assertEqual(status, 403)
        status, _, _ = self.browser.request("POST", "/admin/api/agents/add", {"name": "x"}, origin=None)
        self.assertEqual(status, 200)

    def test_an_agents_key_does_not_open_the_page_and_the_session_does_not_open_the_api(self) -> None:
        self.set_up_admin()
        made = self.browser.request("POST", "/admin/api/agents/add", {"name": "Laptop - Codex"})[1]
        client = RemoteClient(f"http://127.0.0.1:{self.server.port}", agent="codex")
        key = client.join(made["code"])["key"]
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        connection.request("GET", "/admin/api/overview", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(connection.getresponse().status, 401)
        connection.close()
        status, _, _ = self.browser.request("GET", "/api/v1/hello")
        self.assertEqual(status, 401)

    def test_signing_in_and_out(self) -> None:
        self.set_up_admin()
        self.browser.request("POST", "/admin/api/sign-out", {})
        self.assertFalse(self.browser.request("GET", "/admin/api/state")[1]["signed_in"])
        status, _, _ = self.browser.request("POST", "/admin/api/sign-in", {"username": "keeper", "password": "nope"})
        self.assertEqual(status, 400)
        status, _, _ = self.browser.request("POST", "/admin/api/sign-in", {"username": "keeper", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(self.browser.request("GET", "/admin/api/state")[1]["signed_in"])

    def test_guessing_passwords_is_slowed_down(self) -> None:
        self.set_up_admin()
        guesser = Browser(self.server.port)
        for _ in range(JOIN_FAILURES_PER_ADDRESS):
            guesser.request("POST", "/admin/api/sign-in", {"username": "keeper", "password": "guess"})
        status, _, _ = guesser.request("POST", "/admin/api/sign-in", {"username": "keeper", "password": PASSWORD})
        self.assertEqual(status, 429)

    def test_recovering_with_the_code(self) -> None:
        code = self.set_up_admin()
        stranger = Browser(self.server.port)
        status, data, _ = stranger.request("POST", "/admin/api/recover", {
            "username": "keeper", "recovery_code": code, "new_password": "a brand new password"})
        self.assertEqual(status, 200, data)
        self.assertNotEqual(data["recovery_code"], code)
        self.assertTrue(stranger.request("GET", "/admin/api/state")[1]["signed_in"])
        self.assertFalse(self.browser.request("GET", "/admin/api/state")[1]["signed_in"])

    def test_the_compose_reset_brings_back_setup_and_a_reminder(self) -> None:
        self.set_up_admin()
        self.stop()
        self.start(reset="2026-10-01")
        browser = Browser(self.server.port)
        state = browser.request("GET", "/admin/api/state")[1]
        self.assertEqual((state["setup_needed"], state["reset_reminder"]), (True, True))
        self.assertTrue(self.server.admin_was_reset)
        status, _, _ = browser.request("POST", "/admin/api/setup", {"username": "keeper", "new_password": PASSWORD})
        self.assertEqual(status, 200)
        self.stop()
        self.start(reset="2026-10-01")
        self.assertFalse(self.server.admin_was_reset)
        self.assertFalse(Browser(self.server.port).request("GET", "/admin/api/state")[1]["setup_needed"])

    def test_downloading_a_backup(self) -> None:
        self.set_up_admin()
        status, data, headers = self.browser.request("GET", "/admin/api/backup")
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"SQLite format 3"))
        self.assertIn('attachment; filename="knowitall2-server-', headers["Content-Disposition"])

    def test_changing_the_password_and_making_a_new_recovery_code(self) -> None:
        self.set_up_admin()
        status, _, _ = self.browser.request("POST", "/admin/api/password",
                                            {"current": PASSWORD, "new_password": "a brand new password"})
        self.assertEqual(status, 200)
        status, data, _ = self.browser.request("POST", "/admin/api/recovery-code", {"password": "a brand new password"})
        self.assertEqual(status, 200)
        self.assertIn("recovery_code", data)


if __name__ == "__main__":
    unittest.main()
