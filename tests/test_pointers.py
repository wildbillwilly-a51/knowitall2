"""Pointers judged by who wrote a name and how it was used, and folders built from what a project depends on.

0.11.1, "deliver right" (docs/research/2026-10-13-root-causes/pointers.md, "Recommended design", items 1, 2,
and 4): of 144 pointers in two days, 35% named a system the work never touched, set off by branch names,
KnowItAll2's relayed news, monitor events, the agent's own tools, and names most memories use in passing; and
the folders, chosen from where memories were filed, missed half of the systems projects name in their own
documents and reach by command.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import make_repository
from test_known import KnownTestCase

from knowitall2 import catalog, known
from knowitall2.memory import Memory
from knowitall2.store import Store


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def listed(root: Path) -> list[str]:
    return sorted(path.name for path in (root / known.FOLDER).iterdir())


class MentionTests(KnownTestCase):
    """What part a name plays in the words, and who wrote them."""

    def setUp(self) -> None:
        super().setUp()
        for number in range(3):
            saved = self.remember(f"Note {number} about the NAS: sudo -u hermes, step {number}.", scope="project")
            self.file(saved.id, "Synology", aliases=["nas-file1"])
            tool = self.remember(f"PowerShell note {number}: quote the arguments.", scope="project")
            self.file(tool.id, "PowerShell", aliases=["pwsh"], kind="software")
            own = self.remember(f"KnowItAll2 note {number}: run its update script.", scope="project")
            self.file(own.id, "KnowItAll2", aliases=["KIA2"], kind="software")
        known.worked_in(self.project)
        known.refresh(self.connection)
        self.folder = self.project / known.FOLDER

    def pointed(self, texts=(), own_words=(), folder: Path | None = None) -> list[str]:
        return [target for target, _ in known.find_pointers(self.connection, list(texts), folder or self.folder, [],
                                                            own_words=list(own_words))]

    def test_a_name_that_starts_a_branch_ref_or_path_or_sits_in_a_url_brings_no_pointer(self) -> None:
        # Branch names such as codex/visual-suite-repair set off 6 of the 19 pointers to names used as identifiers.
        for words in ("pushed it to origin as synology/fix-backups", "the files are on nas-file1/files/cisco ap",
                      "see https://wiki.example.test/hosts?name=nas-file1 for the list"):
            with self.subTest(words):
                self.assertEqual([], self.pointed([words]))
                self.assertEqual([], self.pointed(own_words=[words]))
        self.assertEqual(["synology.md"], self.pointed(["the backups on nas-file1 failed last night"]))

    def test_knowitall2s_own_name_points_only_in_the_knowitall2_project(self) -> None:
        # "Use KnowItAll2 recall" in a heartbeat prompt pointed to its file in projects that never touched it.
        self.assertEqual([], self.pointed(["Use KnowItAll2 recall before you start."]))
        own = make_repository(self.root / "knowitall2", "https://gitlab.example.com/team/knowitall2.git")
        for number in range(3):
            saved = self.remember(f"KnowItAll2 build note {number}: run the suite.", scope="project",
                                  project_path=own)
            self.file(saved.id, "KnowItAll2", aliases=["KIA2"], kind="software")
        known.worked_in(own)
        known.refresh(self.connection)
        self.assertEqual(["knowitall2.md"], self.pointed(["Why is KnowItAll2 recall slow?"], folder=own / known.FOLDER))

    def test_a_sentence_passing_on_knowitall2s_output_brings_no_pointer(self) -> None:
        # "KnowItAll2 learned 12 things ... through the gateway host" pointed to that host's file (2026-10-12).
        relayed = ["KnowItAll2 learned 12 things from this chat, among them how to reach nas-file1."]
        self.assertEqual([], self.pointed(["go on"], own_words=relayed))
        self.assertEqual(["synology.md"], self.pointed(
            ["go on"], own_words=["KnowItAll2 saved a note.\nNext I'll copy the image to nas-file1."]))

    def test_the_agents_own_tools_point_only_from_the_users_words(self) -> None:
        # "run one-off PowerShell": powershell.md was pointed to 6 times, relevant once.
        self.assertEqual([], self.pointed(["go on"], own_words=["I'll run a one-off PowerShell command for that."]))
        self.assertEqual(["powershell.md"], self.pointed(["PowerShell keeps mangling the quotes"]))

    def test_the_agents_own_tools_are_marked_from_a_fixed_list_and_the_kind_alone(self) -> None:
        for name, kind, aliases, expected in (
                ("Codex", "software", [], True), ("Claude Code", "software", ["claude.exe"], True),
                ("WSL Ubuntu", "software", ["WSL", "Ubuntu"], True), ("Windows shell", "software", ["pwsh"], True),
                ("save-local-work.ps1", "software", [], True), ("Docker Desktop", "other", [], True),
                ("Hermes", "software", ["Hermes gateway"], False), ("OpenClaw", "software", [], False),
                ("codex", "server", [], False), ("Git Bash", "project", [], False)):
            with self.subTest(name):
                self.assertIs(expected, catalog.workstation_tool(name, kind, aliases))


class MessagePartsTests(unittest.TestCase):
    """What arrives as a message, read by who wrote it."""

    def test_monitor_events_and_background_notices_are_nobodys_words_and_a_subagents_result_is_the_agents(self) -> None:
        monitor = ('<task-notification>\n<task-id>b6q</task-id>\n<summary>Monitor event: "store tags"</summary>\n'
                   '<event>codex exec tagged 4 lessons on nas-file1</event>\n</task-notification>')
        background = ('<task-notification>\n<task-id>brh</task-id>\n<status>completed</status>\n'
                      '<summary>Background command "copy to nas-file1" completed (exit code 0)</summary>\n'
                      '</task-notification>')
        subagent = ('<task-notification>\n<task-id>a9e</task-id>\n<status>completed</status>\n'
                    '<summary>Agent "Security review" finished</summary>\n'
                    '<result>The share on nas-file1 is world-readable.</result>\n</task-notification>')
        self.assertEqual(([], []), known.message_parts(monitor))
        self.assertEqual(([], []), known.message_parts(background))
        self.assertEqual(([], ["The share on nas-file1 is world-readable."]), known.message_parts(subagent))

    def test_a_message_from_another_session_is_read_without_its_header_as_another_agents_words(self) -> None:
        handoff = ('<cross-session-message from="uds:pipe" from-session="local_1" from-name="Codex audit review">\n'
                   'Base your work on the branch for nas-file1.\n</cross-session-message>')
        users, agents = known.message_parts(handoff)
        self.assertEqual([], users)
        self.assertEqual(1, len(agents))
        self.assertNotIn("Codex audit", agents[0])
        self.assertIn("nas-file1", agents[0])

    def test_the_users_message_and_automation_prompts_are_the_users_and_app_blocks_are_left_out(self) -> None:
        self.assertEqual((["the backup on nas-file1 failed"], []), known.message_parts("the backup on nas-file1 failed"))
        heartbeat = "<heartbeat>\n<instructions>Check the Synology backups.</instructions>\n</heartbeat>"
        self.assertEqual(([heartbeat], []), known.message_parts(heartbeat))
        browser = ('<in-app-browser-context source="ambient-ui-state">\n- Current URL: https://nas-file1.example.test/\n'
                   '</in-app-browser-context>\nplease check it')
        users, agents = known.message_parts(browser)
        self.assertEqual([], agents)
        self.assertNotIn("nas-file1", users[0])
        self.assertIn("please check it", users[0])


class GenericNameTests(KnownTestCase):
    """A name most memories use in passing, measured from the store."""

    def setUp(self) -> None:
        super().setUp()
        self.own = []
        for number in range(3):
            saved = self.remember(f"The network plane note {number}: the router answers on 10.0.0.{number}.",
                                  scope="project")
            self.file(saved.id, "Network plane", aliases=["UniFi"], kind="other")
            self.own.append(saved)
        self.passing = []
        for number in range(10):
            saved = self.remember(f"The camera {number} sits on the UniFi switch port {number}.", scope="global")
            self.file(saved.id, "Frigate", kind="software")
            self.passing.append(saved)
        self.specific = self.remember("Frigate's cameras need the network plane's VLAN 30.", scope="global")
        self.file(self.specific.id, "Frigate", kind="software")
        known.worked_in(self.project)
        known.refresh(self.connection)
        self.folder = self.project / known.FOLDER

    def test_a_name_most_memories_use_in_passing_lists_none_of_them_again_in_its_file(self) -> None:
        # network-plane.md had 7 memories of its own and 1,918 lines, every memory saying "UniFi" listed again.
        plane = known.render(self.connection)["network-plane.md"]
        for record in self.passing:
            self.assertNotIn(record.id, plane)
        self.assertIn(self.specific.id, plane.split(f"## {known.ELSEWHERE}")[1])   # named by its own name

    def test_a_name_used_in_passing_points_from_the_users_words_but_not_the_agents(self) -> None:
        def pointed(texts=(), own_words=()):
            return [target for target, _ in known.find_pointers(self.connection, list(texts), self.folder, [],
                                                                own_words=list(own_words))]

        self.assertEqual([], pointed(["go on"], ["The UniFi controller restarted."]))
        self.assertEqual(["network-plane.md"], pointed(["The UniFi controller restarted."]))
        self.assertEqual(["network-plane.md"], pointed(["go on"], ["The network plane dropped the route."]))


class DependencyTests(KnownTestCase):
    """A project's folder, built from what it depends on."""

    def setUp(self) -> None:
        super().setUp()
        self.remember("Alpha deploys with make deploy.", scope="project")
        for system, kind in (("EdgeRouter", "device"), ("Printer", "device"), ("Vaultwarden", "software"),
                             ("PowerShell", "software")):
            for number in range(3):
                self.file(self.remember(f"The {system} note {number}: step {number}.", scope="global").id, system,
                          kind=kind)
        known.worked_in(self.project)

    def test_a_folder_gets_the_systems_its_documents_and_scripts_name_in_two_files(self) -> None:
        # saltbox's documents named Immich, Vaultwarden, a gateway host, SABnzbd and 13 more its folder lacked.
        (self.project / "README.md").write_text("Alpha sits behind the EdgeRouter. Run it from PowerShell.\n",
                                                encoding="utf-8")
        (self.project / "docs" / "ops").mkdir(parents=True)
        (self.project / "docs" / "ops" / "network.md").write_text("Open port 443 on the EdgeRouter.\n", encoding="utf-8")
        (self.project / "scripts").mkdir()
        (self.project / "scripts" / "deploy.ps1").write_text("# PowerShell: print to the Printer\n", encoding="utf-8")
        (self.project / ".hidden").mkdir()
        (self.project / ".hidden" / "notes.md").write_text("Vaultwarden\n", encoding="utf-8")
        (self.project / "docs" / "secrets.md").write_text("The keys are in Vaultwarden.\n", encoding="utf-8")
        known.refresh(self.connection)
        self.assertEqual(["README.md", "alpha.md", "edgerouter.md"], listed(self.project))
        self.assertIn("- edgerouter.md: EdgeRouter", text_of(self.project / known.FOLDER / known.INDEX))
        # Once a second document names it, it joins at the next refresh.
        (self.project / "docs" / "backup.md").write_text("Export the Vaultwarden vault weekly.\n", encoding="utf-8")
        self.remember("Alpha's release needs a tag.", scope="project")
        known.refresh(self.connection)
        self.assertIn("vaultwarden.md", listed(self.project))
        self.assertNotIn("powershell.md", listed(self.project))   # never the agent's own tools

    def test_a_project_that_is_itself_a_system_gets_what_its_shared_memories_name(self) -> None:
        # 180 of the Saltbox system's memories are shared and 46 the saltbox project's; Immich was missing.
        saltbox = make_repository(self.root / "saltbox", "https://gitlab.example.com/team/saltbox.git")
        self.remember("saltbox deploys with ansible.", scope="project", project_path=saltbox)
        for number in range(3):
            self.file(self.remember(f"Immich note {number}: its library is on the array.", scope="global").id,
                      "Immich", kind="software")
        shared = self.remember("Saltbox runs Immich behind Authelia.", scope="global")
        self.file(shared.id, "saltbox", kind="project")
        known.worked_in(saltbox)
        known.refresh(self.connection)
        self.assertIn("immich.md", listed(saltbox))
        self.assertNotIn("immich.md", listed(self.project))

    def test_hosts_reached_by_command_and_files_named_while_missing_join_the_folder(self) -> None:
        known.refresh(self.connection)
        folder = self.project / known.FOLDER
        self.assertNotIn("edgerouter.md", listed(self.project))
        known.find_pointers(self.connection, ["go on"], folder, [], commands=["ssh admin@edgerouter show ip route"])
        known.refresh(self.connection)
        self.assertNotIn("edgerouter.md", listed(self.project))   # one command is a passing look
        known.find_pointers(self.connection, ["go on"], folder, [],
                            commands=["ssh admin@edgerouter show interfaces", "knowitall2 recall printer",
                                      "pwsh -c Get-Item", r"rg vaultwarden C:\work\alpha\knowitall2-known"])
        self.assertTrue(known.due(self.connection))   # the hook's nudge starts the refresh
        known.refresh(self.connection)
        self.assertIn("edgerouter.md", listed(self.project))
        self.assertNotIn("printer.md", listed(self.project))   # KnowItAll2's own commands name what they look up
        self.assertNotIn("vaultwarden.md", listed(self.project))
        # Named while missing: no pointer to nowhere, and the file is there for the next mention.
        self.assertEqual([], known.find_pointers(self.connection, ["the Printer jammed"], folder, []))
        known.refresh(self.connection)
        self.assertIn("printer.md", listed(self.project))
        self.assertEqual(["printer.md"], [target for target, _ in known.find_pointers(
            self.connection, ["the Printer jammed again"], folder, [])])

    def test_what_was_noted_more_than_thirty_days_ago_leaves_the_folder(self) -> None:
        known.refresh(self.connection)
        folder = self.project / known.FOLDER
        known.find_pointers(self.connection, ["go on"], folder, [], commands=["ssh edgerouter", "ping edgerouter"])
        known.refresh(self.connection)
        self.assertIn("edgerouter.md", listed(self.project))
        self.clock.value = "2026-10-28T12:00:00Z"   # 31 days on
        known.worked_in(self.project)
        known.refresh(self.connection)
        self.assertNotIn("edgerouter.md", listed(self.project))


class MessageHookTests(unittest.TestCase):
    """Through the message hook, as Claude Code calls it."""

    def setUp(self) -> None:
        from test_learning import SESSION, LogBuilder

        self.session, self.builder = SESSION, LogBuilder
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.alpha = make_repository(self.root / "work" / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.environment = mock.patch.dict(os.environ, {
            "KNOWITALL2_HOME": str(self.root / "data"), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"), known.ENVIRONMENT_SWITCH: "1",
        })
        self.environment.start()
        self.log = self.root / "claude" / "projects" / "C--work-alpha" / f"{SESSION}.jsonl"
        LogBuilder(self.alpha).user("Let's work on alpha.").write(self.log, idle=False)
        store = Store.open(self.root / "data" / "knowitall2.db")
        try:
            memory = Memory(store, agent="cli")
            for system_id, name, alias, scope in (("sys-syn", "Synology", "nas-file1", "project"),
                                                  ("sys-edge", "EdgeRouter", "edge-gw1", "global")):
                store.upsert_system(system_id=system_id, name=name, area="Homelab", kind="device", aliases=[alias],
                                    now=memory.now())
                for number in range(3):
                    saved = memory.remember(f"The {name} note {number}: sudo -u hermes and run the helper.",
                                            scope=scope, project_path=self.alpha).record
                    store.set_note(saved.id, headline=name, system_id=system_id, facet="access",
                                   written_by="catalog", now=memory.now())
            known.worked_in(self.alpha)
            known.refresh(store._connection)
        finally:
            store.close()
        from knowitall2 import hooks

        hooks.session_start(json.dumps({"session_id": self.session, "transcript_path": str(self.log),
                                        "cwd": str(self.alpha), "source": "startup"}), start_learner=lambda: None)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def message(self, prompt: str) -> str:
        from knowitall2 import hooks

        payload = json.dumps({"session_id": self.session, "transcript_path": str(self.log), "cwd": str(self.alpha),
                              "hook_event_name": "UserPromptSubmit", "prompt": prompt})
        output = hooks.prompt_submit(payload, agent="claude-code")
        return json.loads(output)["hookSpecificOutput"]["additionalContext"] if output else ""

    def append(self, builder) -> None:
        with self.log.open("a", encoding="utf-8") as stream:
            for record in builder.records:
                stream.write(json.dumps(record) + "\n")

    def refresh(self) -> None:
        store = Store.open(self.root / "data" / "knowitall2.db")
        try:
            known.refresh(store._connection)
        finally:
            store.close()

    def test_a_monitor_event_brings_no_pointer_and_a_subagents_result_does(self) -> None:
        self.assertNotIn("synology.md", self.message(
            '<task-notification>\n<task-id>b6q</task-id>\n<summary>Monitor event: "copies"</summary>\n'
            '<event>copied 3 files to nas-file1</event>\n</task-notification>'))
        self.assertIn("synology.md", self.message(
            '<task-notification>\n<task-id>a9e</task-id>\n<summary>Agent "Review" finished</summary>\n'
            '<result>The share on nas-file1 is world-readable.</result>\n</task-notification>'))

    def test_a_subagents_answer_to_the_agents_call_counts_as_the_agents_words(self) -> None:
        self.message("hello")
        self.append(self.builder(self.alpha).tool("t1", "Agent", {"description": "check the share"},
                                                  "The share on nas-file1 is full."))
        self.assertIn("synology.md", self.message("what did it find?"))

    def test_hosts_the_chats_commands_reach_join_the_folder(self) -> None:
        self.message("hello")
        self.assertNotIn("edgerouter.md", listed(self.alpha))
        self.append(self.builder(self.alpha)
                    .tool("t1", "Bash", {"command": "ssh admin@edge-gw1 show ip route"}, "ok")
                    .tool("t2", "PowerShell", {"command": "Test-NetConnection edge-gw1 -Port 22"}, "ok"))
        self.assertNotIn("edgerouter.md", self.message("did it answer?"))   # a command is no pointer
        self.refresh()
        self.assertIn("edgerouter.md", listed(self.alpha))


def link_folder(link: Path, target: Path) -> None:
    """A Windows junction (which ``is_symlink`` does not see), elsewhere a symbolic link, to a folder."""

    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


class CandidateReviewTests(KnownTestCase):
    """The candidate review of 0.11.1 (docs/review-2026-10-14-candidate-0-11-1.md), findings in the pointer part."""

    def setUp(self) -> None:
        super().setUp()
        self.remember("Alpha deploys with make deploy.", scope="project")
        for system, kind in (("EdgeRouter", "device"), ("Printer", "device"), ("Vaultwarden", "software"),
                             ("PowerShell", "software"), ("Sonarr", "software"), ("Radarr", "software")):
            for number in range(3):
                self.file(self.remember(f"The {system} note {number}: step {number}.", scope="global").id, system,
                          kind=kind)
        known.worked_in(self.project)
        known.refresh(self.connection)
        self.folder = self.project / known.FOLDER

    def test_a_command_reaching_a_system_by_url_or_network_path_counts_as_reached(self) -> None:
        # E-M3: curl http://sonarr:8989/... and \\nas01\share are how agents most often reach a service.
        known.find_pointers(self.connection, ["go on"], self.folder, [], commands=[
            "curl -s http://sonarr:8989/api/v3/system/status", "Invoke-RestMethod https://sonarr/api/v3/queue",
            "dir \\\\edgerouter\\share", "copy a.txt \\\\edgerouter\\backups\\",
            "cd C:\\work\\printer\\src", 'type "C:\\\\work\\\\printer\\\\a.txt"'])
        known.refresh(self.connection)
        self.assertIn("sonarr.md", listed(self.project))
        self.assertIn("edgerouter.md", listed(self.project))
        self.assertNotIn("printer.md", listed(self.project))   # a folder deeper in a path is no host

    def test_a_hub_host_most_memories_name_in_passing_still_joins_by_command_and_by_document(self) -> None:
        # E-M4: services filed under themselves say "runs on dockhost", so the host itself looks generic.
        for number in range(3):
            self.file(self.remember(f"dockhost note {number}: disk {number} is ext4.", scope="global").id, "dockhost")
        for number in range(30):
            self.file(self.remember(f"Sonarr instance {number} runs on dockhost.", scope="global").id, "Sonarr",
                      kind="software")
        hub = make_repository(self.root / "beta", "https://gitlab.example.com/team/beta.git")
        self.remember("Beta builds with make.", scope="project", project_path=hub)
        (hub / "README.md").write_text("Beta runs on dockhost.\n", encoding="utf-8")
        (hub / "docs").mkdir()
        (hub / "docs" / "deploy.md").write_text("Copy the image to dockhost.\n", encoding="utf-8")
        known.worked_in(hub)
        known.refresh(self.connection)
        self.assertIn(("dockhost",), known.generic_names())
        self.assertIn("dockhost.md", listed(hub))
        for _ in range(2):
            known.find_pointers(self.connection, ["go on"], self.folder, [], commands=["ssh admin@dockhost docker ps"])
        known.refresh(self.connection)
        self.assertIn("dockhost.md", listed(self.project))
        # Still no pointer from the agent's words, and no second listing of those memories in its file.
        self.assertEqual([], [target for target, _ in known.find_pointers(
            self.connection, ["go on"], self.folder, [], own_words=["dockhost is busy"])])
        self.assertNotIn(known.ELSEWHERE, known.render(self.connection)["dockhost.md"])

    def test_the_hooks_refresh_check_reads_what_was_noted_once(self) -> None:
        # E-L2: it read needed.json again for each project, in every hook: 13 to 179 ms.
        for number in range(5):
            known.note_needed(f"prj-{number}", "named", {"edgerouter.md": 1, "printer.md": 1})
        with mock.patch.object(known, "_read_needed", wraps=known._read_needed) as read:
            known._needed_marker()
        self.assertEqual(1, read.call_count)

    def test_two_names_joined_by_a_slash_in_the_users_words_point_to_both(self) -> None:
        # E-L3: "Sonarr/Radarr" means two systems; a branch or a path does not.
        table = known.name_table(self.connection)
        self.assertEqual(["sonarr.md", "radarr.md"], known.pointer_targets(table, ["Sonarr/Radarr both stopped importing"]))
        self.assertEqual(["sonarr.md"], known.pointer_targets(table, ["is it Sonarr/Docker or the share?"]))
        for words in ("pushed origin sonarr/fix-imports", "see sonarr/config/config.xml", "sonarr/v4 branch"):
            with self.subTest(words):
                self.assertEqual([], known.pointer_targets(table, [words]))
        self.assertEqual([], known.pointer_targets(table, ["go on"], ["Sonarr/Radarr both stopped"]))

    def test_the_document_scan_skips_junctions_packages_and_binaries_and_reads_utf16_scripts(self) -> None:
        # E-L4.
        (self.project / "README.md").write_text("Alpha sits behind the EdgeRouter.\n", encoding="utf-8")
        (self.project / "scripts").mkdir()
        (self.project / "scripts" / "setup.ps1").write_bytes("# Open the EdgeRouter port\r\n".encode("utf-16"))
        (self.project / "scripts" / "tool.exe").write_bytes(b"MZ\x00\x01\x00Vaultwarden\x00\x00")
        docs = self.project / "docs"
        (docs / "node_modules" / "one").mkdir(parents=True)
        (docs / "node_modules" / "two").mkdir(parents=True)
        (docs / "node_modules" / "one" / "README.md").write_text("Prints to the Printer.\n", encoding="utf-8")
        (docs / "node_modules" / "two" / "README.md").write_text("Also the Printer.\n", encoding="utf-8")
        (docs / "secrets.md").write_text("The keys are in Vaultwarden.\n", encoding="utf-8")
        link_folder(docs / "loop", docs)
        named = known.documented(self.project, known.name_table(self.connection))
        self.assertIn("edgerouter.md", named)
        self.assertNotIn("printer.md", named)
        self.assertNotIn("vaultwarden.md", named)   # one document, seen once: not through the loop, not in a binary

    def test_the_user_naming_a_tool_does_not_bring_its_file_into_the_folder(self) -> None:
        # E-L7: "Why is PowerShell so slow?" added powershell.md to the folder for 30 days.
        self.assertEqual([], known.find_pointers(self.connection, ["Why is PowerShell so slow?"], self.folder, []))
        self.assertEqual([], known.find_pointers(self.connection, ["the Printer jammed"], self.folder, []))
        known.refresh(self.connection)
        self.assertNotIn("powershell.md", listed(self.project))
        self.assertIn("printer.md", listed(self.project))


if __name__ == "__main__":
    unittest.main()
