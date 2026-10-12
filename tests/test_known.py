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


def link_folder(link: Path, target: Path) -> None:
    """A symbolic link to a folder, or on Windows without the privilege for one, a junction."""

    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise unittest.SkipTest("symbolic links are not available here")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def link_file(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError:
        raise unittest.SkipTest("symbolic links to files are not available here")


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
        # Which folders were worked in lately is judged by the test's clock, not the day the tests run.
        now = mock.patch.object(known, "_now", side_effect=lambda: self.clock(), create=True)
        now.start()
        self.addCleanup(now.stop)

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
        self.assertEqual("con_.md", known.file_name("CON"))
        self.assertEqual("save-local-work.ps1.md", known.file_name("save-local-work.ps1"))
        self.assertEqual("vcenter-lab.md", known.file_name("vCenter Lab"))

    def test_a_reserved_windows_name_before_a_dot_is_avoided_too(self) -> None:
        # Review 2026-10-11 (N-L3): Windows refuses aux.lan.md as it refuses aux.md.
        self.assertEqual("aux_.lan.md", known.file_name("aux.lan"))
        self.assertEqual("com1_.example.md", known.file_name("COM1.example"))
        self.assertEqual("lpt9_.md", known.file_name("lpt9"))
        self.assertEqual("nul_.office.test.md", known.file_name("nul.office.test"))
        for fine in ("auxiliary.lan", "console", "com10", "prn-server"):
            self.assertEqual(f"{fine}.md", known.file_name(fine))

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

    def test_the_ignore_file_is_kept_out_of_git_only_when_it_is_knowitall2s(self) -> None:
        # Review 2026-10-11 (N-L5): a user's own .ignore, not yet committed, vanished from git status.
        other = make_repository(self.root / "beta")
        (other / known.IGNORE_FILE).write_text("node_modules/\n", encoding="utf-8")
        exclude = other / ".git" / "info" / "exclude"
        known.write_folder(other, self.files())
        self.assertIn(f"/{known.FOLDER}/", exclude.read_text(encoding="utf-8"))
        self.assertNotIn(f"/{known.IGNORE_FILE}", exclude.read_text(encoding="utf-8"))
        # A block written before lists it: it is corrected in place.
        exclude.write_text("*.log\n" + "\n".join([known.EXCLUDE_BEGIN, *known.EXCLUDE_LINES, known.EXCLUDE_END])
                           + "\n*.tmp\n", encoding="utf-8")
        known.write_folder(other, self.files())
        self.assertEqual(f"*.log\n{known.EXCLUDE_BEGIN}\n/{known.FOLDER}/\n{known.EXCLUDE_END}\n*.tmp\n",
                         exclude.read_text(encoding="utf-8"))
        known.write_folder(self.project, self.files())   # KnowItAll2's own .ignore stays out of Git
        self.assertIn(f"/{known.IGNORE_FILE}", (self.project / ".git" / "info" / "exclude").read_text(encoding="utf-8"))

    @unittest.skipUnless(GIT, "needs git")
    def test_a_users_uncommitted_ignore_file_still_shows_in_git_status(self) -> None:
        repo = self.root / "real"
        repo.mkdir()
        subprocess.run([GIT, "init", "-q"], cwd=repo, check=True)
        (repo / known.IGNORE_FILE).write_text("node_modules/\n", encoding="utf-8")
        known.write_folder(repo, self.files())
        status = subprocess.run([GIT, "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
        self.assertEqual(f"?? {known.IGNORE_FILE}\n", status)

    def test_a_worktree_uses_the_shared_exclude_file(self) -> None:
        main_git = self.project / ".git"
        (main_git / "worktrees" / "feature").mkdir(parents=True)
        (main_git / "worktrees" / "feature" / "commondir").write_text("../..\n", encoding="utf-8")
        (main_git / "worktrees" / "feature" / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
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

    def test_a_git_folder_git_cannot_use_is_not_a_repository(self) -> None:
        # The live case: a .git folder holding only info/ in a parent folder of projects got the files.
        stub = self.root / "projects"
        (stub / ".git" / "info").mkdir(parents=True)
        self.assertIsNone(known.git_common_dir(stub))
        self.assertEqual("not a Git working tree", known.write_folder(stub, self.files()).skipped)
        self.assertFalse((stub / known.FOLDER).exists())
        self.assertFalse((stub / known.IGNORE_FILE).exists())
        self.assertFalse((stub / ".git" / "info" / "exclude").exists())

    def test_a_worktree_whose_repository_moved_is_skipped_and_no_git_folder_is_made(self) -> None:
        gone = self.root / "moved-away" / "main" / ".git" / "worktrees" / "wt"
        tree = self.root / "wt"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {gone}\n", encoding="utf-8")
        self.assertEqual("not a Git working tree", known.write_folder(tree, self.files()).skipped)
        self.assertFalse((self.root / "moved-away").exists())
        self.assertFalse((tree / known.FOLDER).exists())

    def test_a_file_that_cannot_be_written_does_not_stop_the_others(self) -> None:
        # One refused name stopped every later file and the index in every project (N-L3).
        files = {"a-bad.md": "# bad\n", "zeta.md": "# zeta\n", **self.files()}
        real = known.write_text_atomic

        def refuse(path, text, **options):
            if Path(path).name == "a-bad.md":
                raise FileExistsError(183, "Cannot create a file when that file already exists")
            real(path, text, **options)

        with mock.patch("knowitall2.known.write_text_atomic", side_effect=refuse):
            result = known.write_folder(self.project, files)
        self.assertIsNone(result.skipped)
        folder = self.project / known.FOLDER
        self.assertEqual(["README.md", "synology.md", "zeta.md"], sorted(path.name for path in folder.iterdir()))
        self.assertTrue(any("a-bad.md" in note for note in result.notes), result.notes)

    def test_a_linked_folder_is_refused_and_where_it_points_is_left_alone(self) -> None:
        # Review 2026-10-11 (N-H2): a cloned repository can carry knowitall2-known/ as a link to any folder.
        outside = self.root / "outside"
        outside.mkdir()
        (outside / known.INDEX).write_text(known.MARKER + "\n", encoding="utf-8")
        (outside / "notes.md").write_text("the user's own\n", encoding="utf-8")
        link_folder(self.project / known.FOLDER, outside)
        result = known.write_folder(self.project, self.files())
        self.assertIn("link", result.skipped)
        known.remove_folder(self.project)
        self.assertEqual(["README.md", "notes.md"], sorted(path.name for path in outside.iterdir()))
        self.assertEqual(known.MARKER + "\n", (outside / known.INDEX).read_text(encoding="utf-8"))
        self.assertFalse((self.project / known.IGNORE_FILE).exists())

    def test_a_folder_holding_anything_but_plain_files_is_refused(self) -> None:
        planted = self.project / known.FOLDER
        (planted / "keep").mkdir(parents=True)
        (planted / known.INDEX).write_text(known.MARKER + "\n", encoding="utf-8")
        (planted / "keep" / "notes.md").write_text("mine\n", encoding="utf-8")
        result = known.write_folder(self.project, self.files())
        self.assertIsNotNone(result.skipped)
        self.assertEqual(known.MARKER + "\n", (planted / known.INDEX).read_text(encoding="utf-8"))
        self.assertEqual(["README.md", "keep"], sorted(path.name for path in planted.iterdir()))

    def test_a_linked_file_in_the_folder_is_never_written_through(self) -> None:
        # The reproduction: general.md as a link to a file such as ~/.bashrc was overwritten with memories.
        victim = self.root / "outside" / "important.conf"
        victim.parent.mkdir()
        victim.write_text("export IMPORTANT=1\n", encoding="utf-8")
        planted = self.project / known.FOLDER
        planted.mkdir()
        (planted / known.INDEX).write_text(known.MARKER + "\n", encoding="utf-8")
        link_file(planted / "general.md", victim)
        files = self.files()
        files["general.md"] = "# General\n"
        self.assertIsNotNone(known.write_folder(self.project, files).skipped)
        known.remove_folder(self.project)
        self.assertEqual("export IMPORTANT=1\n", victim.read_text(encoding="utf-8"))
        self.assertTrue((planted / "general.md").is_symlink())

    def test_a_linked_ignore_file_is_never_written_through(self) -> None:
        # A dangling .ignore link made KnowItAll2 create the file it points to, such as ~/.bash_profile.
        missing = self.root / "outside" / "created.txt"
        missing.parent.mkdir()
        link_file(self.project / known.IGNORE_FILE, missing)
        result = known.write_folder(self.project, self.files())
        self.assertIsNone(result.skipped)
        self.assertTrue((self.project / known.FOLDER / known.INDEX).is_file())
        self.assertFalse(missing.exists())
        self.assertTrue(any(known.IGNORE_FILE in note for note in result.notes))

    def test_files_replace_what_is_there_instead_of_writing_into_it(self) -> None:
        known.write_folder(self.project, self.files())
        index = self.project / known.FOLDER / known.INDEX
        with mock.patch("knowitall2.known.write_text_atomic", wraps=known.write_text_atomic) as writes:
            files = self.files()
            files[known.INDEX] += "changed\n"
            known.write_folder(self.project, files)
        self.assertTrue(writes.called)
        self.assertTrue(all(call.kwargs.get("follow_links") is False for call in writes.call_args_list
                            if Path(call.args[0]).parent == index.parent), writes.call_args_list)

    @unittest.skipUnless(GIT, "needs git")
    def test_a_folder_git_tracks_is_the_repositorys_own(self) -> None:
        repo = self.root / "cloned"
        run = lambda *args: subprocess.run([GIT, *args], cwd=repo, check=True, capture_output=True, text=True)
        repo.mkdir()
        run("init", "-q")
        planted = repo / known.FOLDER
        planted.mkdir()
        (planted / known.INDEX).write_text(known.MARKER + "\n", encoding="utf-8")
        (planted / "CONTRIBUTING.md").write_text("tracked\n", encoding="utf-8")
        run("add", "-A")
        run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
        result = known.write_folder(repo, self.files())
        self.assertIn("Git tracks", result.skipped)
        known.remove_folder(repo)
        self.assertEqual("", run("status", "--porcelain", "--ignored").stdout)

    @unittest.skipUnless(GIT, "needs git")
    def test_an_ignore_file_git_tracks_is_never_changed_or_removed(self) -> None:
        repo = self.root / "tracked-ignore"
        run = lambda *args: subprocess.run([GIT, *args], cwd=repo, check=True, capture_output=True, text=True)
        repo.mkdir()
        run("init", "-q")
        (repo / known.IGNORE_FILE).write_bytes(known.IGNORE_TEXT.encode("utf-8"))
        run("add", "-A")
        run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
        self.assertIsNone(known.write_folder(repo, self.files()).skipped)
        self.assertTrue((repo / known.FOLDER / "synology.md").is_file())
        known.remove_folder(repo)
        self.assertFalse((repo / known.FOLDER).exists())
        self.assertEqual("", run("status", "--porcelain").stdout)

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


def text_of(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


class WorkedInTests(KnownTestCase):
    """Knowledge files go only where agents work: per project, the clone worked in last and its worktrees."""

    def worktree(self, main: Path, tree: Path) -> Path:
        git_dir = main / ".git" / "worktrees" / tree.name
        git_dir.mkdir(parents=True)
        (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
        (git_dir / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
        tree.mkdir(parents=True)
        (tree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
        return tree

    def test_only_the_clone_worked_in_last_and_its_worktrees_get_the_files(self) -> None:
        # Review 2026-10-11: projects moved to another folder still got files in their old copies.
        self.remember("Alpha deploys with make deploy.", scope="project")
        old = self.project
        new = make_repository(self.root / "moved" / "alpha", "https://gitlab.example.com/team/alpha.git")
        tree = self.worktree(new, self.root / "moved" / "alpha-feature")
        for folder in (old, new, tree):
            self.memory.project_for(folder)
        known.worked_in(old)
        self.clock.value = "2026-09-28T09:00:00Z"
        known.worked_in(tree / "src")
        self.clock.value = "2026-09-28T10:00:00Z"
        known.worked_in(new)
        known.refresh(self.connection)
        written = lambda folder: (folder / known.FOLDER / "alpha.md").is_file()
        self.assertEqual([False, True, True], [written(folder) for folder in (old, new, tree)])
        self.clock.value = "2026-09-29T10:00:00Z"
        known.worked_in(old / "docs")   # back in the old clone: it is the one written now
        self.assertTrue(known.due(self.connection))
        self.remember("Alpha's staging host is stage-1.", scope="project")
        known.refresh(self.connection)
        self.assertIn("stage-1", text_of(old / known.FOLDER / "alpha.md"))
        self.assertNotIn("stage-1", text_of(new / known.FOLDER / "alpha.md"))

    def test_going_back_to_another_copy_within_the_hour_is_noted(self) -> None:
        self.remember("Alpha deploys with make deploy.", scope="project")
        new = make_repository(self.root / "moved" / "alpha", "https://gitlab.example.com/team/alpha.git")
        known.worked_in(self.project)
        self.clock.value = "2026-09-27T12:05:00Z"
        known.worked_in(new)
        self.clock.value = "2026-09-27T12:10:00Z"
        known.worked_in(self.project)
        known.worked_in(self.project)
        known.refresh(self.connection)
        self.assertTrue((self.project / known.FOLDER / "alpha.md").is_file())
        self.assertFalse((new / known.FOLDER).exists())

    def test_working_in_two_worktrees_in_turn_starts_no_refresh(self) -> None:
        # Release-candidate review (R-M1): each prompt in the worktree used less lately made it the newest copy,
        # which changed the marker, so a full refresh started after nearly every prompt of parallel work.
        self.remember("Alpha deploys with make deploy.", scope="project")
        tree = self.worktree(self.project, self.root / "alpha-feature")
        known.worked_in(self.project)
        self.clock.value = "2026-09-27T12:01:00Z"
        known.worked_in(tree / "src")
        known.refresh(self.connection)
        written = lambda folder: (folder / known.FOLDER / "alpha.md").is_file()
        self.assertEqual([True, True], [written(self.project), written(tree)])
        for moment in ("12:02", "12:03", "12:30", "13:20", "13:21", "14:40", "14:41"):
            for folder in (self.project, tree):
                self.clock.value = f"2026-09-27T{moment}:00Z"
                known.worked_in(folder)
                self.assertFalse(known.due(self.connection), (moment, folder.name))
        other = make_repository(self.root / "moved" / "alpha", "https://gitlab.example.com/team/alpha.git")
        known.worked_in(other)   # a copy with its own Git folder still changes where the files go
        self.assertTrue(known.due(self.connection))

    def test_the_clone_in_the_data_home_never_gets_files_or_counts_as_the_newest_copy(self) -> None:
        # Release-candidate review (R-L5): the live install's own clone, inside the data home, is a clone of the
        # project worked on; one session there took the files away from the checkout and its worktrees.
        self.remember("Alpha deploys with make deploy.", scope="project")
        app = make_repository(self.root / "home" / "app", "https://gitlab.example.com/team/alpha.git")
        self.clock.value = "2026-09-27T12:05:00Z"
        self.memory.project_for(app)   # seen there later than in the checkout, before the record was seeded
        known.refresh(self.connection)
        self.clock.value = "2026-09-27T12:10:00Z"
        known.worked_in(app / "src")
        self.assertFalse(known.due(self.connection))
        self.remember("Alpha's staging host is stage-1.", scope="project")
        known.refresh(self.connection)
        self.assertIn("stage-1", text_of(self.project / known.FOLDER / "alpha.md"))
        self.assertFalse((app / known.FOLDER).exists())
        self.assertFalse((app / known.IGNORE_FILE).exists())
        self.assertEqual([os.path.normcase(str(self.project.resolve()))], list(known.worked_folders()))
        self.assertEqual("inside KnowItAll2's own data folder, which never gets knowledge files",
                         known.write_folder(app, known.render(self.connection)).skipped)
        self.assertFalse((app / known.FOLDER).exists())

    def test_a_folder_a_tool_call_names_gets_no_files(self) -> None:
        # Review 2026-10-11 (H-M4): one briefing call naming any repository made it get every memory.
        self.remember("The NAS answers on 10.0.0.10.", scope="global")
        known.refresh(self.connection)
        elsewhere = make_repository(self.root / "share" / "repo")
        self.memory.briefing(project_path=elsewhere)
        self.memory.remember("The share's repo builds with make.", scope="project", project_path=elsewhere)
        known.refresh(self.connection)
        self.assertTrue((self.project / known.FOLDER / known.INDEX).is_file())
        self.assertFalse((elsewhere / known.FOLDER).exists())
        self.assertFalse((elsewhere / known.IGNORE_FILE).exists())

    def test_a_folder_no_agent_worked_in_for_30_days_gets_no_more_files(self) -> None:
        self.remember("Alpha deploys with make deploy.", scope="project")
        known.worked_in(self.project)
        known.refresh(self.connection)
        self.assertTrue((self.project / known.FOLDER / "alpha.md").is_file())
        self.clock.value = "2026-10-27T11:00:00Z"
        self.assertFalse(known.due(self.connection))
        self.clock.value = "2026-10-28T12:00:00Z"   # 31 days on
        self.assertTrue(known.due(self.connection))
        self.remember("Alpha's staging host is stage-1.", scope="project")
        known.refresh(self.connection)
        self.assertNotIn("stage-1", text_of(self.project / known.FOLDER / "alpha.md"))
        self.assertFalse((self.project / known.FOLDER).exists())   # taken out once

    def test_a_folder_no_longer_written_has_knowitall2s_files_taken_out_once(self) -> None:
        # The release cleans up the old copies once, and then never touches them again (the user's choice).
        self.remember("Alpha deploys with make deploy.", scope="project")
        old = self.project
        exclude = old / ".git" / "info" / "exclude"
        exclude.parent.mkdir()
        exclude.write_text("*.log\n", encoding="utf-8")
        known.worked_in(old)
        known.refresh(self.connection)
        self.assertTrue((old / known.FOLDER / "alpha.md").is_file())
        new = make_repository(self.root / "moved" / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.clock.value = "2026-09-28T10:00:00Z"
        known.worked_in(new)
        report = known.refresh(self.connection)
        self.assertTrue((new / known.FOLDER / "alpha.md").is_file())
        self.assertFalse((old / known.FOLDER).exists())
        self.assertFalse((old / known.IGNORE_FILE).exists())
        self.assertEqual("*.log\n", exclude.read_text(encoding="utf-8"))
        self.assertIn(f"{os.path.normcase(str(old.resolve()))}: no longer written here", report.describe())
        # Put back by hand: left alone from now on.
        (old / known.FOLDER).mkdir()
        (old / known.FOLDER / known.INDEX).write_text(known.MARKER + "\n", encoding="utf-8")
        self.remember("Alpha's staging host is stage-1.", scope="project")
        known.refresh(self.connection, force=True)
        self.assertEqual(known.MARKER + "\n", (old / known.FOLDER / known.INDEX).read_text(encoding="utf-8"))

    def test_a_folder_written_before_with_a_stub_git_folder_is_cleaned_up(self) -> None:
        # The live case: a .git folder holding only info/ made a parent folder of projects get the files.
        stub = self.root / "projects"
        (stub / ".git" / "info").mkdir(parents=True)
        block = "\n".join([known.EXCLUDE_BEGIN, *known.EXCLUDE_LINES, known.EXCLUDE_END]) + "\n"
        (stub / ".git" / "info" / "exclude").write_text(block, encoding="utf-8")
        (stub / known.IGNORE_FILE).write_text(known.IGNORE_TEXT, encoding="utf-8")
        (stub / known.FOLDER).mkdir()
        (stub / known.FOLDER / known.INDEX).write_text(known.MARKER + "\n", encoding="utf-8")
        (stub / known.FOLDER / "general.md").write_text("# General\n", encoding="utf-8")
        (stub / "notes.txt").write_text("the user's own\n", encoding="utf-8")
        (self.root / "home" / known.STATUS_FILE).write_text(json.dumps({"marker": "old", "folders": [str(stub)]}),
                                                            encoding="utf-8")
        self.remember("The NAS answers on 10.0.0.10.", scope="global")
        known.refresh(self.connection)
        self.assertEqual([".git", "notes.txt"], sorted(path.name for path in stub.iterdir()))
        self.assertEqual("", (stub / ".git" / "info" / "exclude").read_text(encoding="utf-8"))
        self.assertTrue((self.project / known.FOLDER / known.INDEX).is_file())

    def test_the_first_refresh_takes_the_folders_seen_in_the_last_30_days(self) -> None:
        old = make_repository(self.root / "old", "https://gitlab.example.test/team/old.git")
        self.clock.value = "2026-08-01T12:00:00Z"
        Memory(self.store, agent="codex", clock=self.clock).project_for(old)
        self.clock.value = "2026-09-27T12:00:00Z"
        self.remember("Alpha deploys with make deploy.", scope="project")
        fresh = make_repository(self.root / "fresh")
        known.worked_in(fresh)   # a session that started before the first refresh
        known.refresh(self.connection)
        self.assertTrue((self.project / known.FOLDER / known.INDEX).is_file())
        self.assertTrue((fresh / known.FOLDER / known.INDEX).is_file())
        self.assertFalse((old / known.FOLDER).exists())
        record = json.loads((self.root / "home" / known.WORKED_FILE).read_text(encoding="utf-8"))
        self.assertEqual(2, len(record["folders"]))
        self.assertTrue(record["seeded"])


class CleanupTests(KnownTestCase):
    """A folder no longer written keeps KnowItAll2's exclude lines until its folder and .ignore are gone."""

    def setUp(self) -> None:
        super().setUp()
        self.remember("Alpha deploys with make deploy.", scope="project")
        self.exclude = self.project / ".git" / "info" / "exclude"
        known.worked_in(self.project)
        known.refresh(self.connection)
        self.assertTrue((self.project / known.FOLDER / "alpha.md").is_file())
        self.assertIn(f"/{known.IGNORE_FILE}", self.exclude.read_text(encoding="utf-8"))
        # Then the project is worked on in a copy elsewhere: the checkout is cleaned up at the next refresh.
        self.moved = make_repository(self.root / "moved" / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.clock.value = "2026-09-28T10:00:00Z"
        known.worked_in(self.moved)

    def retiring(self) -> list[str]:
        return [os.path.normcase(os.path.realpath(path)) for path in known.read_status().get("retiring", [])]

    def assert_kept(self) -> None:
        """KnowItAll2's exclude lines are all still there, and the cleanup is tried again later."""

        self.assertIn("\n".join([known.EXCLUDE_BEGIN, *known.EXCLUDE_LINES, known.EXCLUDE_END]),
                      self.exclude.read_text(encoding="utf-8"))
        self.assertEqual([os.path.normcase(os.path.realpath(self.project))], self.retiring())

    def assert_cleaned(self) -> None:
        self.assertFalse((self.project / known.FOLDER).exists())
        self.assertFalse((self.project / known.IGNORE_FILE).exists())
        self.assertNotIn(known.EXCLUDE_BEGIN, text_of(self.exclude))
        self.assertEqual([], self.retiring())

    def test_while_git_cannot_run_everything_stays_and_is_tried_again(self) -> None:
        # Release-candidate review (R-M2): the exclude lines went although the folder and .ignore stayed, so
        # every memory about the project showed in git status, ready for a `git add -A`.
        real = known._tracked
        same = lambda root: known._key(Path(root).resolve()) == known._key(self.project.resolve())
        refused = lambda root, git_dir: None if same(root) else real(root, git_dir)
        with mock.patch.object(known, "_tracked", side_effect=refused):
            known.refresh(self.connection)
        self.assertTrue((self.moved / known.FOLDER / "alpha.md").is_file())
        self.assertTrue((self.project / known.FOLDER / "alpha.md").is_file())
        self.assertEqual(known.IGNORE_TEXT, text_of(self.project / known.IGNORE_FILE))
        self.assert_kept()
        known.refresh(self.connection, force=True)   # Git runs again
        self.assert_cleaned()

    def test_a_file_knowitall2_did_not_write_keeps_the_folder_and_the_exclude_lines(self) -> None:
        left = self.project / known.FOLDER / ".README.md.x1y2.tmp"   # such as what a crash leaves
        left.write_text("partial\n", encoding="utf-8")
        report = known.refresh(self.connection)
        self.assertEqual([left.name], [path.name for path in (self.project / known.FOLDER).iterdir()])
        self.assertEqual(known.IGNORE_TEXT, text_of(self.project / known.IGNORE_FILE))
        self.assert_kept()
        self.assertIn("tried again", report.describe())
        known.refresh(self.connection, force=True)
        self.assert_kept()
        left.unlink()
        known.refresh(self.connection, force=True)
        self.assert_cleaned()

    def test_an_ignore_file_that_cannot_be_removed_keeps_the_exclude_lines(self) -> None:
        real = Path.unlink

        def unlink(path: Path, *args, **options) -> None:
            if path.name == known.IGNORE_FILE:
                raise PermissionError(13, "The process cannot access the file because it is being used")
            real(path, *args, **options)

        with mock.patch.object(Path, "unlink", unlink):
            known.refresh(self.connection)
        self.assertFalse((self.project / known.FOLDER).exists())
        self.assertEqual(known.IGNORE_TEXT, text_of(self.project / known.IGNORE_FILE))
        self.assert_kept()
        known.refresh(self.connection, force=True)
        self.assert_cleaned()

    def test_exclude_lines_go_when_the_folder_and_ignore_file_were_never_there(self) -> None:
        other = make_repository(self.root / "beta")
        exclude = other / ".git" / "info" / "exclude"
        exclude.parent.mkdir()
        exclude.write_text("*.log\n" + "\n".join([known.EXCLUDE_BEGIN, *known.EXCLUDE_LINES, known.EXCLUDE_END]) + "\n",
                           encoding="utf-8")
        self.assertEqual([f"removed KnowItAll2's lines from {exclude.resolve()}"], known.remove_folder(other))
        self.assertEqual("*.log\n", exclude.read_text(encoding="utf-8"))


class OwnershipTests(KnownTestCase):
    """A folder KnowItAll2 wrote before is its own by its record in the data home, not only by the marker."""

    def setUp(self) -> None:
        super().setUp()
        self.remember("Alpha deploys with make deploy.", scope="project")
        self.folder = self.project / known.FOLDER
        known.worked_in(self.project)

    def damaged(self, readme: bytes, *, recorded: bool) -> None:
        """The folder as a crash on 2026-10-05 left six live ones: the index empty or zeros, the rest stale."""

        self.folder.mkdir()
        (self.folder / known.INDEX).write_bytes(readme)
        (self.folder / "alpha.md").write_text("# alpha\n\nstale\n", encoding="utf-8")
        if recorded:
            (self.root / "home" / known.STATUS_FILE).write_text(
                json.dumps({"marker": "old", "folders": [str(self.project)]}), encoding="utf-8")

    def test_a_recorded_folder_with_an_empty_or_zeroed_index_is_written_again(self) -> None:
        # Release-candidate review (R-M3): six active projects kept stale files, refused as not KnowItAll2's.
        for readme in (b"", b"\0" * 7255):
            with self.subTest(size=len(readme)):
                self.damaged(readme, recorded=True)
                report = known.refresh(self.connection)
                self.assertEqual([None], [item.skipped for item in report.folders])
                self.assertTrue(known.ours(self.folder))
                self.assertIn("make deploy", text_of(self.folder / "alpha.md"))
                shutil.rmtree(self.folder)

    def test_an_unrecorded_folder_without_the_marker_is_still_refused(self) -> None:
        self.damaged(b"\0" * 7255, recorded=False)
        report = known.refresh(self.connection)
        self.assertIn("not written by KnowItAll2", report.folders[0].skipped)
        self.assertEqual(b"\0" * 7255, (self.folder / known.INDEX).read_bytes())
        self.assertEqual("# alpha\n\nstale\n", text_of(self.folder / "alpha.md"))
        self.assertEqual([{"folder": report.folders[0].root, "reason": "foreign"}], known.read_status()["refused"])

    def test_a_folder_a_crash_zeroed_beside_knowitall2s_own_ignore_file_is_written_again(self) -> None:
        # The six live folders had dropped out of KnowItAll2's record before it kept one: their index is all zeros,
        # and the .ignore beside them is KnowItAll2's own, word for word.
        self.damaged(b"\0" * 7255, recorded=False)
        (self.project / known.IGNORE_FILE).write_text(known.IGNORE_TEXT, encoding="utf-8")
        report = known.refresh(self.connection)
        self.assertIsNone(report.folders[0].skipped)
        self.assertTrue(known.ours(self.folder))
        self.assertFalse((self.folder / "alpha.md").exists() and "stale" in text_of(self.folder / "alpha.md"))

    def test_a_recorded_folder_holding_anything_but_markdown_is_still_refused(self) -> None:
        self.damaged(b"", recorded=True)
        (self.folder / "notes.txt").write_text("the user's own\n", encoding="utf-8")
        report = known.refresh(self.connection)
        self.assertIn("not written by KnowItAll2", report.folders[0].skipped)
        self.assertEqual("# alpha\n\nstale\n", text_of(self.folder / "alpha.md"))
        self.assertEqual(b"", (self.folder / known.INDEX).read_bytes())

    def test_the_index_is_written_last(self) -> None:
        files = known.render(self.connection)
        index_first = {known.INDEX: files[known.INDEX], **{name: text for name, text in files.items() if name != known.INDEX}}
        with mock.patch("knowitall2.known.write_text_atomic", wraps=known.write_text_atomic) as writes:
            known.write_folder(self.project, index_first)
        names = [Path(call.args[0]).name for call in writes.call_args_list if Path(call.args[0]).parent.name == known.FOLDER]
        self.assertEqual(sorted(files), sorted(names))
        self.assertEqual(known.INDEX, names[-1])

    def test_a_new_folder_a_crash_left_without_its_index_is_written_again(self) -> None:
        # With the index written last, a crash before it left files no marker vouched for: the record does.
        class Crash(BaseException):
            pass

        real = known.write_text_atomic

        def crash(path, text, **options):
            if Path(path).name == known.INDEX:
                raise Crash()
            real(path, text, **options)

        with mock.patch("knowitall2.known.write_text_atomic", side_effect=crash), self.assertRaises(Crash):
            known.refresh(self.connection)
        self.assertTrue((self.folder / "alpha.md").is_file())
        self.assertFalse((self.folder / known.INDEX).exists())
        known.refresh(self.connection)
        self.assertTrue(known.ours(self.folder))

    def test_doctor_names_a_folder_the_refresh_refused_and_what_to_do(self) -> None:
        from knowitall2 import doctor
        from knowitall2.agents.base import describe_checks

        self.damaged(b"\0" * 7255, recorded=False)
        beta = make_repository(self.root / "beta")
        known.worked_in(beta)
        self.remember("Beta builds with npm.", scope="project", project_path=beta)
        real = known._tracked
        same = lambda root: known._key(Path(root).resolve()) == known._key(beta.resolve())
        with mock.patch.object(known, "_tracked", side_effect=lambda root, git_dir: None if same(root) else real(root, git_dir)):
            known.refresh(self.connection)
        checks = doctor._refused_folder_checks()
        self.assertEqual(2, len(checks))
        text = describe_checks(checks)
        self.assertTrue(all(line.startswith(("NOTE ", "     fix: ")) for line in text.splitlines()), text)
        foreign = next(check for check in checks if os.path.normcase(str(self.project.resolve())) in os.path.normcase(check.detail))
        self.assertIn("not written by KnowItAll2", foreign.detail)
        self.assertIn("delete", foreign.fix)
        git = next(check for check in checks if check is not foreign)
        self.assertIn(os.path.normcase(str(beta.resolve())), os.path.normcase(git.detail))
        self.assertIn("Git could not be run", git.detail)
        self.assertIn("git -C", git.fix)
        known.set_enabled(False)
        self.assertEqual([], doctor._refused_folder_checks())


class SelectionTests(KnownTestCase):
    """Each project's folder holds its own memories and the shared memories about the systems it uses."""

    def setUp(self) -> None:
        super().setUp()
        self.beta = make_repository(self.root / "beta", "https://gitlab.example.test/team/beta.git")
        self.filed = self.remember("Alpha backs up to the NAS share alpha-backups.", scope="project")
        self.file(self.filed.id, "Synology")
        self.naming = self.remember("Alpha's deploys go through the EdgeRouter first.", scope="project")
        self.own = self.remember("Alpha deploys with make deploy.", scope="project")
        for number in range(3):
            self.file(self.remember(f"The EdgeRouter note {number}: it answers on 10.0.0.1.", scope="global").id,
                      "EdgeRouter")
            self.file(self.remember(f"The office printer note {number}: it takes toner.", scope="global").id, "Printer")
        self.loose = self.remember("Prefer tabs over spaces in shell scripts.", scope="global")
        self.betas = self.remember("Beta's release is signed with the vault entry beta-sign.", scope="project",
                                   project_path=self.beta)
        known.worked_in(self.project)
        known.worked_in(self.beta)
        known.refresh(self.connection)

    def listed(self, root: Path) -> list[str]:
        return sorted(path.name for path in (root / known.FOLDER).iterdir())

    def test_each_project_gets_its_own_files_and_those_of_the_systems_it_uses(self) -> None:
        # Review 2026-10-11: every folder held everything KnowItAll2 knew, from every project.
        self.assertEqual(["README.md", "alpha.md", "edgerouter.md", "general.md", "synology.md"], self.listed(self.project))
        self.assertEqual(["README.md", "beta.md", "general.md"], self.listed(self.beta))
        held = "".join(text_of(path) for path in (self.project / known.FOLDER).iterdir())
        for record in (self.filed, self.naming, self.own, self.loose):
            self.assertIn(record.id, held)
        self.assertNotIn(self.betas.id, held)
        self.assertNotIn("toner", held)

    def test_the_index_lists_only_the_files_present(self) -> None:
        index = text_of(self.project / known.FOLDER / known.INDEX)
        self.assertEqual(known.MARKER, index.splitlines()[0])
        listed = sorted(line.split(":")[0][2:] for line in index.splitlines() if line.startswith("- "))
        self.assertEqual(["alpha.md", "edgerouter.md", "general.md", "synology.md"], listed)

    def test_a_pointer_goes_only_to_a_file_in_the_projects_folder(self) -> None:
        folder = self.project / known.FOLDER
        self.assertEqual([], known.find_pointers(self.connection, ["the Printer is jammed again"], folder, []))
        self.assertEqual(["edgerouter.md"], [target for target, _ in known.find_pointers(
            self.connection, ["the EdgeRouter dropped the route"], folder, [])])


class PointerTests(KnownTestCase):
    def systems(self, *, memories: int = 3):
        """Three systems this project uses, each with enough memories that a pointer to it is worth showing."""

        for system, aliases, words in (("Synology", ["nas-file1", "NAS", "shared-name"], "the NAS"),
                                       ("gw-control", ["control host", "shared-name"], "the gateway"),
                                       ("Hermes", ["gw-control"], "the helper runner")):
            for number in range(memories):
                saved = self.remember(f"Note {number} about {words}: sudo -u hermes, step {number}.", scope="project")
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
        tiny = self.remember("Linux note.", scope="project")
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
            saved = self.remember(f"KnowItAll2 note {number}: run its update script.", scope="project")
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
        self.assertIn(f"What KnowItAll2 knows about this project and the systems it uses is also in {known.FOLDER}/",
                      self.memory.briefing(project_path=self.project))

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
                                      scope="project", project_path=self.alpha).record
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

    def test_the_hooks_note_the_folder_the_agent_works_in(self) -> None:
        # Only a folder an agent session runs in counts, as both agents' hooks report it.
        from knowitall2 import hooks

        beta = make_repository(self.root / "work" / "beta")
        gamma = make_repository(self.root / "work" / "gamma")
        (beta / "src").mkdir()
        (beta / "src" / "main.py").write_text("print('beta')\n", encoding="utf-8")
        hooks.session_start(json.dumps({"session_id": self.session, "transcript_path": str(self.log),
                                        "cwd": str(beta / "src"), "source": "startup"}), start_learner=lambda: None)
        hooks.prompt_submit(json.dumps({"session_id": "codex-chat", "cwd": str(gamma), "prompt": "hello",
                                        "hook_event_name": "UserPromptSubmit"}), agent="codex")
        noted = known.worked_folders()
        for folder in (beta, gamma):
            self.assertIn(os.path.normcase(str(folder.resolve())), noted)
        self.assertNotIn(os.path.normcase(str((beta / "src").resolve())), noted)

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
