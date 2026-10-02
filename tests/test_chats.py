import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import make_repository
from test_learning import SESSION, LogBuilder, text

from knowitall2 import chats, hooks
from knowitall2.mcp_server import LATEST_PROTOCOL_VERSION, McpServer
from knowitall2.memory import Memory
from knowitall2.paths import database_path
from knowitall2.store import Store


class ChatTestCase(unittest.TestCase):
    """A long-lived chat hears what KnowItAll2 learned after its briefing, and about projects it moves to."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.alpha = make_repository(self.root / "work" / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.beta = make_repository(self.root / "work" / "beta", "https://gitlab.example.com/team/beta.git")
        self.environment = mock.patch.dict(os.environ, {
            "KNOWITALL2_HOME": str(self.root / "data"), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
        })
        self.environment.start()
        self.log_path = self.root / "claude" / "projects" / "C--work-alpha" / f"{SESSION}.jsonl"
        LogBuilder(self.alpha).user("Let's work on alpha.").write(self.log_path, idle=False)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def remember(self, words: str, project: Path, **options) -> str:
        store = Store.open(database_path())
        try:
            options.setdefault("scope", "project")
            return Memory(store, agent="cli").remember(words, project_path=project, **options).record.id
        finally:
            store.close()

    def payload(self, **extra) -> str:
        return json.dumps({"session_id": SESSION, "transcript_path": str(self.log_path), "cwd": str(self.alpha),
                           **extra})

    def start(self) -> str:
        output = json.loads(hooks.session_start(self.payload(source="startup"), start_learner=lambda: None) or "{}")
        return output.get("hookSpecificOutput", {}).get("additionalContext", "")

    def prompt(self, agent: str = "claude-code") -> str:
        output = hooks.prompt_submit(self.payload(hook_event_name="UserPromptSubmit", prompt="next"), agent=agent)
        return json.loads(output)["hookSpecificOutput"]["additionalContext"] if output else ""

    def append(self, builder: LogBuilder) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(record) + "\n" for record in builder.records))


class NewSinceTests(ChatTestCase):
    def test_a_memory_added_after_the_briefing_is_told_once_at_the_next_message(self) -> None:
        known = self.remember("Alpha builds with make.", self.alpha)
        self.assertIn(known, self.start())
        self.assertEqual("", self.prompt())
        added = self.remember("Alpha deploys from the release branch with make ship.", self.alpha, kind="procedure")
        context = self.prompt()
        self.assertTrue(context.startswith(hooks.BACKGROUND))
        self.assertIn(hooks.NEW_HEADER, context)
        self.assertIn("Alpha deploys from the release branch with make ship", context)
        self.assertNotIn(known, context)
        self.assertNotIn(added, context)  # headlines, not ids: the agent recalls the details
        self.assertEqual("", self.prompt())

    def test_what_the_chat_already_saw_is_not_repeated(self) -> None:
        self.start()
        saved = self.remember("Alpha's staging host is stage1.", self.alpha)
        self.append(LogBuilder(self.alpha).tool("t1", "mcp__other__save", {"text": "x"}, f"Saved [{saved}] (fact)."))
        self.assertEqual("", self.prompt())

    def test_many_new_memories_are_capped(self) -> None:
        self.start()
        for number in range(5):
            self.remember(f"Alpha module {number} handles feature {number}.", self.alpha)
        context = self.prompt()
        self.assertEqual(chats.NEW_SHOWN, context.count("\n- "))
        self.assertIn("(+2 more; use recall to search.)", context)
        self.assertEqual("", self.prompt())

    def test_current_state_notes_are_left_out(self) -> None:
        self.start()
        store = Store.open(database_path())
        try:
            memory = Memory(store, agent="cli")
            record = memory.remember("Alpha's test suite has 212 passing tests.", project_path=self.alpha,
                                     scope="project").record
            store.upsert_system(system_id="sys-alpha", name="alpha", area="Projects and practices", kind="project",
                                aliases=[], now=memory.now())
            store.set_note(record.id, headline="Alpha has 212 passing tests", system_id="sys-alpha", facet="status",
                           written_by="test", now=memory.now())
        finally:
            store.close()
        self.assertEqual("", self.prompt())

    def test_codex_and_apps_that_show_hook_messages_get_it_too(self) -> None:
        self.start()
        self.remember("Alpha's docs live in docs/.", self.alpha)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            self.assertIn("Alpha's docs live in docs/", self.prompt())
        self.remember("Alpha's CI runs on every push.", self.alpha)
        self.assertIn("Alpha's CI runs on every push", self.prompt(agent="codex"))

    def test_a_new_briefing_starts_the_chat_afresh(self) -> None:
        self.start()
        self.remember("Alpha uses SQLite.", self.alpha)
        self.assertIn("Alpha uses SQLite", self.start())  # resumed or compacted: it is in the new briefing
        self.assertEqual("", self.prompt())


class ToolNewsTests(ChatTestCase):
    """An agent's KnowItAll2 tool calls hear what is new to the chat, without waiting for the user's message."""

    def setUp(self) -> None:
        super().setUp()
        self.stores: list[Store] = []

    def tearDown(self) -> None:
        for store in self.stores:
            store.close()
        super().tearDown()

    def server(self) -> McpServer:
        def memory(agent):
            self.stores.append(Store.open(database_path()))
            return Memory(self.stores[-1], agent=agent)

        server = McpServer(memory_factory=memory, cwd=self.alpha)
        server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                       "params": {"protocolVersion": LATEST_PROTOCOL_VERSION, "clientInfo": {"name": "claude-code"}}})
        return server

    def call(self, server: McpServer, name: str, **arguments) -> str:
        answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})
        return answer["result"]["content"][0]["text"]

    def test_another_chats_decision_reaches_the_next_tool_call_once(self) -> None:
        # The live case: the user's decision saved in a Codex chat reached a Claude chat only at the next message.
        self.start()
        self.remember("Alpha builds with make.", self.alpha)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
            server = self.server()
            self.remember("The user excludes a paid API for alpha.", self.alpha, kind="decision", source="user")
            first = self.call(server, "remember", text="Alpha's 403 comes from the automated browser.",
                              scope="project", project_path=str(self.alpha))
            own = chats.RECORD_ID.findall(first)[0]
            self.assertIn(hooks.NEW_HEADER, first)
            self.assertIn("The user excludes a paid API for alpha", first)
            self.assertNotIn("Alpha's 403 comes from", first[first.index(hooks.NEW_HEADER):])  # its own save
            second = self.call(server, "recall", query="alpha builds make", project_path=str(self.alpha))
            self.assertIn("Alpha builds with make", second)
            self.assertNotIn(hooks.NEW_HEADER, second)  # told once, and what the answer shows is not repeated
            self.assertNotIn("excludes a paid API", self.prompt())  # nor again at the user's next message
            self.assertNotIn(own, self.prompt())
            self.assertNotIn(hooks.NEW_HEADER, self.call(server, "briefing", project_path=str(self.alpha)))

    def test_without_a_session_id_the_chat_is_found_by_its_log(self) -> None:
        self.start()
        self.remember("Alpha's staging host is stage1.", self.alpha)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertIn("Alpha's staging host is stage1", self.call(self.server(), "recall", query="zebra"))

    def test_memories_the_chat_already_saw_in_its_log_are_not_repeated(self) -> None:
        self.start()
        saved = self.remember("Alpha's docs live in docs/.", self.alpha)
        self.append(LogBuilder(self.alpha).tool("t1", "mcp__knowitall2__recall", {"query": "docs"}, f"[{saved}] docs"))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
            self.assertNotIn(hooks.NEW_HEADER, self.call(self.server(), "recall", query="zebra"))

    def test_a_chat_without_state_gets_nothing_extra(self) -> None:
        self.remember("Alpha's CI runs on every push.", self.alpha)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "unknown-session"}):
            self.assertNotIn(hooks.NEW_HEADER, self.call(self.server(), "recall", query="alpha ci"))


class MovedProjectTests(ChatTestCase):
    def work_in_beta(self, calls: int) -> None:
        builder = LogBuilder(self.alpha)
        for number in range(calls):
            builder.tool(f"b{number}", "Read", {"file_path": str(self.beta / f"file{number}.py")}, "content")
        self.append(builder)

    def test_working_in_another_project_adds_its_table_of_contents_once(self) -> None:
        self.remember("Beta ships with make release.", self.beta, kind="procedure")
        self.remember("Beta's rule: never force-push.", self.beta, kind="rule", source="user")
        self.start()
        self.work_in_beta(1)
        self.assertEqual("", self.prompt())  # one look is not working there
        self.work_in_beta(chats.WORK_CALLS)
        context = self.prompt()
        self.assertIn("project beta, where this chat is now working", context)
        self.assertIn("Beta ships with make release.", context)
        self.assertIn("never force-push", context)
        self.work_in_beta(chats.WORK_CALLS)
        self.assertEqual("", self.prompt())
        self.remember("Beta's release notes go in CHANGES.md.", self.beta)
        self.assertIn("Beta's release notes go in CHANGES.md", self.prompt())

    def test_a_chat_from_before_this_version_gets_its_table_of_contents_once(self) -> None:
        self.remember("Alpha builds with make.", self.alpha, kind="procedure")
        context = self.prompt()
        self.assertIn("table of contents for project alpha, which this chat's briefing did not have", context)
        self.assertIn("Alpha builds with make.", context)
        self.assertEqual("", self.prompt())

    def test_the_deepest_folder_wins_and_paths_match_however_they_are_written(self) -> None:
        known = [("prj-work", "work", os.path.normcase(str(self.root / "work"))),
                 ("prj-alpha", "alpha", os.path.normcase(str(self.alpha)))]
        inside = str(self.alpha / "src" / "main.py")
        self.assertEqual({"prj-alpha": 1}, chats.projects_worked_in([f"cat {inside}"], known))
        self.assertEqual({"prj-work": 1}, chats.projects_worked_in([str(self.root / "work" / "notes.txt")], known))
        self.assertEqual({"prj-work": 1}, chats.projects_worked_in([str(self.alpha) + "-old/file"], known))
        if os.name == "nt":
            drive, rest = inside[0].lower(), inside[3:].replace("\\", "/")
            for written in (inside.replace("\\", "/"), f"/{drive}/{rest}", f"/mnt/{drive}/{rest}",
                            json.dumps({"path": inside})):
                with self.subTest(written):
                    self.assertEqual({"prj-alpha": 1}, chats.projects_worked_in([written], known))


if __name__ == "__main__":
    unittest.main()
