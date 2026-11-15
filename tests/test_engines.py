"""Learning on the engine that did the work, with no engine required (plan step 2 of the joint review,
2026-11-13; the user's direction of 2026-11-15: each engine learns in its own right, failover both ways)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import test_codex_learning as codex_fixtures
import test_learning as fixtures
from knowitall2.learning import engines as routing
from knowitall2.learning.engines import CLAUDE, CODEX, Engines, qualification_id
from knowitall2.learning.extractor import ExtractionError
from knowitall2.learning.learner import learn
from knowitall2.learning.state import LearnerSettings, LearnerState, load_settings

MODEL = "gpt-test"
LISTED = [{"slug": MODEL}, {"slug": "gpt-other"}]


class Engine(fixtures.FakeEngine):
    """A stand-in engine with a name and a model, noting which chats it read."""

    def __init__(self, name, results=(), *, model="m", **options) -> None:
        super().__init__(results, **options)
        self.name, self.model, self.effort = name, model, "medium"

    def sessions(self) -> list[str]:
        return [dossier.session_id for dossier in self.dossiers]


def settings(**overrides) -> LearnerSettings:
    values = dict(enabled=True, route_by_source=True, codex_model=MODEL, codex_effort="medium")
    return LearnerSettings(**{**values, **overrides})


def router(chosen: LearnerSettings, primary, built: dict, *, paused=None, replaced=None, builds=None) -> Engines:
    def build(backend, model, effort):
        if builds is not None:
            builds.append((backend, model, effort))
        if backend == CODEX and replaced and built.get(backend) is not None:
            # command.build_engine found the model no longer listed and built Codex's everyday model instead.
            return Engine(CODEX, model=replaced)
        return built.get(backend)

    return Engines(chosen, primary, build=build, paused=lambda backend: (paused or {}).get(backend))


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.claude, self.codex = Engine(CLAUDE, model="sonnet"), Engine(CODEX, model=MODEL)

    def route(self, agent, chosen=None, *, primary=None, built=None, **options):
        built = {CLAUDE: self.claude, CODEX: self.codex} if built is None else built
        return router(chosen or settings(), primary or self.claude, built, **options).for_source(agent)

    def test_each_chat_learns_on_its_own_engine(self) -> None:
        codex, claude = self.route("codex"), self.route("claude-code")
        self.assertEqual((self.codex, CODEX, "routed to the chat's engine"), (codex.engine, codex.preferred, codex.reason))
        self.assertEqual((self.claude, CLAUDE), (claude.engine, claude.preferred))

    def test_codex_learns_without_having_to_match_claude(self) -> None:
        # No qualification against Sonnet (the user, 2026-11-15): an older configuration's empty one changes nothing.
        route = self.route("codex", settings(codex_qualified=""))
        self.assertIs(self.codex, route.engine)

    def test_routing_is_on_by_default(self) -> None:
        self.assertTrue(LearnerSettings().route_by_source)
        self.assertEqual(("gpt-6.1-sol", "medium"), (LearnerSettings().codex_model, LearnerSettings().codex_effort))

    def test_with_routing_off_every_chat_learns_on_the_primary_and_its_source_is_still_recorded(self) -> None:
        route = self.route("codex", settings(route_by_source=False))
        self.assertEqual((self.claude, CODEX), (route.engine, route.preferred))
        self.assertIn("routing off", route.reason)

    def test_an_unknown_source_is_not_guessed(self) -> None:
        route = self.route("something-else")
        self.assertEqual((self.claude, None), (route.engine, route.preferred))
        self.assertIn("source unknown", route.reason)

    def test_a_computer_with_only_codex_learns_every_chat_on_codex(self) -> None:
        for agent in ("claude-code", "codex", "something-else"):
            route = self.route(agent, primary=self.codex, built={CLAUDE: None, CODEX: self.codex})
            self.assertIs(self.codex, route.engine, agent)

    def test_a_computer_with_only_claude_learns_every_chat_on_claude(self) -> None:
        for agent in ("claude-code", "codex", "something-else"):
            route = self.route(agent, built={CLAUDE: self.claude, CODEX: None})
            self.assertIs(self.claude, route.engine, agent)
        self.assertIn("Codex is not installed", self.route("codex", built={CLAUDE: self.claude, CODEX: None}).reason)

    def test_a_retired_codex_model_gives_way_to_codexs_everyday_model(self) -> None:
        builds = []
        route = self.route("codex", replaced="gpt-everyday", builds=builds)
        self.assertEqual((CODEX, "gpt-everyday"), (route.engine.name, route.engine.model))
        self.assertIn(f"Codex no longer lists {MODEL}", route.reason)
        self.assertEqual([(CODEX, MODEL, "medium")], builds)   # built once: no second look at the model list

    def test_with_routing_off_the_primary_keeps_its_own_model(self) -> None:
        # Codex's review of 372b11b: a primary chosen with --model must not be swapped for claude_model.
        primary = Engine(CLAUDE, model="opus")
        for agent in ("codex", "claude-code", "something-else"):
            route = self.route(agent, settings(route_by_source=False), primary=primary)
            self.assertIs(primary, route.engine, agent)

    def test_a_paused_engine_is_not_even_built(self) -> None:
        builds = []
        route = self.route("codex", paused={CODEX: {"retry_at": "later"}}, builds=builds)
        self.assertIs(self.claude, route.engine)
        self.assertNotIn(CODEX, [backend for backend, _model, _effort in builds])

    def test_retry_backend_tries_the_paused_engine_instead_of_routing_around_it(self) -> None:
        primary = Engine(CLAUDE, model="sonnet")
        primary.retry_backend_once = True
        engines = router(settings(), primary, {CLAUDE: self.claude, CODEX: self.codex},
                         paused={CLAUDE: {"retry_at": "later"}, CODEX: {"retry_at": "later"}})
        self.assertIs(primary, engines.for_source("claude-code").engine)
        route = engines.for_source("codex")
        self.assertIs(self.codex, route.engine)
        self.assertTrue(self.codex.retry_backend_once)
        # One engine's failed retry does not stop the run before the other's retry.
        engines.block(CLAUDE, "still limited")
        self.assertFalse(engines.all_blocked())
        engines.block(CODEX, "still limited")
        self.assertTrue(engines.all_blocked())

    def test_checking_whether_every_engine_is_blocked_builds_no_paused_engine(self) -> None:
        builds = []
        engines = router(settings(), self.claude, {CLAUDE: self.claude, CODEX: self.codex},
                         paused={CODEX: {"retry_at": "later"}}, builds=builds)
        engines.block(CLAUDE, "usage limit")
        self.assertTrue(engines.all_blocked())
        self.assertNotIn(CODEX, [backend for backend, _model, _effort in builds])

    def test_a_blocked_or_paused_engine_hands_its_chats_to_the_other(self) -> None:
        engines = router(settings(), self.claude, {CLAUDE: self.claude, CODEX: self.codex})
        engines.for_source("codex")
        engines.block(CODEX, "usage limit")
        route = engines.for_source("codex")
        self.assertIs(self.claude, route.engine)
        self.assertIn("usage limit", route.reason)
        self.assertIn("other engine", route.reason)
        self.assertFalse(engines.all_blocked())
        engines.block(CLAUDE, "signed out")
        self.assertIsNone(engines.for_source("codex").engine)
        self.assertTrue(engines.all_blocked())
        paused = self.route("claude-code", paused={CLAUDE: {"retry_at": "2026-11-16T00:00:00+00:00"}})
        self.assertIs(self.codex, paused.engine)
        self.assertIn("paused until 2026-11-16", paused.reason)

    def test_upkeep_runs_on_the_primary_or_else_the_other_engine(self) -> None:
        engines = router(settings(), self.claude, {CLAUDE: self.claude, CODEX: self.codex})
        self.assertIs(self.claude, engines.upkeep())
        engines.block(CLAUDE, "usage limit")
        self.assertIs(self.codex, engines.upkeep())
        engines.block(CODEX, "usage limit")
        self.assertIsNone(engines.upkeep())
        only_codex = router(settings(), self.codex, {CLAUDE: None, CODEX: self.codex})
        self.assertIs(self.codex, only_codex.upkeep())

    def test_the_primary_is_reused_when_it_is_the_same_engine_and_model(self) -> None:
        built = []
        primary = Engine(CLAUDE, model="sonnet")
        engines = Engines(settings(), primary, build=lambda *a: built.append(a) or self.claude,
                          paused=lambda backend: None)
        self.assertIs(primary, engines.engine(CLAUDE))
        self.assertEqual([], built)

    def test_the_qualification_id_still_names_the_current_prompts(self) -> None:
        # Kept for the evaluation harness (scripts/qualify_learning.py), which labels what it measured.
        qualified = qualification_id(MODEL, "medium")
        with mock.patch.object(routing, "prompts_fingerprint", return_value="changed"):
            self.assertNotEqual(qualified, qualification_id(MODEL, "medium"))


class PrimaryEngineTests(unittest.TestCase):
    def test_the_other_engine_is_primary_when_the_configured_one_is_not_installed(self) -> None:
        from knowitall2.learning import command

        codex = Engine(CODEX, model=MODEL)
        built = {"codex-cli": codex, "claude-cli": None}
        with mock.patch.object(command, "build_engine", side_effect=lambda backend, model: built[backend]):
            self.assertIs(codex, command.primary_engine(settings(backend="claude-cli")))
            built["codex-cli"] = None
            self.assertIsNone(command.primary_engine(settings(backend="claude-cli")))


    def test_a_retired_codex_model_is_replaced_after_one_look_at_the_catalog(self) -> None:
        from knowitall2.learning import command, extractor

        reads = []
        catalog = [{"slug": "gpt-new", "description": "Everyday model", "priority": 1}]

        def listed(executable, **options):
            reads.append(executable)
            return catalog

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"KNOWITALL2_HOME": home}),                 mock.patch.object(command, "codex_listed_models", side_effect=listed),                 mock.patch.object(extractor, "codex_listed_models", side_effect=listed):
            self.assertEqual("gpt-new", command._listed_codex_model(Path("codex.exe"), "gpt-retired"))
        self.assertEqual(1, len(reads))


class SettingsTests(unittest.TestCase):
    def test_routing_settings_are_read_by_type(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"KNOWITALL2_HOME": home}):
            path = Path(home) / "config.json"
            path.write_text(json.dumps({"learning": {"enabled": True, "backend": "claude-cli", "model": "sonnet",
                                                     "codex_qualified": "gpt-test@medium#old"}}), encoding="utf-8")
            old = load_settings()
            self.assertEqual((True, "sonnet", "gpt-6.1-sol", "medium"), (old.route_by_source, old.claude_model,
                                                                         old.codex_model, old.codex_effort))
            path.write_text(json.dumps({"learning": {"route_by_source": "false", "codex_model": 5,
                                                     "codex_qualified": ["x"]}}), encoding="utf-8")
            edited = load_settings()
            # A hand-edited "false" is not a boolean: the default stays, as for any mistyped value.
            self.assertEqual((True, "gpt-6.1-sol", ""), (edited.route_by_source, edited.codex_model,
                                                         edited.codex_qualified))


class RoutingCommandTests(unittest.TestCase):
    def test_the_user_turns_routing_off_and_names_the_codex_model(self) -> None:
        import argparse
        import contextlib
        import io
        from knowitall2.learning import command

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"KNOWITALL2_HOME": home}):
            arguments = argparse.Namespace(route_by_source="off", codex_model=None, codex_effort=None)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                command._set_routing(load_settings(), arguments)
            self.assertFalse(load_settings().route_by_source)
            self.assertIn("other engine when it cannot run", output.getvalue())
            arguments = argparse.Namespace(route_by_source="on", codex_model=MODEL, codex_effort="high")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                command._set_routing(load_settings(), arguments)
            saved = load_settings()
            self.assertEqual((True, MODEL, "high"), (saved.route_by_source, saved.codex_model, saved.codex_effort))
            self.assertIn(f"({MODEL} at high effort)", output.getvalue())


class RoutedLearningTests(unittest.TestCase):
    """A Claude Code chat and a Codex chat learned in one run."""

    def setUp(self) -> None:
        self.fixture = fixtures.TwoPassTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        t = self.fixture
        t.sample_log().write(t.log_path)
        self.codex_log = t.root / "codex" / "sessions" / "2026" / "09" / "28" / (
            f"rollout-2026-09-28T10-00-00-{codex_fixtures.SESSION}.jsonl")
        codex_fixtures.RolloutBuilder(t.project).user("Please check the router.").command(
            "ssh root@10.20.30.1 cat /etc/openwrt_release", "DISTRIB_RELEASE='23.05.3'").agent(
            "The router runs OpenWrt 23.05.3.").write(self.codex_log)

    def run_both(self, claude, codex, *, logs=None, chosen=None):
        t = self.fixture
        engines = router(chosen or settings(), claude, {CLAUDE: claude, CODEX: codex})
        report = learn(logs=logs or [t.log_path, self.codex_log], state=t.state, settings=t.settings,
                       memory_factory=lambda: t.memory, extractor=claude, dry_run=False, quick=True,
                       right_away=True, engines=engines)
        return report

    def test_each_chat_is_learned_by_its_own_engine(self) -> None:
        claude, codex = Engine(CLAUDE, [[]]), Engine(CODEX, [[]])
        report = self.run_both(claude, codex)
        self.assertEqual([fixtures.SESSION], claude.sessions())
        self.assertEqual([codex_fixtures.SESSION], codex.sessions())
        self.assertEqual(Counter({CLAUDE: 1, CODEX: 1}), report.engines)

    def test_without_codex_both_chats_learn_on_claude(self) -> None:
        t = self.fixture
        claude = Engine(CLAUDE, [[], []])
        engines = router(settings(), claude, {CLAUDE: claude, CODEX: None})
        learn(logs=[t.log_path, self.codex_log], state=t.state, settings=t.settings, memory_factory=lambda: t.memory,
              extractor=claude, dry_run=False, quick=True, right_away=True, engines=engines)
        self.assertEqual({fixtures.SESSION, codex_fixtures.SESSION}, set(claude.sessions()))

    def test_after_codex_hits_its_limit_the_next_codex_chat_learns_on_claude(self) -> None:
        t = self.fixture
        quota = ExtractionError("You've hit your weekly limit", blocking=True, category="quota")
        second = self.codex_log.with_name(self.codex_log.name.replace("10-00-00", "11-00-00").replace(
            codex_fixtures.SESSION, "01a00000-0000-7000-8000-000000000002"))
        second.write_bytes(self.codex_log.read_bytes().replace(codex_fixtures.SESSION.encode(),
                                                               b"01a00000-0000-7000-8000-000000000002"))
        claude, codex = Engine(CLAUDE, [[]]), Engine(CODEX, [quota])
        report = self.run_both(claude, codex, logs=[self.codex_log, second])
        self.assertIsNone(report.blocked)
        self.assertEqual(1, report.deferred)                       # the chat that met the limit keeps its place
        self.assertEqual(["01a00000-0000-7000-8000-000000000002"], claude.sessions())   # the next one moved

    def test_an_unusable_engine_stops_only_its_own_chats_in_either_order(self) -> None:
        quota = ExtractionError("You've hit your weekly limit", blocking=True, category="quota")
        for order in ("codex first", "claude first"):
            with self.subTest(order):
                self.setUp()
                t = self.fixture
                claude, codex = Engine(CLAUDE, [[t.DNSMASQ]]), Engine(CODEX, [quota])
                logs = [self.codex_log, t.log_path] if order == "codex first" else [t.log_path, self.codex_log]
                report = self.run_both(claude, codex, logs=logs)
                self.assertIsNone(report.blocked)
                self.assertIn(CODEX, report.engine_blocks)
                self.assertEqual([fixtures.SESSION], claude.sessions())
                self.assertEqual(1, len(t.provisional()))
                self.assertTrue(t.state.entry(self.codex_log).get("pending_reviews"))   # its place is kept

    def test_a_primary_that_fails_first_does_not_stop_a_healthy_engine_not_yet_used(self) -> None:
        t = self.fixture
        quota = ExtractionError("You've hit your weekly limit", blocking=True, category="quota")
        claude, codex = Engine(CLAUDE, [quota]), Engine(CODEX, [[]])
        report = self.run_both(claude, codex, logs=[t.log_path, self.codex_log])
        self.assertIsNone(report.blocked)
        self.assertEqual([codex_fixtures.SESSION], codex.sessions())
        self.assertEqual(1, report.deferred)   # the Claude chat waits for its engine

    def test_when_every_engine_is_unusable_the_run_stops(self) -> None:
        quota = ExtractionError("You've hit your weekly limit", blocking=True, category="quota")
        report = self.run_both(Engine(CLAUDE, [quota]), Engine(CODEX, [quota]))
        self.assertIsNotNone(report.blocked)

    def test_a_chat_quick_learned_on_one_engine_is_finalized_on_its_own(self) -> None:
        t = self.fixture
        claude = Engine(CLAUDE, finals=[{"reviews": [fixtures.review("p1", "confirm")], "memories": []}])
        codex = Engine(CODEX, [[t.DNSMASQ]])
        # The quick pass ran on Codex (routing off, Codex primary); the final pass on Claude, the chat's own.
        learn(logs=[t.log_path], state=t.state, settings=t.settings, memory_factory=lambda: t.memory,
              extractor=codex, dry_run=False, quick=True, right_away=True,
              engines=router(settings(route_by_source=False), codex, {CLAUDE: claude, CODEX: codex}))
        [record] = t.provisional()
        fixtures.quiet_longer(t.log_path)
        report = learn(logs=[t.log_path], state=t.state, settings=t.settings, memory_factory=lambda: t.memory,
                       extractor=codex, dry_run=False,
                       engines=router(settings(), codex, {CLAUDE: claude, CODEX: codex}))
        self.assertEqual([], report.failed)
        self.assertEqual(Counter({CLAUDE: report.calls}), report.engines)
        self.assertEqual([], t.provisional())
        self.assertEqual("active", t.store.get(record.id).status)


class FilingEngineTests(unittest.TestCase):
    def test_filing_after_requests_uses_the_engine_that_learned_them(self) -> None:
        from knowitall2.learning.command import _attached_engine

        claude, codex = Engine(CLAUDE), Engine(CODEX, model=MODEL)
        engines = router(settings(), claude, {CLAUDE: claude, CODEX: codex})
        engines.for_source("codex")
        self.assertIs(codex, _attached_engine(engines, Counter({CODEX: 2})))
        self.assertIs(claude, _attached_engine(engines, Counter({CLAUDE: 1})))
        self.assertIsNone(_attached_engine(engines, Counter({CLAUDE: 1, CODEX: 1})))   # the primary files
        engines.block(CODEX, "usage limit")
        self.assertIsNone(_attached_engine(engines, Counter({CODEX: 2})))
        self.assertIsNone(_attached_engine(None, Counter({CODEX: 2})))


if __name__ == "__main__":
    unittest.main()
