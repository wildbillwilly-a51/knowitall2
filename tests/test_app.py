import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _support import SystemProxy, make_repository

from knowitall2 import app as app_module
from knowitall2 import journal
from knowitall2.app import api, server
from knowitall2.memory import Memory
from knowitall2.paths import database_path
from knowitall2.store import Store

KEY = "test-key-0123456789"


class AppTestCase(unittest.TestCase):
    """A real app server on a free port, over a private data home."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {
            "KNOWITALL2_HOME": str(self.root / "home"), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
        })
        self.environment.start()
        api._agents.clear()
        api._engines.clear()
        self.engines = mock.patch.object(api, "engine_path", return_value=None)
        self.engines.start()
        self.server = server.AppServer(KEY, watch_interval=0.05)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.thread.join(timeout=5)
        self.engines.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def request(self, method: str, path: str, *, body=None, headers=None, key: str | None = KEY,
                host: str | None = None) -> tuple[int, dict, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        sent = {"Host": host or f"127.0.0.1:{self.server.port}"}
        if key is not None:
            sent[server.KEY_HEADER] = key
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        try:
            connection.request(method, path, body=payload, headers=sent)
            response = connection.getresponse()
            data = response.read()
            received = {name.lower(): value for name, value in response.getheaders()}
        finally:
            connection.close()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = {"_text": data.decode("utf-8", errors="replace")}
        return response.status, parsed, received

    def memory(self) -> tuple[Store, Memory]:
        store = Store.open(database_path())
        return store, Memory(store, agent="codex")


class SecurityTests(AppTestCase):
    def test_the_page_is_served_with_a_strict_policy(self) -> None:
        status, body, headers = self.request("GET", "/", key=None)
        self.assertEqual(200, status)
        self.assertIn("<title>KnowItAll2</title>", body["_text"])
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual("nosniff", headers["x-content-type-options"])
        status, _, headers = self.request("GET", "/app.js", key=None)
        self.assertEqual((200, "text/javascript; charset=utf-8"), (status, headers["content-type"]))

    def test_the_api_needs_the_key_and_this_host(self) -> None:
        self.assertEqual(401, self.request("GET", "/api/overview", key=None)[0])
        self.assertEqual(401, self.request("GET", "/api/overview", key="wrong")[0])
        self.assertEqual(403, self.request("GET", "/api/overview", host="evil.example.com")[0])
        self.assertEqual(403, self.request("GET", "/", key=None, host=f"attacker.test:{self.server.port}")[0])
        self.assertEqual(200, self.request("GET", "/api/ping")[0])

    def test_changes_must_be_json_from_this_origin_and_cross_origin_is_refused(self) -> None:
        self.assertEqual(403, self.request("OPTIONS", "/api/doctor")[0])
        status, _, _ = self.request("POST", "/api/doctor", body={}, headers={"Origin": "https://evil.example.com"})
        self.assertEqual(403, status)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            connection.request("POST", "/api/doctor", body="x=1", headers={
                "Host": f"127.0.0.1:{self.server.port}", server.KEY_HEADER: KEY,
                "Content-Type": "application/x-www-form-urlencoded",
            })
            self.assertEqual(415, connection.getresponse().status)
        finally:
            connection.close()
        self.assertEqual(405, self.request("POST", "/api/overview", body={})[0])
        self.assertEqual(404, self.request("GET", "/api/nothing-here")[0])


class LifetimeTests(AppTestCase):
    def test_goodbye_stops_the_server_unless_another_window_is_open(self) -> None:
        with mock.patch.object(server, "GOODBYE_GRACE_SECONDS", 0.2):
            self.request("GET", "/api/ping", headers={server.WINDOW_HEADER: "first"})
            self.request("GET", "/api/ping", headers={server.WINDOW_HEADER: "second"})
            self.server.goodbye("first")
            time.sleep(0.5)
            self.assertTrue(self.thread.is_alive())
            self.server.goodbye("second")
            self.thread.join(timeout=5)
            self.assertFalse(self.thread.is_alive())

    def test_a_goodbye_beacon_carries_the_key_in_its_body(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            connection.request("POST", "/api/bye", body="wrong window", headers={
                "Host": f"127.0.0.1:{self.server.port}", "Content-Type": "text/plain"})
            self.assertEqual(401, connection.getresponse().status)
        finally:
            connection.close()
        with mock.patch.object(self.server, "goodbye") as goodbye:
            connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
            try:
                connection.request("POST", "/api/bye", body=f"{KEY} window-1", headers={
                    "Host": f"127.0.0.1:{self.server.port}", "Content-Type": "text/plain"})
                self.assertEqual(200, connection.getresponse().status)
            finally:
                connection.close()
        goodbye.assert_called_once_with("window-1")

    def test_a_window_that_goes_away_mid_answer_is_not_a_problem(self) -> None:
        journal._last_problem.clear()
        with mock.patch.object(server.AppHandler, "_send_json", side_effect=ConnectionAbortedError("gone")):
            with self.assertRaises(Exception):
                self.request("GET", "/api/ping")
        self.assertEqual([], journal.read_problems())

    def test_a_silent_page_stops_the_server(self) -> None:
        self.server.idle_seconds = 0.2
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def test_an_open_app_is_found_again(self) -> None:
        self.assertIsNone(app_module.running_instance())
        app_module.instance_path().parent.mkdir(parents=True, exist_ok=True)
        app_module.instance_path().write_text(json.dumps({"port": self.server.port, "key": KEY}), encoding="utf-8")
        self.assertEqual({"port": self.server.port, "key": KEY}, app_module.running_instance())
        app_module.instance_path().write_text(json.dumps({"port": self.server.port, "key": "stale"}), encoding="utf-8")
        self.assertIsNone(app_module.running_instance())

    def test_an_open_app_is_found_with_a_system_proxy_which_never_sees_its_key(self) -> None:
        app_module.instance_path().parent.mkdir(parents=True, exist_ok=True)
        app_module.instance_path().write_text(json.dumps({"port": self.server.port, "key": KEY}), encoding="utf-8")
        with SystemProxy() as proxy:
            self.assertEqual({"port": self.server.port, "key": KEY}, app_module.running_instance())
        self.assertEqual([], proxy.seen)


class ScreenTests(AppTestCase):
    def test_the_overview_reports_health_use_and_learning(self) -> None:
        store, memory = self.memory()
        try:
            project = make_repository(self.root / "alpha", "https://gitlab.example.com/team/alpha.git")
            memory.remember("Always tag releases.", kind="rule", source="user")
            memory.remember("The NAS is nas01.", subjects=["NAS"])
            memory.briefing(project_path=project)
            memory.recall("nas", project_path=project)
            memory.recall("printer", project_path=project)
            journal.record(store, "candidate", "A learned fact.", outcome="saved", run="r-1")
            journal.record(store, "candidate", "An invented fact.", outcome="rejected", run="r-1",
                           details={"reason": "evidence not in the session"})
        finally:
            store.close()
        status, data, _ = self.request("GET", "/api/overview?tz=0")
        self.assertEqual(200, status)
        today = data["periods"]["today"]
        self.assertEqual((1, 2, 1, 1, 1, 2), (today["briefings"], today["recalls"], today["recall_hits"],
                                              today["learned"], today["rejected"], today["saved"]))
        self.assertEqual(2, data["memories"]["active"])
        self.assertEqual("attention", data["health"]["level"])
        self.assertIn("Learning is off", " ".join(item["text"] for item in data["health"]["items"]))
        self.assertEqual(14, len(data["daily"]))
        self.assertEqual(1, data["daily"][-1]["user"])
        self.assertFalse(data["learning"]["enabled"])

    def test_activity_pages_back_and_filters(self) -> None:
        store, memory = self.memory()
        try:
            for number in range(5):
                memory.remember(f"Host number {number} is server{number}.")
            journal.record(store, "candidate", "From one run.", outcome="saved", run="r-9")
        finally:
            store.close()
        status, first, _ = self.request("GET", "/api/activity?limit=3")
        self.assertEqual((200, 3), (status, len(first["events"])))
        self.assertIsNotNone(first["next"])
        _, older, _ = self.request("GET", f"/api/activity?limit=3&before={first['next']}")
        self.assertEqual(2, len(older["events"]))
        self.assertNotIn("candidate", [event["kind"] for event in first["events"] + older["events"]])
        _, run, _ = self.request("GET", "/api/activity?run=r-9")
        self.assertEqual(["From one run."], [event["summary"] for event in run["events"]])
        self.assertEqual(400, self.request("GET", "/api/activity?group=everything")[0])

    def test_problems_are_listed_and_a_failure_is_recorded_as_one(self) -> None:
        journal._last_problem.clear()
        journal.problem("learning", "learning stopped: Not logged in")
        _, data, _ = self.request("GET", "/api/problems")
        self.assertEqual(["learning stopped: Not logged in"], [item["message"] for item in data["problems"]])
        with mock.patch("knowitall2.app.api.Store.open", side_effect=RuntimeError("disk gone")):
            status, body, _ = self.request("GET", "/api/overview")
        self.assertEqual(500, status)
        self.assertIn("disk gone", body["error"])
        self.assertIn("GET /api/overview failed", journal.read_problems()[0]["message"])


class MemoryScreenTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = make_repository(self.root / "alpha", "https://gitlab.example.com/team/alpha.git")
        store, memory = self.memory()
        try:
            self.nas = memory.remember("The NAS is nas01.", subjects=["NAS"]).record
            self.rule = memory.remember("Always tag releases.", kind="rule", source="user").record
            self.decision = memory.remember("Releases are cut from main.", kind="decision",
                                            project_path=self.project).record
        finally:
            store.close()

    def ids(self, path: str) -> list[str]:
        status, data, _ = self.request("GET", path)
        self.assertEqual(200, status, data)
        return [item["id"] for item in data["items"]]

    def test_odd_page_numbers_are_a_plain_refusal_or_an_empty_page(self) -> None:
        self.assertEqual([], self.ids("/api/memories?offset=1000000"))
        self.assertEqual(3, len(self.ids("/api/memories?offset=-5")))
        for path in ("/api/memories?offset=" + "1" + "0" * 30, "/api/memories?offset=1e9", "/api/activity?before="
                     + "9" * 25, "/api/activity?limit=" + "9" * 25):
            with self.subTest(path=path):
                status, data, _ = self.request("GET", path)
                self.assertEqual(400, status, data)
                self.assertRegex(data["error"], "must be a whole number|is too large")

    def test_memories_are_searched_and_filtered_without_counting_as_use(self) -> None:
        self.assertEqual(3, len(self.ids("/api/memories")))
        self.assertEqual([self.nas.id], self.ids("/api/memories?q=nas"))
        self.assertEqual([self.rule.id], self.ids("/api/memories?kind=rule"))
        self.assertEqual([self.rule.id], self.ids("/api/memories?origin=user"))
        self.assertEqual({self.nas.id, self.rule.id}, set(self.ids("/api/memories?project=global")))
        self.assertEqual([self.decision.id], self.ids(f"/api/memories?project={self.decision.project_id}"))
        self.assertEqual(3, len(self.ids("/api/memories?sort=unused")))
        self.assertEqual([], self.ids("/api/memories?status=inactive"))
        self.assertEqual(400, self.request("GET", "/api/memories?kind=gossip")[0])
        _, known, _ = self.request("GET", "/api/projects")
        self.assertEqual((["alpha"], 2), ([item["name"] for item in known["projects"] if item["memories"]], known["global"]))
        store = Store.open(database_path())
        try:
            self.assertEqual(0, store.usage_counts(since="2000-01-01T00:00:00Z").get("recall", {"count": 0})["count"])
        finally:
            store.close()

    def test_a_memory_shows_its_use_and_what_replaced_it(self) -> None:
        store, memory = self.memory()
        try:
            memory.recall("nas", project_path=self.project)
        finally:
            store.close()
        _, detail, _ = self.request("GET", f"/api/memories/{self.nas.id}")
        self.assertEqual((1, {"recall": 1}), (detail["memory"]["recall_count"], detail["memory"]["uses"]))
        status, result, _ = self.request("POST", f"/api/memories/{self.nas.id}/correct", body={"text": "The NAS is nas02."})
        self.assertEqual(200, status, result)
        _, newer, _ = self.request("GET", f"/api/memories/{result['id']}")
        self.assertEqual(("user_stated", [self.nas.id]), (newer["memory"]["verification"],
                                                          [item["id"] for item in newer["memory"]["replaced"]]))
        self.assertEqual(("remember", "updated", "app"), (newer["events"][0]["kind"], newer["events"][0]["outcome"],
                                                          newer["events"][0]["agent"]))
        _, older, _ = self.request("GET", f"/api/memories/{self.nas.id}")
        self.assertEqual(("superseded", result["id"]), (older["memory"]["status"], older["replaced_by"]["id"]))
        self.assertEqual([self.nas.id], self.ids("/api/memories?status=inactive&q=nas01"))

    def test_actions_follow_the_same_rules_as_everywhere_else(self) -> None:
        path = f"/api/memories/{self.nas.id}"
        self.assertEqual(200, self.request("POST", path + "/confirm", body={})[0])
        self.assertEqual("user_stated", self.request("GET", path)[1]["memory"]["verification"])
        status, body, _ = self.request("POST", path + "/correct", body={"text": "The NAS password is hunter22 now."})
        self.assertEqual(400, status)
        self.assertIn("secret", body["error"])
        self.assertEqual(400, self.request("POST", path + "/correct", body={"text": "The NAS is nas01."})[0])
        self.assertEqual(200, self.request("POST", path + "/forget", body={"reason": "moved"})[0])
        self.assertEqual(("retired", "moved"), tuple(self.request("GET", path)[1]["memory"][key]
                                                     for key in ("status", "retired_reason")))
        self.assertEqual(400, self.request("POST", path + "/correct", body={"text": "The NAS is nas03."})[0])
        self.assertEqual(200, self.request("POST", path + "/restore", body={})[0])
        self.assertEqual("active", self.request("GET", path)[1]["memory"]["status"])
        self.assertEqual(404, self.request("GET", "/api/memories/k-0000000000")[0])
        self.assertEqual(404, self.request("POST", "/api/memories/k-0000000000/forget", body={})[0])


    def test_a_change_in_the_app_rewrites_the_knowledge_files(self) -> None:
        with mock.patch("knowitall2.known.nudge") as refresh:
            self.assertEqual(200, self.request("POST", f"/api/memories/{self.nas.id}/confirm", body={})[0])
            deadline = time.monotonic() + 5  # checked after the answer is sent
            while not refresh.called and time.monotonic() < deadline:
                time.sleep(0.01)
        refresh.assert_called_once_with()


class LearningScreenTests(AppTestCase):
    def test_learning_shows_runs_and_why_candidates_were_not_kept(self) -> None:
        store = Store.open(database_path())
        try:
            journal.record(store, "learning", "Learned from 1 session", outcome="ok", run="r-1", details={
                "sessions": [{"session": "s-1", "dossiers": 1, "characters": 10}], "calls": 2,
                "usage": {"input_tokens": 1000, "output_tokens": 100},
                "maintenance": {"calls": 1, "usage": {"input_tokens": 500}},
            })
            journal.record(store, "candidate", "Kept.", outcome="saved", run="r-1")
            for text in ("Invented.", "Also invented."):
                journal.record(store, "candidate", text, outcome="rejected", run="r-1",
                               details={"reason": "evidence not in the session"})
        finally:
            store.close()
        with mock.patch.object(api, "_waiting", return_value={"logs": 3, "active": 1, "ready": 2, "characters": 10}):
            status, data, _ = self.request("GET", "/api/learning")
        self.assertEqual(200, status, data)
        week = data["week"]
        self.assertEqual((1, 1, 2, 1, 1500, 100), (week["runs"], week["sessions"], week["calls"], week["review_calls"],
                                                   week["tokens"]["input_tokens"], week["tokens"]["output_tokens"]))
        self.assertEqual(({"saved": 1, "rejected": 2}, {"evidence not in the session": 2}),
                         (week["candidates"], week["reasons"]))
        self.assertEqual(("r-1", 2, False), (data["runs"][0]["run_id"], data["waiting"]["ready"],
                                             data["settings"]["enabled"]))

    def test_settings_are_checked_and_turning_learning_on_is_recorded(self) -> None:
        with mock.patch("knowitall2.learning.command.find_claude_cli", return_value=Path("claude.exe")), \
                mock.patch("knowitall2.learning.command.find_codex_cli", return_value=None):
            status, data, _ = self.request("POST", "/api/learning/settings", body={"enabled": True, "max_calls_per_day": 30})
        self.assertEqual(200, status, data)
        self.assertEqual((True, "claude-cli", "sonnet", 30), tuple(data["settings"][key] for key in (
            "enabled", "backend", "model", "max_calls_per_day")))
        for bad in ({"max_calls_per_run": 0}, {"backend": "other-engine"}, {"model": "bad model!"}, {"enabled": "yes"},
                    {"idle_minutes": True}):
            with self.subTest(body=bad):
                self.assertEqual(400, self.request("POST", "/api/learning/settings", body=bad)[0])
        self.assertEqual(200, self.request("POST", "/api/learning/settings", body={"enabled": False})[0])
        store = Store.open(database_path())
        try:
            recorded = [(event["summary"], event["agent"]) for event in reversed(store.events(kinds=["settings"]))]
        finally:
            store.close()
        self.assertEqual([("Learning turned on: engine claude-cli, model sonnet", "app"),
                          ("Learning limits changed: max calls per day 30", "app"),
                          ("Learning turned off", "app")], recorded)

    def test_run_now_starts_one_background_run(self) -> None:
        from knowitall2.learning.state import LearnerSettings, save_settings

        self.assertEqual(400, self.request("POST", "/api/learning/run", body={})[0])
        save_settings(LearnerSettings(enabled=True))
        with mock.patch("knowitall2.hooks.maybe_start_learner", return_value=True) as start:
            status, data, _ = self.request("POST", "/api/learning/run", body={})
        self.assertEqual((200, True), (status, data["started"]))
        start.assert_called_once_with(force=True)
        with mock.patch.object(api.RunLock, "busy", return_value=True):
            self.assertFalse(self.request("POST", "/api/learning/run", body={})[1]["started"])

    def test_learning_shows_what_it_just_learned_and_learns_anyway(self) -> None:
        from knowitall2.learning import moments
        from knowitall2.learning.state import LearnerSettings, save_settings

        save_settings(LearnerSettings(enabled=True))
        request = {"transcript": str(self.root / "s.jsonl"), "session_id": "s", "agent": "claude-code",
                   "cwd": str(self.root / "work" / "homelab"), "reason": "commit", "detail": "1a2b3c4"}
        waiting = moments.add_news({"reason": "commit", "detail": "1a2b3c4", "status": "limit", "limit": 60,
                                    "folders": [request["cwd"]], "requests": [request]})
        moments.add_news({"reason": "session end", "status": "done", "folders": [request["cwd"]],
                          "saved": [{"id": "k-1", "text": "The router runs OpenWrt."}]})
        status, data, _ = self.request("GET", "/api/learning")
        self.assertEqual(200, status)
        latest, older = data["news"]
        self.assertEqual(("when the session ended", ["homelab"], False),
                         (latest["why"], latest["folders"], latest["can_learn_anyway"]))
        self.assertEqual(("after your commit 1a2b3c4", True), (older["why"], older["can_learn_anyway"]))
        self.assertNotIn("requests", older)  # session files stay out of the app
        self.assertEqual("done", self.request("GET", "/api/overview")[1]["learning"]["latest"]["status"])
        with mock.patch("knowitall2.hooks.maybe_start_learner", return_value=True) as start:
            status, data, _ = self.request("POST", f"/api/learning/news/{waiting['id']}/anyway", body={})
        self.assertEqual((200, "Learning it now."), (status, data["message"]))
        start.assert_called_once_with(requests=True)
        [again] = moments.pending_requests()
        self.assertEqual((True, "commit", request["transcript"]), (again["ignore_limit"], again["reason"],
                                                                   again["transcript"]))
        self.assertEqual(404, self.request("POST", "/api/learning/news/n-00000000/anyway", body={})[0])


class RouteTests(AppTestCase):
    def test_every_route_is_found_for_its_own_method(self) -> None:
        # Review 2026-10-04, U-H1: a GET listed first for a path made its POST answer 405.
        import re

        from knowitall2.app.server import _find_route

        for method, pattern, handler in api.ROUTES:
            if re.search(r"[\\^$*+?()\[\]{}|]", pattern):
                continue  # only plain paths: several routes share one
            with self.subTest(route=f"{method} {pattern}"):
                found, _ = _find_route(method, pattern)
                self.assertIs(found, handler)

    def test_learn_these_starts_catching_up(self) -> None:
        from knowitall2.learning import catchup

        with mock.patch.object(catchup, "catch_up", return_value=2) as started:
            status, data, _ = self.request("POST", "/api/learning/catch-up", body={"folder": "C:/work/homelab"})
        self.assertEqual((200, 2), (status, data.get("marked")), data)
        started.assert_called_once()
        self.assertEqual(405, self.request("POST", "/api/learning", body={})[0])  # a path known only for GET

    def test_catching_up_while_learning_runs_says_so_plainly(self) -> None:
        from knowitall2.learning import catchup
        from knowitall2.learning.state import LockBusy

        with mock.patch.object(catchup, "catch_up", side_effect=LockBusy("busy")):
            status, data, _ = self.request("POST", "/api/learning/catch-up", body={"folder": "C:/work/homelab"})
        self.assertEqual(400, status, data)
        self.assertIn("in progress", data["error"])


class QuestionScreenTests(AppTestCase):
    def test_questions_show_their_memories_and_answers_are_the_users_word(self) -> None:
        store, memory = self.memory()
        try:
            stated = memory.remember("Backups run at 02:00.", source="user").record
            newer = memory.remember("Backups run at 04:00.").record
            from knowitall2 import review

            review.ask_conflict(memory, stated, newer)
            [question] = store.open_questions(limit=5)
        finally:
            store.close()
        _, data, _ = self.request("GET", "/api/questions")
        # Learning is off in this home, so there is no background review: the question is for the user now.
        [shown] = data["for_you"]
        self.assertEqual([stated.id, newer.id], [item["id"] for item in shown["memories"]])
        self.assertEqual(("The newer one is right", "keep_both"), (shown["labels"][0]["label"], shown["not_sure"]))
        self.assertEqual(400, self.request("POST", f"/api/questions/{question['id']}/answer", body={"choice": "maybe"})[0])
        status, result, _ = self.request("POST", f"/api/questions/{question['id']}/answer", body={"choice": "not_sure"})
        self.assertEqual((200, 0), (status, result["remaining"]))
        _, after, _ = self.request("GET", "/api/questions")
        self.assertEqual(([], []), (after["for_you"], after["in_progress"]))
        self.assertEqual(("Both are right", "you"), (after["answered"][0]["answer_label"],
                                                     after["answered"][0]["settled_by"]))
        self.assertEqual(("active", "active"), tuple(self.request("GET", f"/api/memories/{item}")[1]["memory"]["status"]
                                                     for item in (stated.id, newer.id)))
        self.assertEqual(404, self.request("POST", "/api/questions/q-missing/answer", body={"choice": "use_new"})[0])


class KnowledgeScreenTests(AppTestCase):
    def test_systems_are_listed_by_area_and_the_user_can_fill_in_or_ask(self) -> None:
        from knowitall2 import catalog

        store, memory = self.memory()
        try:
            record = memory.remember("vCenter vc01 is at 10.9.15.16.", source="observed").record
            system_id = catalog.system_id_for("vCenter")
            store.upsert_system(system_id=system_id, name="vCenter", area="Servers and virtual machines",
                                kind="service", aliases=["vc01"], now="2026-09-28T12:00:00Z")
            store.set_note(record.id, headline="Where vCenter is", system_id=system_id, facet="where",
                           written_by="catalog", now="2026-09-28T12:00:00Z")
        finally:
            store.close()
        _, listed, _ = self.request("GET", "/api/knowledge")
        [area] = listed["areas"]
        self.assertEqual(("Servers and virtual machines", "vCenter", "Partly known"),
                         (area["area"], area["systems"][0]["name"], area["systems"][0]["status"]["label"]))
        _, shown, _ = self.request("GET", f"/api/knowledge/{system_id}")
        self.assertEqual(["How agents reach it", "Where the sign-in is kept"], [item["label"] for item in shown["missing"]])
        status, told, _ = self.request("POST", f"/api/knowledge/{system_id}/tell",
                                       body={"facet": "signin", "text": "The vCenter sign-in is in Vaultwarden item vcenter."})
        self.assertEqual(200, status, told)
        status, asked, _ = self.request("POST", f"/api/knowledge/{system_id}/ask", body={"facet": "access"})
        self.assertEqual(200, status, asked)
        _, shown, _ = self.request("GET", f"/api/knowledge/{system_id}")
        self.assertEqual(["How agents reach it"], [item["label"] for item in shown["missing"]])
        self.assertEqual(["open"], [item["status"] for item in shown["requests"]])
        _, page, _ = self.request("GET", f"/api/memories?system={system_id}")
        self.assertEqual({"Where vCenter is", "The vCenter sign-in is in Vaultwarden item vcenter."},
                         {item["headline"] for item in page["items"]})
        self.assertEqual(400, self.request("POST", f"/api/knowledge/{system_id}/tell", body={"facet": "x", "text": "y"})[0])
        self.assertEqual(404, self.request("GET", "/api/knowledge/sys-000000000000")[0])
        # One agent looks for everything missing, right away.
        self.assertEqual(("Claude Code", None), (shown["search"]["engine"], shown["finding"]))
        self.assertIsInstance(shown["search"]["folder"], str)
        with mock.patch("knowitall2.learning.finder.start",
                        return_value={"started": True, "message": "An agent is looking now."}) as start:
            status, found, _ = self.request("POST", f"/api/knowledge/{system_id}/find-out", body={})
        self.assertEqual((200, True), (status, found["started"]))
        start.assert_called_once_with(system_id)
        self.assertEqual(404, self.request("POST", "/api/knowledge/sys-000000000000/find-out", body={})[0])


class ShortcutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "home"),
                                                        "XDG_DATA_HOME": str(self.root / "share")})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_the_launcher_opens_the_app_from_this_installation(self) -> None:
        from knowitall2.app import shortcut

        text = shortcut.render_launcher()
        self.assertIn("from knowitall2.app import run", text)
        self.assertIn(repr(str(Path(__file__).resolve().parents[1] / "src")), text)
        compile(text, "launcher", "exec")

    def windows(self, *, packaged: bool):
        """Fake Windows' folders and shortcut writer; ``packaged`` hides Start menu files as a package would."""

        from knowitall2.app import shortcut

        self.folders = (self.root / "Programs", self.root / "Desktop")
        self.scripts = []

        def powershell(script, failure=None):
            self.scripts.append(script)
            if "GetFolderPath" in script:
                return "\n".join(str(folder) for folder in self.folders) + "\n"
            Path(script.split("CreateShortcut('")[1].split("')")[0]).write_bytes(b"lnk")
            return ""

        self.packaged = packaged
        known = {shortcut.FOLDERID_PROGRAMS: str(self.folders[0]), shortcut.FOLDERID_DESKTOP: str(self.folders[1])}
        patches = [
            mock.patch.object(shortcut, "_known_folder", side_effect=known.get),
            mock.patch.object(shortcut, "_powershell", side_effect=powershell),
            mock.patch.object(shortcut, "in_package_storage",
                              side_effect=lambda path: self.packaged and path.parent == self.folders[0]),
            mock.patch("knowitall2.app.icon.ico_bytes", return_value=b"ico"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return shortcut

    def exists(self, place: int) -> bool:
        return (self.folders[place] / "KnowItAll2.lnk").is_file()

    def test_the_launcher_opens_the_app_from_this_installation(self) -> None:
        from knowitall2.app import shortcut

        text = shortcut.render_launcher()
        self.assertIn("from knowitall2.app import run", text)
        self.assertIn(repr(str(Path(__file__).resolve().parents[1] / "src")), text)
        compile(text, "launcher", "exec")

    @unittest.skipUnless(os.name == "nt", "Windows shortcuts")
    def test_setup_installs_the_start_menu_shortcut_once(self) -> None:
        shortcut = self.windows(packaged=False)
        changes = shortcut.install()
        self.assertTrue(self.exists(0))
        self.assertFalse(self.exists(1))
        self.assertIn("added KnowItAll2 to the Start menu", " ".join(changes))
        self.assertIn("pythonw.exe", self.scripts[-1])
        self.assertIn(str(shortcut.launcher_path()), self.scripts[-1])
        self.assertEqual([], shortcut.install())
        self.assertEqual((True, "in the Start menu"), shortcut.describe()[:2])
        (self.folders[0] / "KnowItAll2.lnk").unlink()
        self.assertEqual([], shortcut.install())  # deleted by the user: not made again
        self.assertFalse(self.exists(0))

    @unittest.skipUnless(os.name == "nt", "Windows shortcuts")
    def test_inside_a_packaged_app_the_desktop_stands_in_until_the_app_first_runs(self) -> None:
        shortcut = self.windows(packaged=True)
        changes = shortcut.install()
        self.assertEqual((False, True), (self.exists(0), self.exists(1)))
        self.assertIn("adds itself to the Start menu", changes[-1])
        self.assertEqual([], shortcut.install())
        self.assertTrue(shortcut.describe()[0])
        self.assertEqual([], shortcut.install(from_app=True))  # still inside the package
        self.packaged = False  # opened from the desktop, through Explorer
        self.assertEqual(["added KnowItAll2 to the Start menu"], shortcut.install(from_app=True))
        self.assertTrue(self.exists(0))
        self.assertEqual([], shortcut.install(from_app=True))
        self.assertEqual((True, "in the Start menu"), shortcut.describe()[:2])

    @unittest.skipUnless(os.name == "nt", "Windows shortcuts")
    def test_a_lost_desktop_stand_in_is_reported_and_removal_is_respected(self) -> None:
        shortcut = self.windows(packaged=True)
        shortcut.install()
        (self.folders[1] / "KnowItAll2.lnk").unlink()
        ok, _, fix = shortcut.describe()
        self.assertFalse(ok)
        self.assertIn("your own terminal", fix)
        with self.assertRaises(shortcut.ShortcutError):
            shortcut.create()
        self.assertIn("did not add the Start menu shortcut yet", shortcut.create(desktop=True)[-1])
        self.assertEqual(3, len(shortcut.remove()))
        self.assertEqual([], shortcut.install())
        self.assertEqual([], shortcut.install(from_app=True))
        self.assertFalse(self.exists(1) or shortcut.launcher_path().exists())
        self.assertEqual((True, "removed at your request"), shortcut.describe()[:2])
        self.packaged = False
        shortcut.create()
        self.assertTrue(self.exists(0))

    @unittest.skipUnless(os.name == "nt", "Windows shortcuts")
    def test_the_folders_come_from_windows_itself_or_else_from_powershell(self) -> None:
        shortcut = self.windows(packaged=False)
        self.assertEqual([folder / "KnowItAll2.lnk" for folder in self.folders], shortcut._windows_shortcuts())
        self.assertFalse(any("GetFolderPath" in script for script in self.scripts))
        self.folders = (self.root / "Programs José", self.root / "山田" / "Desktop")
        with mock.patch.object(shortcut, "_known_folder", return_value=None):
            self.assertEqual([folder / "KnowItAll2.lnk" for folder in self.folders], shortcut._windows_shortcuts())

    def test_powershell_text_quotes_every_kind_of_single_quote(self) -> None:
        from knowitall2.app import shortcut

        self.assertEqual("'O''Brien'", shortcut.quoted("O'Brien"))
        self.assertEqual("'O\u2019\u2019Brien \u2018\u2018a\u201a\u201a\u201b\u201b'",
                         shortcut.quoted("O\u2019Brien \u2018a\u201a\u201b"))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell")
    def test_powershell_round_trips_any_name(self) -> None:
        # Read-only: PowerShell only echoes the text, and looks up where the desktop is.
        from knowitall2.app import shortcut

        for name in ("José", "山田", "O\u2019Brien", "C:/Users/O'Brien \u2018x\u2019/山田 José"):
            with self.subTest(name=name):
                self.assertEqual(name, shortcut._powershell("Write-Output " + shortcut.quoted(name)).strip())
        self.assertEqual(shortcut._powershell("[Environment]::GetFolderPath('Desktop')").strip(),
                         shortcut._known_folder(shortcut.FOLDERID_DESKTOP))

    def test_the_linux_menu_entry_runs_any_path(self) -> None:
        from knowitall2.app import shortcut

        self.assertEqual(r'"/opt/py 3/python3" "/home/o\\"b/\\$x/\\`y\\`/a\\\\b/100%%/app.pyw"',
                         shortcut.desktop_exec(["/opt/py 3/python3", '/home/o"b/$x/`y`/a\\b/100%/app.pyw']))

    @unittest.skipIf(os.name == "nt", "Linux menu entries")
    def test_a_linux_menu_entry_is_installed_once_and_removed(self) -> None:
        from knowitall2.app import shortcut

        entry = self.root / "share" / "applications" / "knowitall2.desktop"
        self.assertIn("applications menu", " ".join(shortcut.install()))
        self.assertIn("Name=KnowItAll2", entry.read_text(encoding="utf-8"))
        self.assertEqual([], shortcut.install())
        self.assertTrue(shortcut.describe()[0])
        shortcut.remove()
        self.assertFalse(entry.exists())
        self.assertEqual([], shortcut.install())

    def test_setup_never_fails_because_of_the_shortcut(self) -> None:
        from knowitall2 import cli

        with mock.patch("knowitall2.app.shortcut.install", side_effect=OSError("disk full")):
            [note] = cli._install_app_shortcut()
        self.assertIn("knowitall2 app --shortcut", note)

    def test_the_app_records_finishing_its_start_menu_shortcut(self) -> None:
        with mock.patch("knowitall2.app.shortcut.install", return_value=["added KnowItAll2 to the Start menu"]):
            app_module._finish_shortcut()
        store = Store.open(database_path())
        try:
            [event] = store.events(kinds=["settings"])
        finally:
            store.close()
        self.assertEqual(("Added KnowItAll2 to the Start menu", "app"), (event["summary"], event["agent"]))


if __name__ == "__main__":
    unittest.main()
