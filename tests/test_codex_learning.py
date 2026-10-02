import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository

from knowitall2 import hooks
from knowitall2.cli import main
from knowitall2.learning.dossier import build_dossiers
from knowitall2.learning.learner import learn
from knowitall2.learning.state import LearnerSettings, LearnerState
from knowitall2.learning.transcripts import codex_logs, read_codex_session, read_session, session_logs
from knowitall2.memory import Memory
from knowitall2.store import Store

SESSION = "01a0e45f-3ee3-7951-b9e3-6691dd0f0a62"


class RolloutBuilder:
    """Writes synthetic Codex rollout logs."""

    def __init__(self, cwd: Path, *, source: object = "vscode") -> None:
        self.records = [self.record("session_meta", {
            "id": SESSION, "session_id": SESSION, "cwd": str(cwd), "originator": "Codex Desktop",
            "cli_version": "0.158.0", "source": source,
        })]

    @staticmethod
    def record(kind: str, payload: dict) -> dict:
        return {"timestamp": "2026-09-28T10:00:00Z", "type": kind, "payload": payload}

    def item(self, item: dict) -> "RolloutBuilder":
        self.records.append(self.record("event_msg", {"type": "item_completed", "item": item}))
        return self

    def user(self, text: str) -> "RolloutBuilder":
        return self.item({"type": "UserMessage", "content": [{"type": "text", "text": text}]})

    def agent(self, text: str) -> "RolloutBuilder":
        return self.item({"type": "AgentMessage", "content": [{"type": "Text", "text": text}], "phase": "final"})

    def command(self, script: str, output: str, exit_code: int = 0) -> "RolloutBuilder":
        return self.item({
            "type": "CommandExecution", "command": ["C:\\Program Files\\PowerShell\\7\\pwsh.exe", "-Command", script],
            "cwd": "file:///C:/work", "aggregated_output": output, "exit_code": exit_code,
        })

    def write(self, path: Path, *, idle: bool = True, partial_tail: str | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(record) + "\n" for record in self.records)
        path.write_text(text + (partial_tail or ""), encoding="utf-8")
        if idle:
            old = time.time() - 3600
            os.utime(path, (old, old))
        return path


class CodexTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = make_repository(self.root / "work" / "homelab", "https://gitlab.example.com/me/homelab.git")
        self.codex_home = self.root / "codex"
        self.log_path = self.codex_home / "sessions" / "2026" / "09" / "28" / f"rollout-2026-09-28T10-00-00-{SESSION}.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def sample(self) -> RolloutBuilder:
        return (
            RolloutBuilder(self.project)
            .user("Please check the router. Always keep router backups in /srv/backups.")
            .user("<environment_context>\n  <cwd>C:\\work</cwd>\n</environment_context>")
            .user("<codex_delegation>\n  <source_thread_id>abc</source_thread_id>\n  The NAS is full.\n</codex_delegation>")
            .item({"type": "Reasoning", "summary": [{"text": "private reasoning"}]})
            .agent("Checking the router release.")
            .command("ssh root@10.20.30.1 cat /etc/openwrt_release", "DISTRIB_RELEASE='23.05.3'")
            .command("Get-Item missing.txt", "cannot find path", exit_code=1)
            .item({"type": "McpToolCall", "server": "knowitall2", "tool": "recall", "arguments": {"query": "router"},
                   "result": {"content": [{"type": "text", "text": "own lookup"}]}})
            .item({"type": "McpToolCall", "server": "docs", "tool": "search", "arguments": {"q": "dnsmasq"},
                   "result": {"content": [{"type": "text", "text": "dnsmasq manual"}]}})
            .item({"type": "FileChange", "changes": {"C:\\work\\notes.md": {"type": "update", "content": "file body"}}})
            .item({"type": "WebSearch", "query": "openwrt dnsmasq restart"})
            .item({"type": "SubAgentActivity", "kind": "interacted", "agent_thread_id": "x"})
            .agent("DNS is fixed; dnsmasq needed a restart.")
        )


class CodexReaderTests(CodexTestCase):
    def test_keeps_real_events_and_drops_noise(self) -> None:
        session, _ = read_codex_session(self.sample().write(self.log_path))
        self.assertEqual(("codex", SESSION, str(self.project)), (session.agent, session.session_id, session.cwd))
        kinds = [(event.kind, event.tool) for event in session.events]
        self.assertEqual([
            ("user", None), ("agent_report", None), ("assistant", None), ("tool", "shell"), ("tool", "shell"),
            ("tool", "docs.search"), ("tool", "FileChange"), ("tool", "WebSearch"), ("assistant", None),
        ], kinds)
        commands = [event for event in session.events if event.tool == "shell"]
        self.assertEqual("ssh root@10.20.30.1 cat /etc/openwrt_release", commands[0].text)
        self.assertEqual("(exit code 1)\ncannot find path", commands[1].output)
        joined = " ".join(event.text + (event.output or "") for event in session.events)
        for dropped in ("private reasoning", "environment_context", "own lookup", "file body"):
            self.assertNotIn(dropped, joined)
        self.assertIn("update C:\\work\\notes.md", joined)

    def test_delegated_messages_are_never_the_users_words(self) -> None:
        session, _ = read_codex_session(self.sample().write(self.log_path))
        [dossier] = build_dossiers(session)
        self.assertEqual(["Please check the router. Always keep router backups in /srv/backups."], dossier.user_texts)
        self.assertIn("(codex)", dossier.text)

    def test_helper_and_non_interactive_sessions_yield_nothing(self) -> None:
        for source in ({"subagent": {"thread_spawn": {}}}, "exec"):
            with self.subTest(source=source):
                log = RolloutBuilder(self.project, source=source).user("hello there").agent("hi").write(self.log_path)
                session, offset = read_codex_session(log)
                self.assertEqual([], session.events)
                self.assertEqual(log.stat().st_size, offset)

    def test_resumes_from_an_offset_and_leaves_a_partial_line(self) -> None:
        builder = RolloutBuilder(self.project).user("first message").agent("first answer")
        log = builder.write(self.log_path, partial_tail='{"type": "event_msg", "pay')
        session, offset = read_codex_session(log)
        self.assertEqual(2, len(session.events))
        self.assertLess(offset, log.stat().st_size)
        builder.user("second message").write(log)
        resumed, _ = read_codex_session(log, start=offset)
        self.assertEqual(["second message"], [event.text for event in resumed.events])
        self.assertEqual(str(self.project), resumed.cwd)

    def test_logs_of_both_agents_are_found_newest_first(self) -> None:
        claude_log = self.root / "claude" / "projects" / "C--work" / "session.jsonl"
        claude_log.parent.mkdir(parents=True)
        claude_log.write_text("", encoding="utf-8")
        self.sample().write(self.log_path)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home), "CLAUDE_CONFIG_DIR": str(self.root / "claude")}):
            self.assertEqual([self.log_path], codex_logs())
            self.assertEqual([claude_log, self.log_path], session_logs())
            self.assertEqual("codex", read_session(self.log_path)[0].agent)


class CodexLearningTests(CodexTestCase):
    def test_the_learner_learns_from_codex_sessions(self) -> None:
        self.sample().write(self.log_path)
        store = Store.in_memory()
        memory = Memory(store, agent="learner", clock=Clock())

        class Extractor:
            name = "fake"
            dossiers = []

            def extract(self, dossier):
                self.dossiers.append(dossier)
                return [
                    {"text": "The homelab router runs OpenWrt 23.05.3.", "kind": "fact", "subjects": ["router"],
                     "scope": "global", "evidence": "DISTRIB_RELEASE='23.05.3'", "relation": "new", "known_id": ""},
                    {"text": "Always keep router backups in /srv/backups.", "kind": "rule", "subjects": ["router"],
                     "scope": "global", "evidence": "always keep router backups in /srv/backups",
                     "relation": "new", "known_id": ""},
                    {"text": "The NAS is full and needs attention.", "kind": "rule", "subjects": ["NAS"],
                     "scope": "global", "evidence": "The NAS is full.", "relation": "new", "known_id": ""},
                ]

        try:
            report = learn(
                logs=[self.log_path], state=LearnerState(self.root / "state.json"),
                settings=LearnerSettings(enabled=True), memory_factory=lambda: memory, extractor=Extractor(),
                dry_run=False,
            )
            self.assertEqual({"saved": 3}, dict(report.outcomes))
            kinds = {row.text: (row.kind, row.verification) for row in store.list_active(project_id=None, scope="global", limit=10)}
            self.assertEqual(("fact", "observed"), kinds["The homelab router runs OpenWrt 23.05.3."])
            self.assertEqual(("rule", "user_stated"), kinds["Always keep router backups in /srv/backups."])
            # Not the user's words: kept as a plain note, and the user is not asked about it.
            self.assertEqual(("note", "unverified"), kinds["The NAS is full and needs attention."])
            self.assertEqual(0, store.count_open_questions())
        finally:
            store.close()

    def test_start_from_now_skips_history_but_learns_what_comes_next(self) -> None:
        builder = self.sample()
        builder.write(self.log_path)
        environment = {
            "KNOWITALL2_HOME": str(self.root / "data"), "CODEX_HOME": str(self.codex_home),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
        }
        with mock.patch.dict(os.environ, environment):
            code, output = self.run_cli("learn", "--start-from-now", "codex")
            self.assertEqual(0, code)
            self.assertIn("Marked 1 of 1 codex session log(s) as already read", output)
            self.assertIn("ready to learn from: 0", self.run_cli("learn", "--dry-run")[1])
            builder.user("New question about the NAS.").agent("The NAS pool is healthy.").write(self.log_path)
            self.assertIn("ready to learn from: 1", self.run_cli("learn", "--dry-run")[1])
            code, output = self.run_cli("learn", "--show", SESSION[:8])
            self.assertIn("[user] Please check the router.", output)

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = main(list(arguments))
        return code, output.getvalue()


class CodexHookTests(unittest.TestCase):
    def test_the_codex_session_start_hook_returns_the_briefing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": temporary}):
                store = Store.open(Path(temporary) / "knowitall2.db")
                try:
                    Memory(store, agent="cli").remember("Always tag releases.", kind="rule", source="user")
                finally:
                    store.close()
                output = io.StringIO()
                payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": temporary})
                with redirect_stdout(output), mock.patch.object(hooks, "maybe_start_learner", return_value=False):
                    self.assertEqual(0, hooks.main(["codex", "session-start"], stdin=payload))
                result = json.loads(output.getvalue())
                self.assertEqual("SessionStart", result["hookSpecificOutput"]["hookEventName"])
                self.assertIn("Always tag releases.", result["hookSpecificOutput"]["additionalContext"])
                self.assertEqual(0, hooks.main(["other-agent", "session-start"], stdin=payload))


if __name__ == "__main__":
    unittest.main()
