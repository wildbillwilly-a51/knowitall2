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

    def test_many_new_memories_are_capped_without_advertising_the_rest(self) -> None:
        # 2026-10-13 study: "+N more" advertised 312 items behind 197 shown, and about 7% of what was shown was used.
        self.start()
        for number in range(5):
            self.remember(f"Alpha module {number} handles feature {number}.", self.alpha)
        context = self.prompt()
        self.assertEqual(chats.NEW_SHOWN, context.count("\n- "))
        self.assertNotIn("more; use recall", context)
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


class ChatLearningTestCase(ChatTestCase):
    """Memories the learner wrote from this chat, and shared ones filed under systems."""

    def learned(self, words: str, *, session: str = SESSION, provisional: bool = True, **options) -> str:
        store = Store.open(database_path())
        try:
            options.setdefault("scope", "project")
            memory = Memory(store, agent="learner")
            record = memory.remember(words, project_path=self.alpha, session=session, **options).record
            if provisional:
                store.set_tags(record.id, [*record.tags, "provisional"], now=memory.now())
            return record.id
        finally:
            store.close()

    def systems(self) -> None:
        """Alpha's own memories are filed under the NAS and the vault, so alpha uses both; and alpha is a system."""

        store = Store.open(database_path())
        try:
            memory = Memory(store, agent="cli")
            for system_id, name, aliases in (("sys-nas", "nas01", ["the NAS"]), ("sys-vault", "vault01", []),
                                             ("sys-alpha", "alpha", [])):
                store.upsert_system(system_id=system_id, name=name, area="Infrastructure", kind="host",
                                    aliases=aliases, now=memory.now())
            for system_id, words in (("sys-nas", "Alpha keeps its builds on nas01."),
                                     ("sys-vault", "Alpha reads its signing key from vault01.")):
                record = memory.remember(words, project_path=self.alpha, scope="project").record
                store.set_note(record.id, headline=words, system_id=system_id, facet="where", written_by="test",
                               now=memory.now())
        finally:
            store.close()

    def shared(self, words: str, system_id: str) -> str:
        store = Store.open(database_path())
        try:
            memory = Memory(store, agent="cli")
            record = memory.remember(words, scope="global", detect_project=False).record
            store.set_note(record.id, headline=words, system_id=system_id, facet="where", written_by="test",
                           now=memory.now())
            return record.id
        finally:
            store.close()

    def tool(self, name: str, **arguments) -> str:
        """An agent's KnowItAll2 tool call from this chat, and its answer."""

        stores: list[Store] = []

        def memory(agent):
            stores.append(Store.open(database_path()))
            return Memory(stores[-1], agent=agent)

        try:
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
                server = McpServer(memory_factory=memory, cwd=self.alpha)
                server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                               "params": {"protocolVersion": LATEST_PROTOCOL_VERSION,
                                          "clientInfo": {"name": "claude-code"}}})
                answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": name, "arguments": arguments}})
        finally:
            for store in stores:
                store.close()
        return answer["result"]["content"][0]["text"]

    def news(self, context: str) -> str:
        """The "new since" block alone."""

        if hooks.NEW_HEADER not in context:
            return ""
        return context[context.index(hooks.NEW_HEADER):].split("\n\n")[0]


class OwnLearningTests(ChatLearningTestCase):
    """2026-10-13 study: 61% of the items "new since" listed were what the learner had just written from that same
    chat, which had them already."""

    def test_the_chats_own_learning_is_never_listed_as_new(self) -> None:
        self.start()
        self.learned("Alpha's 403 comes from the automated browser.")
        self.learned("Alpha deploys from the release branch with make ship.", session="another-chat",
                     provisional=False)
        news = self.news(self.prompt())
        self.assertIn("Alpha deploys from the release branch with make ship", news)
        self.assertNotIn("403", news)

    def test_the_chat_is_told_once_apart_what_was_learned_from_it_as_not_yet_checked(self) -> None:
        self.start()
        first = self.learned("Alpha's 403 comes from the automated browser.")
        context = self.prompt()
        self.assertIn(hooks.OWN_HEADER, context)
        self.assertIn(f"- [{first}] Alpha's 403 comes from the automated browser (not yet checked)", context)
        self.assertEqual("", self.news(context))
        self.assertEqual("", self.prompt())

    def test_what_later_commits_taught_is_told_once_each_in_a_short_line(self) -> None:
        # Candidate review 2026-10-14, E-L6: only the first batch was told; what later commits taught never was,
        # so the chat could not correct a wrong one.
        self.start()
        first = self.learned("Alpha's 403 comes from the automated browser.")
        self.assertIn(hooks.OWN_HEADER, self.prompt())
        second = self.learned("Alpha's CI job needs the docker group.")
        later = self.prompt()
        self.assertNotIn(hooks.OWN_HEADER, later)
        self.assertNotIn(first, later)
        self.assertIn(f"{hooks.OWN_LATER} [{second}] Alpha's CI job needs the docker group (not yet checked)", later)
        self.assertEqual(1, later.count(hooks.OWN_LATER))
        self.assertEqual("", self.news(later))
        self.assertEqual("", self.prompt())

    def test_after_a_compaction_the_chat_hears_its_own_learning_again(self) -> None:
        # A compacted chat may have lost its early findings from its context (the study's risks).
        self.start()
        first = self.learned("Alpha's 403 comes from the automated browser.")
        self.assertIn(first, self.prompt())
        second = self.learned("Alpha's CI job needs the docker group.")
        output = json.loads(hooks.session_start(self.payload(source="compact"), start_learner=lambda: None) or "{}")
        briefing = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        told = briefing + self.prompt()
        self.assertIn(first, told)
        self.assertIn(second, told)
        self.assertEqual("", self.prompt())

    def test_a_tool_call_leaves_out_the_chats_own_learning_too(self) -> None:
        self.start()
        self.learned("Alpha's 403 comes from the automated browser.")
        self.learned("Alpha deploys from the release branch with make ship.", session="another-chat")
        answer = self.tool("recall", query="zebra")
        self.assertIn("Alpha deploys from the release branch with make ship", answer)
        self.assertNotIn("403", answer)


class TopicalNewsTests(ChatLearningTestCase):
    """Shared memories are new to a chat only when they are about the project itself or a system the chat touched
    (2026-10-13 replay, F2: 106 items listed instead of 462, 71% topical instead of 20%)."""

    def test_only_shared_memories_about_systems_the_chat_touched_are_new(self) -> None:
        self.systems()
        self.start()
        self.shared("nas01 exports /volume1/builds read-only.", "sys-nas")
        self.shared("vault01 unseals with three of five keys.", "sys-vault")
        self.shared("alpha's public mirror is read-only.", "sys-alpha")
        self.remember("Alpha's release notes go in CHANGES.md.", self.alpha)
        self.append(LogBuilder(self.alpha).tool("t1", "Bash", {"command": "ssh nas01 df -h /volume1"}, "ok"))
        news = self.news(self.prompt())
        self.assertIn("exports /volume1/builds read-only", news)        # a system the chat ran a command on
        self.assertIn("public mirror is read-only", news)               # the project's own system
        self.assertIn("release notes go in CHANGES.md", news)           # the project's own memory
        self.assertNotIn("unseals", news)                               # a system the project uses, not touched
        self.append(LogBuilder(self.alpha).tool("t2", "Bash", {"command": "vault01 status"}, "sealed"))
        self.shared("vault01 audit logs go to /var/log/vault.", "sys-vault")
        news = self.news(self.prompt())
        self.assertIn("audit logs go to /var/log/vault", news)
        self.assertIn("unseals with three of five keys", news)  # held back until the chat touched vault01

    def test_only_commands_and_the_users_words_count_as_touching_a_system(self) -> None:
        # Candidate review 2026-10-14, E-L9: every tool input counted, such as a search pattern, a web query, or a
        # subagent's task, which name systems in passing.
        self.systems()
        self.start()
        self.shared("vault01 unseals with three of five keys.", "sys-vault")
        self.append(LogBuilder(self.alpha)
                    .tool("t1", "Grep", {"pattern": "vault01", "path": str(self.alpha)}, "no matches")
                    .tool("t2", "WebSearch", {"query": "vault01 unseal keys"}, "results")
                    .tool("t3", "Agent", {"description": "Look into vault01", "prompt": "x"}, "done"))
        self.assertEqual("", self.news(self.prompt()))
        self.assertEqual("", self.news(self.tool("recall", query="zebra")))
        self.append(LogBuilder(self.alpha).tool("t4", "Bash", {"command": "vault01 status"}, "sealed"))
        self.assertIn("unseals with three of five keys", self.news(self.tool("recall", query="zebra")))

    def test_a_command_reaching_a_system_by_url_touches_it(self) -> None:
        # Candidate review 2026-10-14, E-M3: `curl http://vault01:8200/...` did not count as touching vault01.
        self.systems()
        self.start()
        self.shared("vault01 unseals with three of five keys.", "sys-vault")
        self.append(LogBuilder(self.alpha).tool("t1", "Bash", {"command": "curl -s http://vault01:8200/v1/sys/health"},
                                                "{}"))
        self.assertIn("unseals with three of five keys", self.news(self.tool("recall", query="zebra")))

    def test_the_users_words_name_a_system_the_chat_touches(self) -> None:
        self.systems()
        self.start()
        self.shared("vault01 unseals with three of five keys.", "sys-vault")
        output = hooks.prompt_submit(self.payload(hook_event_name="UserPromptSubmit",
                                                  prompt="Why is vault01 sealed again?"))
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"] if output else ""
        self.assertIn("unseals with three of five keys", self.news(context))

    def test_a_tool_call_hears_about_a_system_touched_since_the_users_message(self) -> None:
        self.systems()
        self.start()
        self.shared("nas01 exports /volume1/builds read-only.", "sys-nas")
        self.assertEqual("", self.news(self.prompt()))
        self.append(LogBuilder(self.alpha).tool("t1", "Bash", {"command": "ssh nas01 df -h /volume1"}, "ok"))
        self.assertIn("exports /volume1/builds read-only", self.news(self.tool("recall", query="zebra")))


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


class StateWriteTests(ChatTestCase):
    """Candidate review 2026-10-14, E-L5: the message hook, a KnowItAll2 tool call, and the note that news was passed
    on can save one chat's state at about the same time; what each added must survive."""

    SINCE = "2026-10-14T10:00:00Z"

    def test_two_saves_of_one_chat_keep_what_each_added(self) -> None:
        chats.start("s-chat", since=self.SINCE, project_id="prj-a", told=["k-0000000001"], transcript=None)
        first, second = chats.load("s-chat"), chats.load("s-chat")
        first["told"].append("k-0000000002")
        first["relayed"] = True
        first["offset"] = 500
        chats.save("s-chat", first)
        second["told"].append("k-0000000003")
        second["touched"] = ["sys-nas"]
        second["own_told"] = True
        chats.save("s-chat", second)   # read before the first save, written after it
        state = chats.load("s-chat")
        self.assertEqual(["k-0000000001", "k-0000000002", "k-0000000003"], state["told"])
        self.assertEqual((True, True, ["sys-nas"], 500),
                         (state["relayed"], state["own_told"], state["touched"], state["offset"]))

    def test_a_save_from_before_a_new_briefing_does_not_undo_it(self) -> None:
        chats.start("s-chat", since=self.SINCE, project_id="prj-a", told=["k-0000000001"], transcript=None)
        stale = chats.load("s-chat")
        stale["told"].append("k-0000000002")
        stale["relayed"] = True
        chats.start("s-chat", since="2026-10-14T11:00:00Z", project_id="prj-a", told=["k-0000000009"],
                    transcript=None)   # compacted meanwhile
        chats.save("s-chat", stale)
        state = chats.load("s-chat")
        self.assertEqual(("2026-10-14T11:00:00Z", ["k-0000000009"], True),
                         (state["since"], state["told"], state["relayed"]))

    def test_a_busy_lock_holds_a_save_back_only_briefly(self) -> None:
        import time

        chats.start("s-chat", since=self.SINCE, project_id="prj-a", told=[], transcript=None)
        lock = chats.folder() / "s-chat.lock"
        lock.write_text("", encoding="utf-8")   # another hook holds it, and keeps it
        state = chats.load("s-chat")
        state["told"].append("k-0000000002")
        began = time.perf_counter()
        chats.save("s-chat", state)
        self.assertLess(time.perf_counter() - began, 0.25)
        self.assertEqual(["k-0000000002"], chats.load("s-chat")["told"])
        self.assertTrue(lock.exists())   # never taken from its holder


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
