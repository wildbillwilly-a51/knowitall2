"""The server's admin account and its page: setup, sign-in, recovery, the compose reset, and running agents."""

import base64
import hashlib
import http.client
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (keeps the journal out of the real data home)

from knowitall2 import journal
from knowitall2.remote import RemoteClient
from knowitall2.server import accounts, admin, open_store, web
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

    def test_new_passwords_use_a_cost_that_older_versions_can_still_check(self) -> None:
        stored = admin.hash_password(PASSWORD)
        _, n, r, p, salt, digest = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        self.assertEqual((n, r, p), (2 ** 14, 8, 5))
        # What scrypt needs (OpenSSL's count), within the 64 MiB limit that 0.8.4 and earlier check with.
        self.assertLessEqual(128 * r * (n + p + 2), 64 * 1024 * 1024)
        checked = hashlib.scrypt(PASSWORD.encode("utf-8"), salt=base64.b64decode(salt), n=n, r=r, p=p, dklen=32,
                                 maxmem=64 * 1024 * 1024)
        self.assertEqual(checked, base64.b64decode(digest))

    def test_a_password_kept_at_the_old_cost_still_works_and_is_upgraded_on_sign_in(self) -> None:
        admin.create(self.store, "keeper", PASSWORD, now=NOW)
        salt = b"0123456789abcdef"
        digest = hashlib.scrypt(PASSWORD.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32,
                                maxmem=64 * 1024 * 1024)
        old = f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
        self.store.connection.execute("UPDATE server_admin SET password_hash = ?", (old,))
        with self.assertRaises(admin.AdminError):
            admin.sign_in(self.store, "keeper", "not the password", now=NOW)
        stored = lambda: self.store.connection.execute("SELECT password_hash FROM server_admin").fetchone()[0]  # noqa: E731
        self.assertEqual(stored(), old)
        admin.sign_in(self.store, "keeper", PASSWORD, now=NOW)
        self.assertTrue(stored().startswith("scrypt$16384$8$5$"))
        admin.sign_in(self.store, "keeper", PASSWORD, now=NOW)

    def test_passwords_are_kept_only_as_hashes(self) -> None:
        code = admin.create(self.store, "keeper", PASSWORD, now=NOW)
        stored = self.store.connection.execute("SELECT password_hash, recovery_hash FROM server_admin").fetchone()
        self.assertTrue(stored[0].startswith("scrypt$"))
        self.assertNotIn(PASSWORD, stored[0])
        self.assertNotIn(code.replace("-", ""), stored[1])


class CredentialRaceTests(unittest.TestCase):
    """A credential is checked before the write, as scrypt is slow; a change in between wins (audit 2026-10-09)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "server.db"
        store = self.open()
        self.code = admin.create(store, "keeper", PASSWORD, now=NOW)

    def open(self):  # one connection for each request, as the server has
        store = open_store(self.path)
        self.addCleanup(store.close)
        return store

    def paused_after_checking(self, work):
        """Run ``work`` until it has checked a password; returns its outcome holder and a function that finishes it."""

        checked, resume, outcome = threading.Event(), threading.Event(), {}
        check = admin.check_password

        def paused(password: str, stored: str | None) -> bool:
            found = check(password, stored)
            checked.set()
            resume.wait(10)
            return found

        def run() -> None:
            store = open_store(self.path)  # closed in its own thread, as SQLite requires
            try:
                outcome["result"] = work(store)
            except admin.AdminError as exc:
                outcome["result"] = exc
            finally:
                store.close()

        with mock.patch.object(admin, "check_password", paused):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(checked.wait(10))

        def finish() -> object:
            resume.set()
            thread.join(10)
            return outcome["result"]

        return finish

    def sessions(self) -> int:
        return self.open().connection.execute("SELECT COUNT(*) FROM server_sessions").fetchone()[0]

    def test_a_sign_in_checked_before_a_recovery_starts_no_session(self) -> None:
        finish = self.paused_after_checking(lambda store: admin.sign_in(store, "keeper", PASSWORD, now=NOW))
        admin.recover(self.open(), "keeper", self.code, "a brand new password", now=NOW)
        self.assertIsInstance(finish(), admin.AdminError)
        self.assertEqual(1, self.sessions())  # the recovery's own

    def test_a_password_change_checked_before_a_recovery_is_refused(self) -> None:
        token = admin.start_session(self.open(), now=NOW)["token"]
        finish = self.paused_after_checking(
            lambda store: admin.change_password(store, token, PASSWORD, "another new password", now=NOW))
        admin.recover(self.open(), "keeper", self.code, "a brand new password", now=NOW)
        self.assertIsInstance(finish(), admin.AdminError)
        admin.sign_in(self.open(), "keeper", "a brand new password", now=NOW)

    def test_a_recovery_code_replaced_after_a_recovery_is_refused(self) -> None:
        finish = self.paused_after_checking(lambda store: admin.replace_recovery_code(store, PASSWORD, now=NOW))
        admin.recover(self.open(), "keeper", self.code, "a brand new password", now=NOW)
        self.assertIsInstance(finish(), admin.AdminError)

    def test_one_recovery_code_spent_twice_at_once_works_once(self) -> None:
        both_checked = threading.Barrier(2, timeout=10)
        hashing = admin.hash_password

        def then_wait(password: str) -> str:
            hashed = hashing(password)
            both_checked.wait()
            return hashed

        outcomes: dict[int, object] = {}

        def recover(number: int) -> None:
            store = open_store(self.path)
            try:
                outcomes[number] = admin.recover(store, "keeper", self.code, f"new password {number}!", now=NOW)
            except admin.AdminError as exc:
                outcomes[number] = exc
            finally:
                store.close()

        with mock.patch.object(admin, "hash_password", then_wait):
            threads = [threading.Thread(target=recover, args=(number,)) for number in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(1, sum(isinstance(outcome, tuple) for outcome in outcomes.values()), outcomes)
        self.assertEqual(1, sum(isinstance(outcome, admin.AdminError) for outcome in outcomes.values()), outcomes)
        self.assertEqual(1, self.sessions())


class Browser:
    """A minimal browser for the page: keeps the session cookie and sends the form token."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie: str | None = None
        self.form: str | None = None

    def request(self, method: str, path: str, body=None, *, origin: str | None = "same", form: bool = True,
                extra: dict[str, str] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        host = f"127.0.0.1:{self.port}"
        headers = {"Host": host, **(extra or {})}
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
        status, data, _ = self.browser.request("POST", "/admin/api/setup", {
            "username": "keeper", "new_password": PASSWORD, "setup_code": self.server.setup_code})
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

    def test_wrong_passwords_sent_at_once_are_checked_at_most_the_limit(self) -> None:
        self.set_up_admin()
        checked = []
        check_password = admin.check_password

        def slowly(password, stored):
            checked.append(password)
            time.sleep(0.3)  # every request arrives while the first ones are still being checked
            return check_password(password, stored)

        def guess(number: int) -> int:
            return Browser(self.server.port).request(
                "POST", "/admin/api/sign-in", {"username": "keeper", "password": f"guess {number}"})[0]

        with mock.patch.object(admin, "check_password", side_effect=slowly):
            with ThreadPoolExecutor(20) as pool:
                statuses = list(pool.map(guess, range(20)))
        self.assertLessEqual(len(checked), JOIN_FAILURES_PER_ADDRESS)
        self.assertLessEqual(statuses.count(400), JOIN_FAILURES_PER_ADDRESS)
        self.assertLessEqual(set(statuses), {400, 429, 503})

    def test_a_server_busy_checking_passwords_says_so_and_counts_no_failure(self) -> None:
        self.set_up_admin()
        taken = threading.BoundedSemaphore(1)
        taken.acquire()  # every place for a password check is in use
        with mock.patch.object(admin, "_scrypt_slots", taken), mock.patch.object(admin, "SCRYPT_WAIT_SECONDS", 0.1):
            for _ in range(JOIN_FAILURES_PER_ADDRESS):
                status, data, headers = Browser(self.server.port).request(
                    "POST", "/admin/api/sign-in", {"username": "keeper", "password": PASSWORD})
                self.assertEqual(status, 503)
                self.assertIn("busy", data["error"])
                self.assertIn("Retry-After", headers)
        status, _, _ = Browser(self.server.port).request(
            "POST", "/admin/api/sign-in", {"username": "keeper", "password": PASSWORD})
        self.assertEqual(status, 200)

    def test_an_address_forwarded_by_a_proxy_the_server_does_not_trust_is_noted_once(self) -> None:
        self.set_up_admin()
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"KNOWITALL2_HOME": home}):
            for _ in range(2):
                journal._last_problem.clear()  # the server notes it once, not merely the journal
                self.browser.request("POST", "/admin/api/sign-in", {"username": "keeper", "password": PASSWORD},
                                     extra={"X-Forwarded-For": "203.0.113.9"})
            notes = [item for item in journal.read_problems() if "KNOWITALL2_TRUSTED_PROXY" in item["message"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("X-Forwarded-For", notes[0]["message"])

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
        status, _, _ = browser.request("POST", "/admin/api/setup", {"username": "keeper", "new_password": PASSWORD,
                                                                    "setup_code": self.server.setup_code})
        self.assertEqual(status, 200)
        self.stop()
        self.start(reset="2026-10-01")
        self.assertFalse(self.server.admin_was_reset)
        self.assertFalse(Browser(self.server.port).request("GET", "/admin/api/state")[1]["setup_needed"])

    def test_after_a_reset_only_the_setup_code_from_the_servers_log_sets_up_the_admin(self) -> None:
        self.set_up_admin()
        self.stop()
        started = []

        class Started(KnowItAll2Server):
            def __init__(self, *args, **options) -> None:
                super().__init__(*args, **options)
                started.append(self)

        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(web, "KnowItAll2Server", Started), \
                mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.home), "KNOWITALL2_RESET_ADMIN": "2026-10-03"}):
            self.thread = threading.Thread(target=web.run, args=("127.0.0.1", 0), daemon=True)
            self.thread.start()
            for _ in range(200):  # what `docker compose logs` shows
                if "setup code: " in output.getvalue():
                    break
                time.sleep(0.05)
        self.server = started[0]
        code = re.search(r"setup code: ([A-Z2-9]{4}(?:-[A-Z2-9]{4}){4})", output.getvalue()).group(1)
        self.assertEqual(code, self.server.setup_code)
        port = self.server.port
        wanted = {"username": "intruder", "new_password": PASSWORD}
        refused = [
            Browser(port).request("POST", "/admin/api/setup", wanted, origin=None),
            Browser(port).request("POST", "/admin/api/setup", wanted, origin=f"http://evil.example:{port}",
                                  extra={"Host": f"evil.example:{port}"}),
            Browser(port).request("POST", "/admin/api/setup", {**wanted, "setup_code": "AAAA-BBBB-CCCC-DDDD-EEEE"}),
        ]
        self.assertEqual([status for status, _, _ in refused], [403, 403, 403])
        self.assertIn("setup code", refused[0][1]["error"])
        status, _, _ = Browser(port).request("POST", "/admin/api/setup", {
            "username": "keeper", "new_password": PASSWORD, "setup_code": code.lower().replace("-", " ")})
        self.assertEqual(status, 200)
        self.assertIsNone(self.server.setup_code)
        status, data, _ = Browser(port).request("POST", "/admin/api/setup", {**wanted, "setup_code": code})
        self.assertEqual(status, 400)
        self.assertIn("already set up", data["error"])

    def test_wrong_setup_codes_are_limited_like_wrong_passwords(self) -> None:
        # Review 2026-10-04, S-6.
        wanted = {"username": "keeper", "new_password": PASSWORD, "setup_code": "AAAA-BBBB-CCCC-DDDD-EEEE"}
        statuses = [self.browser.request("POST", "/admin/api/setup", wanted)[0] for _ in range(6)]
        self.assertEqual(statuses[:5], [403] * 5)
        self.assertEqual(statuses[5], 429)

    def test_a_server_with_an_admin_has_no_setup_code(self) -> None:
        self.set_up_admin()
        self.stop()
        self.start()
        self.assertIsNone(self.server.setup_code)
        status, _, _ = Browser(self.server.port).request(
            "POST", "/admin/api/setup", {"username": "me", "new_password": PASSWORD, "setup_code": "AAAA"})
        self.assertEqual(status, 400)

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
