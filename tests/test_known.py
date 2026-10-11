"""Knowledge files: everything KnowItAll2 knows, written into each project for agents' own tools."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from _support import Clock, make_repository

from knowitall2 import cli, known, sync
from knowitall2.memory import Memory
from knowitall2.store import Store

GIT = shutil.which("git")
RG = shutil.which("rg")


class KnownTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.saved_environment = {key: os.environ.get(key) for key in ("KNOWITALL2_HOME", known.ENVIRONMENT_SWITCH)}
        os.environ["KNOWITALL2_HOME"] = str(self.root / "home")
        os.environ.pop(known.ENVIRONMENT_SWITCH, None)
        (self.root / "home").mkdir()
        self.project = make_repository(self.root / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.clock = Clock()
        self.store = Store.open(self.root / "home" / "knowitall2.db")
        self.memory = Memory(self.store, agent="codex", clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        for key, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def remember(self, text: str, **options):
        options.setdefault("project_path", self.project)
        return self.memory.remember(text, **options).record

    def file(self, record_id: str, system: str, *, aliases=(), facet: str = "access") -> None:
        system_id = "sys-" + system.casefold().replace(" ", "-")
        self.store.upsert_system(system_id=system_id, name=system, area="Homelab", kind="device", aliases=list(aliases),
                                 now=self.clock())
        self.store.set_note(record_id, headline=system, system_id=system_id, facet=facet, written_by="catalog",
                            now=self.clock())

    @property
    def connection(self) -> sqlite3.Connection:
        return self.store._connection


class RenderTests(KnownTestCase):
    def test_every_active_memory_is_in_exactly_one_file(self) -> None:
        route = self.remember("To read files on the NAS, ssh to the gateway and sudo -u hermes, then run the helper.",
                              scope="global")
        self.file(route.id, "Synology", aliases=["nas-file1", "NAS"])
        local = self.remember("The alpha release script needs a tag first.", scope="project")
        loose = self.remember("Prefer tabs over spaces in shell scripts.", scope="global")
        gone = self.remember("The old router lived at 10.0.0.1.", scope="global")
        self.memory.forget(gone.id, reason="replaced")
        files = known.render(self.connection)
        self.assertIn(route.id, files["synology.md"])
        self.assertIn("Also called: nas-file1, NAS", files["synology.md"])
        self.assertIn(local.id, files["alpha.md"])
        self.assertIn(loose.id, files["general.md"])
        everything = "".join(files.values())
        self.assertNotIn(gone.id, everything)
        for record in (route, local, loose):
            self.assertEqual(1, sum(record.id in text for name, text in files.items() if name != known.INDEX))

    def test_a_memory_is_also_listed_in_the_files_of_the_systems_it_names(self) -> None:
        # 2026-10-05: the iDRAC addresses were filed under the homelab and missing from the iDRAC's file.
        helper = self.remember("The read-only iDRAC helper runs on the jump host as hermes.", scope="global")
        self.file(helper.id, "Dell iDRAC", aliases=["iDRAC", "Redfish"])
        addresses = self.remember("Homelab iDRAC endpoints: vhost1-mgmt 10.9.15.19 (helper profile vhost1-mgmt).",
                                  scope="global")
        self.file(addresses.id, "Homelab", facet="where")
        path_only = self.remember("Homelab runbooks are in docs/idrac/ of the repository.", scope="global")
        self.file(path_only.id, "Homelab", facet="about")
        nowhere = self.remember("The Grafana dashboards are on the Homelab wiki.", scope="global")
        self.file(nowhere.id, "Homelab", facet="about")
        files = known.render(self.connection)
        idrac = files["dell-idrac.md"]
        self.assertIn("1 memories, newest first.\n1 more, filed in other files, name it; they are listed last.", idrac)
        own, listed = idrac.split(f"## {known.ELSEWHERE}\n")
        self.assertIn(helper.id, own)
        self.assertIn(f"[{addresses.id}] fact, saved", listed)
        self.assertIn("filed in homelab.md: Homelab iDRAC endpoints: vhost1-mgmt 10.9.15.19", listed)
        self.assertNotIn(path_only.id, idrac)   # a folder in a path is not a mention
        # Filed in one file only; a name with no file of its own (Grafana) makes none.
        self.assertIn(addresses.id, files["homelab.md"])
        self.assertNotIn(known.ELSEWHERE, files["homelab.md"])
        self.assertNotIn("grafana.md", files)
        self.assertIn("- dell-idrac.md: Dell iDRAC (also iDRAC, Redfish), 1 memories, and 1 filed elsewhere that name it",
                      files[known.INDEX])

    def test_named_finds_the_longest_name_in_order_and_skips_parts_of_paths_and_logins(self) -> None:
        table = {("lab", "vhost1"): "vhost1", ("lab", "vhost1", "mgmt"): "idrac", ("saltbox",): "saltbox",
                 ("codex",): "codex"}
        self.assertEqual(["saltbox", "vhost1", "idrac"],
                         known.named("Saltbox runs on Lab-vhost1; its iDRAC is lab_vhost1-mgmt. lab-vhost1 again.", table))
        self.assertEqual([], known.named("ssh codex@host; see C:\\Projects\\saltbox\\ and https://x/.codex", table))
        self.assertEqual(["saltbox"], known.named("saltbox/docs is a host path", table))
        self.assertEqual([], known.named("", {}))

    def test_a_system_and_a_project_with_one_name_share_a_file(self) -> None:
        # Two writes to one file name used to overwrite each other and lose memories.
        project_memory = self.remember("Alpha's tests run with make check.", scope="project")
        system_memory = self.remember("The Alpha service listens on port 8080.", scope="global")
        self.file(system_memory.id, "ALPHA")
        files = known.render(self.connection)
        self.assertIn(project_memory.id, files["alpha.md"])
        self.assertIn(system_memory.id, files["alpha.md"])
        self.assertTrue(files["alpha.md"].startswith("# ALPHA / alpha") or files["alpha.md"].startswith("# alpha / ALPHA"))

    def test_newest_first_short_lines_and_unverified_marked(self) -> None:
        older = self.remember("First note about the gateway.", scope="global")
        self.clock.value = "2026-09-27T12:05:00Z"
        newer = self.remember("Second note about the gateway " + "with many words " * 30 + "lab-very-long-host-name-x.",
                              scope="global", source="observed")
        text = known.render(self.connection)["general.md"]
        self.assertLess(text.index(newer.id), text.index(older.id))
        for line in text.splitlines():
            self.assertLessEqual(len(line), known.LINE_WIDTH + 2)
        self.assertIn("lab-very-long-host-name-x.", text)   # names are never broken across lines
        self.assertIn(f"[{older.id}] fact, saved", text)
        self.assertIn("(unverified: check before relying on it)", text.split(older.id)[1].splitlines()[0])
        self.assertNotIn("unverified", text.split(newer.id)[1].splitlines()[0])

    def test_how_to_reach_it_and_how_tos_come_first(self) -> None:
        status = self.remember("The NAS fans were replaced in August.", scope="global")
        self.file(status.id, "Synology", facet="status")
        self.clock.value = "2026-09-27T12:01:00Z"
        steps = self.remember("To rebuild an NFS share, stop the client mounts first.", scope="global", kind="procedure")
        self.file(steps.id, "Synology", facet="howto")
        self.clock.value = "2026-09-27T12:02:00Z"
        newest = self.remember("The NAS runs DSM 7.3.", scope="global")
        self.file(newest.id, "Synology", facet="about")
        route = self.remember("To read files on the NAS, ssh to the gateway and sudo -u hermes.", scope="global")
        self.clock.value = "2026-09-27T11:00:00Z"   # the oldest memory, still first because it says how to reach it
        self.connection.execute("UPDATE records SET created_at = ? WHERE id = ?", ("2026-09-27T11:00:00Z", route.id))
        self.file(route.id, "Synology", facet="access")
        text = known.render(self.connection)["synology.md"]
        order = [text.index(record.id) for record in (route, steps, newest, status)]
        self.assertEqual(sorted(order), order)
        self.assertIn("## How to reach it, and where its sign-in is kept", text)
        self.assertIn("## How-tos", text)
        self.assertIn("## Everything else", text)
        self.assertIn("4 memories, newest first.", text)

    def test_index_names_every_file_and_is_marked(self) -> None:
        route = self.remember("The NAS answers on 10.0.0.10.", scope="global")
        self.file(route.id, "Synology")
        files = known.render(self.connection)
        index = files[known.INDEX]
        self.assertEqual(known.MARKER, index.splitlines()[0])
        self.assertIn("- synology.md: Synology, 1 memories", index)

    def test_file_names_are_safe(self) -> None:
        self.assertEqual("readme-.md", known.file_name("README"))
        self.assertEqual("con-.md", known.file_name("CON"))
        self.assertEqual("save-local-work.ps1.md", known.file_name("save-local-work.ps1"))
        self.assertEqual("vcenter-lab.md", known.file_name("vCenter Lab"))

    def test_secret_like_text_is_redacted_again(self) -> None:
        saved = self.remember("Router admin is kept in Vaultwarden item 'router admin'.", scope="global")
        value = "Hunter2" + "Secret!x9"   # built here so the source holds no secret-shaped text
        self.connection.execute("UPDATE records SET text = ? WHERE id = ?",
                                ("Router " + "pass" + "word=" + value + " for admin.", saved.id))
        text = known.render(self.connection)["general.md"]
        self.assertNotIn(value, text)


class FolderTests(KnownTestCase):
    def files(self) -> dict[str, str]:
        route = self.remember("To read files on the NAS, sudo -u hermes and run the helper.", scope="global")
        self.file(route.id, "Synology")
        return known.render(self.connection)

    def test_written_out_of_git_and_visible_to_search(self) -> None:
        result = known.write_folder(self.project, self.files())
        self.assertIsNone(result.skipped)
        self.assertTrue((self.project / known.FOLDER / "synology.md").is_file())
        exclude = (self.project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(f"/{known.FOLDER}/", exclude)
        self.assertIn("/.ignore", exclude)
        self.assertEqual(known.IGNORE_TEXT, (self.project / ".ignore").read_text(encoding="utf-8"))

    def test_an_unchanged_folder_is_not_rewritten_and_stale_files_go(self) -> None:
        files = self.files()
        known.write_folder(self.project, files)
        stale = self.project / known.FOLDER / "old-system.md"
        stale.write_text("old", encoding="utf-8")
        again = known.write_folder(self.project, files)
        self.assertEqual(0, again.written)
        self.assertEqual(1, again.removed)
        self.assertFalse(stale.exists())
        exclude = (self.project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(1, exclude.count(known.EXCLUDE_BEGIN))

    def test_a_folder_or_ignore_file_it_did_not_write_is_left_alone(self) -> None:
        foreign = self.project / known.FOLDER
        foreign.mkdir()
        (foreign / "notes.md").write_text("mine", encoding="utf-8")
        result = known.write_folder(self.project, self.files())
        self.assertIn("not written by KnowItAll2", result.skipped)
        self.assertEqual(["notes.md"], [path.name for path in foreign.iterdir()])
        other = make_repository(self.root / "beta")
        (other / ".ignore").write_text("node_modules/\n", encoding="utf-8")
        result = known.write_folder(other, self.files())
        self.assertIsNone(result.skipped)
        self.assertEqual("node_modules/\n", (other / ".ignore").read_text(encoding="utf-8"))
        self.assertTrue(any("add the line" in note for note in result.notes))

    def test_a_worktree_uses_the_shared_exclude_file(self) -> None:
        main_git = self.project / ".git"
        (main_git / "worktrees" / "feature").mkdir(parents=True)
        (main_git / "worktrees" / "feature" / "commondir").write_text("../..\n", encoding="utf-8")
        tree = self.root / "alpha-feature"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {main_git / 'worktrees' / 'feature'}\n", encoding="utf-8")
        self.assertEqual(main_git.resolve(), known.git_common_dir(tree))
        known.write_folder(tree, self.files())
        self.assertIn(f"/{known.FOLDER}/", (main_git / "info" / "exclude").read_text(encoding="utf-8"))
        self.assertTrue((tree / known.FOLDER / known.INDEX).is_file())

    def test_not_a_git_folder_is_skipped(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual("not a Git working tree", known.write_folder(plain, self.files()).skipped)
        self.assertFalse((plain / known.FOLDER).exists())

    def test_removal_takes_out_only_what_it_added(self) -> None:
        known.write_folder(self.project, self.files())
        exclude = self.project / ".git" / "info" / "exclude"
        exclude.write_text("*.log\n" + exclude.read_text(encoding="utf-8"), encoding="utf-8")
        changes = known.remove_folder(self.project)
        self.assertEqual(3, len(changes))
        self.assertFalse((self.project / known.FOLDER).exists())
        self.assertFalse((self.project / ".ignore").exists())
        self.assertEqual("*.log\n", exclude.read_text(encoding="utf-8"))

    @unittest.skipUnless(GIT, "needs git")
    def test_git_sees_no_change_in_a_real_repository_and_its_worktree(self) -> None:
        repo = self.root / "real"
        run = lambda *args, cwd=repo: subprocess.run([GIT, *args], cwd=cwd, check=True, capture_output=True, text=True)
        repo.mkdir()
        run("init", "-q")
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        run("add", "a.txt")
        run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
        run("worktree", "add", "-q", str(self.root / "real-wt"), "-b", "wt")
        for tree in (repo, self.root / "real-wt"):
            known.write_folder(tree, self.files())
            self.assertEqual("", run("status", "--porcelain", cwd=tree).stdout)
            self.assertTrue((tree / known.FOLDER / "synology.md").is_file())

    @unittest.skipUnless(GIT and RG, "needs git and rg")
    def test_rg_searches_the_folder_although_git_ignores_it(self) -> None:
        repo = self.root / "real"
        repo.mkdir()
        subprocess.run([GIT, "init", "-q"], cwd=repo, check=True)
        known.write_folder(repo, self.files())
        found = subprocess.run([RG, "-l", "hermes", "."], cwd=repo, capture_output=True, text=True).stdout
        self.assertIn("synology.md", found)


class RefreshTests(KnownTestCase):
    def test_refresh_writes_each_project_once_and_only_after_a_change(self) -> None:
        self.remember("Alpha deploys with make deploy.", scope="project")
        report = known.refresh(self.connection)
        self.assertFalse(report.unchanged)
        self.assertTrue((self.project / known.FOLDER / "alpha.md").is_file())
        self.assertTrue(known.refresh(self.connection).unchanged)
        self.remember("Alpha's staging host is stage-1.", scope="project")
        self.assertTrue(known.due(self.connection))
        known.refresh(self.connection)
        self.assertIn("stage-1", (self.project / known.FOLDER / "alpha.md").read_text(encoding="utf-8"))
        self.assertEqual([os.path.normcase(os.path.realpath(self.project))],
                         [os.path.normcase(os.path.realpath(path)) for path in known.read_status()["folders"]])

    def test_a_catalog_change_alone_makes_a_refresh_due(self) -> None:
        saved = self.remember("The NAS answers on 10.0.0.10.", scope="global")
        known.refresh(self.connection)
        self.clock.value = "2026-09-27T12:01:00Z"
        self.file(saved.id, "Synology")
        self.assertTrue(known.due(self.connection))

    def test_a_change_from_the_server_older_than_the_newest_memory_makes_a_refresh_due(self) -> None:
        # Audit 2026-10-09: a pulled row keeps its own time, so counts and latest times did not change.
        first = self.remember("The NAS answers on 10.0.0.10.", scope="global")
        self.clock.value = "2026-09-27T12:05:00Z"
        self.remember("The router answers on 10.0.0.1.", scope="global")
        known.refresh(self.connection)
        row = sync.read_row(self.connection, sync.table_for("records"), first.id)
        row.update(text="The NAS answers on 10.0.0.11.", updated_at="2026-09-27T12:01:00Z")
        sync.apply_pulled(self.store, [{"table": "records", "key": first.id, "op": "upsert", "row": row, "seq": 7}])
        self.assertTrue(known.due(self.connection))
        known.refresh(self.connection)
        self.assertIn("10.0.0.11", (self.project / known.FOLDER / known.file_name(known.GENERAL)).read_text(encoding="utf-8"))

    def test_a_second_change_within_the_same_second_makes_a_refresh_due(self) -> None:
        self.remember("The NAS answers on 10.0.0.10.", scope="global")
        known.refresh(self.connection)
        self.remember("The NAS answers on 10.0.0.10.", scope="global", source="user")  # no longer unverified
        self.assertTrue(known.due(self.connection))

    def test_using_a_memory_does_not_make_a_refresh_due(self) -> None:
        self.remember("The NAS answers on 10.0.0.10.", scope="global")
        known.refresh(self.connection)
        self.memory.recall("NAS", project_path=self.project)
        self.assertGreater(self.connection.execute("SELECT MAX(recall_count) FROM records").fetchone()[0], 0)
        self.assertFalse(known.due(self.connection))

    def test_work_without_a_hook_around_it_refreshes_the_files_when_done(self) -> None:
        # Sync, learning, upkeep, and the finder run on their own, mostly after the hook that started them.
        work = {
            ("sync", "--quiet"): "knowitall2.connected.run_command",
            ("learn",): "knowitall2.learning.command.run_learn",
            ("maintain",): "knowitall2.learning.command.run_maintain",
            ("catalog",): "knowitall2.learning.command.run_catalog_command",
        }
        for argv, target in work.items():
            with self.subTest(argv=argv), mock.patch(target, return_value=0), \
                    mock.patch("knowitall2.known.nudge") as refresh:
                self.assertEqual(0, cli.main(list(argv)))
                refresh.assert_called_once_with()
        with mock.patch("knowitall2.learning.command.run_learn", side_effect=RuntimeError("stopped")), \
                mock.patch("knowitall2.known.nudge") as refresh, self.assertRaises(RuntimeError):
            cli.main(["learn"])
        refresh.assert_called_once_with()

    def test_nudge_starts_a_refresh_only_when_due_and_on(self) -> None:
        self.remember("Alpha deploys with make deploy.", scope="project")
        started = []
        launcher = lambda command, **options: started.append(command)
        self.assertTrue(known.nudge(launcher=launcher))
        self.assertEqual(["known", "--quiet"], started[0][-2:])
        (self.root / "home" / known.LOCK_FILE).unlink()
        known.refresh(self.connection)
        self.assertFalse(known.nudge(launcher=launcher))
        self.remember("Alpha's staging host is stage-1.", scope="project")
        os.environ[known.ENVIRONMENT_SWITCH] = "0"
        self.assertFalse(known.nudge(launcher=launcher))
        os.environ.pop(known.ENVIRONMENT_SWITCH)
        known.set_enabled(False)
        self.assertFalse(known.nudge(launcher=launcher))
        known.set_enabled(True)
        self.assertTrue(known.nudge(launcher=launcher))
        self.assertEqual(2, len(started))

    def test_a_running_refresh_is_not_started_twice(self) -> None:
        self.remember("Alpha deploys with make deploy.", scope="project")
        started = []
        launcher = lambda command, **options: started.append(command)
        self.assertTrue(known.nudge(launcher=launcher))
        self.assertFalse(known.nudge(launcher=launcher))
        self.assertEqual(1, len(started))

    def test_turning_off_keeps_other_settings(self) -> None:
        config = self.root / "home" / "config.json"
        config.write_text(json.dumps({"learning": {"enabled": True}}), encoding="utf-8")
        known.set_enabled(False)
        self.assertFalse(known.enabled())
        self.assertEqual({"enabled": True}, json.loads(config.read_text(encoding="utf-8"))["learning"])
        known.set_enabled(True)
        self.assertTrue(known.enabled())

    def test_off_removes_every_folder_and_on_writes_them_again(self) -> None:
        self.remember("Alpha deploys with make deploy.", scope="project")
        known.refresh(self.connection)
        self.store.close()
        arguments = types.SimpleNamespace(off=True, on=False, quiet=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, known.run_command(arguments))
        self.assertFalse((self.project / known.FOLDER).exists())
        self.assertFalse(known.enabled())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, known.run_command(types.SimpleNamespace(off=False, on=True, quiet=False)))
        self.assertTrue((self.project / known.FOLDER / "alpha.md").is_file())
        self.store = Store.open(self.root / "home" / "knowitall2.db")


class PointerTests(KnownTestCase):
    def systems(self, *, memories: int = 3):
        """Three systems, each with enough memories that a pointer to it is worth showing."""

        for system, aliases, words in (("Synology", ["nas-file1", "NAS", "shared-name"], "the NAS"),
                                       ("gw-control", ["control host", "shared-name"], "the gateway"),
                                       ("Hermes", ["gw-control"], "the helper runner")):
            for number in range(memories):
                saved = self.remember(f"Note {number} about {words}: sudo -u hermes, step {number}.", scope="global")
                self.file(saved.id, system, aliases=aliases)
        known.refresh(self.connection)
        return self.project / known.FOLDER

    def test_names_and_unshared_aliases_point_to_their_file(self) -> None:
        self.systems()
        table = known.name_table(self.connection)
        self.assertEqual("synology.md", table[("nas", "file1")])
        self.assertEqual("synology.md", table[("synology",)])
        self.assertEqual("gw-control.md", table[("gw", "control")])   # a system's own name wins over an alias
        self.assertNotIn(("shared", "name"), table)                         # an alias two systems use points nowhere
        self.assertNotIn(("nas",), table)                                   # too short to name one thing
        self.assertEqual("alpha.md", table[("alpha",)])                     # projects too

    def test_a_message_naming_a_system_points_once_to_its_file(self) -> None:
        folder = self.systems()
        found = known.find_pointers(self.connection, ["the images are on nas-file1/files/cisco ap"], folder, [])
        self.assertEqual(["synology.md"], [target for target, _ in found])
        self.assertIn(f"{known.FOLDER}/synology.md (3 memories)", found[0][1])
        self.assertIn("what is known about Synology", found[0][1])
        self.assertEqual([], known.find_pointers(self.connection, ["nas-file1 again"], folder, ["synology.md"]))

    def test_only_whole_names_count_and_at_most_a_few_per_message(self) -> None:
        folder = self.systems()
        self.assertEqual([], known.find_pointers(self.connection, ["synologyx and xnas-file1"], folder, []))
        text = "ssh codex@gw-control then Synology, Hermes, and the alpha repo"
        found = known.find_pointers(self.connection, [text], folder, [])
        self.assertEqual(known.POINTERS_PER_MESSAGE, len(found))
        self.assertEqual("gw-control.md", found[0][0])   # in the order the text names them; a host after @ counts

    def test_logins_paths_and_small_files_bring_no_pointer(self) -> None:
        # Each of these pointed agents to files the work did not need, so they skipped the rest (2026-10-05).
        folder = self.systems()
        tiny = self.remember("Linux note.", scope="global")
        self.file(tiny.id, "Linux")
        known.refresh(self.connection)
        for text in ("ssh synology@10.0.0.5", r"C:\Projects\synology\src", "see .synology/config",
                     "https://synology.example.com/admin", "it runs on Linux"):
            with self.subTest(text):
                self.assertEqual([], known.find_pointers(self.connection, [text], folder, []))
        self.assertEqual(["synology.md"], [t for t, _ in known.find_pointers(
            self.connection, ["the files are on nas-file1/files/cisco ap"], folder, [])])   # a host first in a path

    def test_knowitall2_named_in_the_agents_words_brings_no_pointer(self) -> None:
        # Agents pass on KnowItAll2's news, which pointed them to its file in every project (2026-10-10).
        folder = self.systems()
        for number in range(3):
            saved = self.remember(f"KnowItAll2 note {number}: run its update script.", scope="global")
            self.file(saved.id, "KnowItAll2", aliases=["KIA2"])
        known.refresh(self.connection)
        news = ["KnowItAll2 learned 3 things from this session.", "KIA2 saved the NAS route."]
        self.assertEqual([], known.find_pointers(self.connection, ["go on"], folder, [], own_words=news))
        self.assertEqual(["synology.md"], [t for t, _ in known.find_pointers(
            self.connection, ["go on"], folder, [], own_words=["KnowItAll2 learned how to reach nas-file1."])])
        self.assertEqual(["knowitall2.md"], [t for t, _ in known.find_pointers(
            self.connection, ["how do I update KnowItAll2?"], folder, [], own_words=news)])

    def test_no_pointer_without_the_folder_knowitall2_wrote(self) -> None:
        self.systems()
        other = make_repository(self.root / "beta")
        self.assertEqual([], known.find_pointers(self.connection, ["nas-file1"], other / known.FOLDER, []))

    def test_the_briefing_says_where_everything_is_once_the_folder_exists(self) -> None:
        self.remember("Alpha deploys with make deploy.", scope="project")
        self.assertNotIn(known.FOLDER, self.memory.briefing(project_path=self.project))
        known.refresh(self.connection)
        self.assertIn(f"Everything KnowItAll2 knows is also in {known.FOLDER}/", self.memory.briefing(project_path=self.project))

    def test_every_agent_text_says_where_everything_is(self) -> None:
        from knowitall2.agents import instructions, skill
        from knowitall2.mcp_server import INSTRUCTIONS

        for text in (instructions.TEXT, skill.SKILL_MARKDOWN,
                     INSTRUCTIONS):
            self.assertIn(known.FOLDER, text)
            self.assertIn("do not edit", text.casefold())


class PointerHookTests(unittest.TestCase):
    """Through the message hook, as Claude Code and Codex call it."""

    def setUp(self) -> None:
        from unittest import mock
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
            store.upsert_system(system_id="sys-syn", name="Synology", area="Homelab", kind="device",
                                aliases=["nas-file1"], now=memory.now())
            for number in range(3):
                nas = memory.remember(f"To read files on the NAS, step {number}: sudo -u hermes and run the helper.",
                                      scope="global", project_path=self.alpha).record
                store.set_note(nas.id, headline="NAS", system_id="sys-syn", facet="access", written_by="catalog",
                               now=memory.now())
            known.refresh(store._connection)
        finally:
            store.close()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def message(self, prompt: str, agent: str = "claude-code") -> str:
        from knowitall2 import hooks

        payload = json.dumps({"session_id": self.session, "transcript_path": str(self.log), "cwd": str(self.alpha),
                              "hook_event_name": "UserPromptSubmit", "prompt": prompt})
        output = hooks.prompt_submit(payload, agent=agent)
        return json.loads(output)["hookSpecificOutput"]["additionalContext"] if output else ""

    def test_the_users_message_brings_a_pointer_once_per_chat(self) -> None:
        from knowitall2 import hooks

        hooks.session_start(json.dumps({"session_id": self.session, "transcript_path": str(self.log),
                                        "cwd": str(self.alpha), "source": "startup"}), start_learner=lambda: None)
        context = self.message("the .tar is on nas-file1/files/cisco ap")
        self.assertIn(f"{known.FOLDER}/synology.md (3 memories)", context)
        self.assertNotIn("synology.md", self.message("still about nas-file1"))
        logged = (self.root / "data" / known.POINTER_LOG).read_text(encoding="utf-8").splitlines()
        self.assertEqual("synology.md", json.loads(logged[0])["file"])

    def test_the_agents_own_words_bring_a_pointer_but_its_commands_do_not(self) -> None:
        from knowitall2 import hooks
        from test_learning import text

        hooks.session_start(json.dumps({"session_id": self.session, "transcript_path": str(self.log),
                                        "cwd": str(self.alpha), "source": "startup"}), start_learner=lambda: None)
        self.message("hello")
        with self.log.open("a", encoding="utf-8") as stream:
            for record in self.builder(self.alpha).tool("t1", "Bash", {"command": "net view nas-file1"}, "denied").records:
                stream.write(json.dumps(record) + "\n")
        self.assertNotIn("synology.md", self.message("it did not work"))
        with self.log.open("a", encoding="utf-8") as stream:
            for record in self.builder(self.alpha).assistant(text("I'll try nas-file1 another way.")).records:
                stream.write(json.dumps(record) + "\n")
        self.assertIn("synology.md", self.message("go on"))

    def test_turned_off_means_no_pointer(self) -> None:
        from knowitall2 import hooks

        known.set_enabled(False)
        hooks.session_start(json.dumps({"session_id": self.session, "transcript_path": str(self.log),
                                        "cwd": str(self.alpha), "source": "startup"}), start_learner=lambda: None)
        self.assertNotIn("synology.md", self.message("the .tar is on nas-file1"))


class DoctorTests(KnownTestCase):
    def test_doctor_says_whether_the_files_are_off_unwritten_or_where(self) -> None:
        from knowitall2.doctor import _known_files_check

        self.assertIn("not written yet", _known_files_check().detail)
        other = make_repository(self.root / "beta")
        (other / ".ignore").write_text("node_modules/\n", encoding="utf-8")
        self.remember("Alpha deploys with make deploy.", scope="project")
        self.remember("Beta builds with npm.", scope="project", project_path=other)
        known.refresh(self.connection)
        check = _known_files_check()
        self.assertTrue(check.ok)
        self.assertIn("in 2 project folders", check.detail)
        self.assertIn("add the line", check.detail)
        self.assertIsNotNone(check.fix)
        known.set_enabled(False)
        self.assertIn("off", _known_files_check().detail)


if __name__ == "__main__":
    unittest.main()
