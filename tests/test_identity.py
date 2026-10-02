import tempfile
import unittest
from pathlib import Path

from _support import make_repository

from knowitall2.identity import find_git_root, identify, normalize_remote


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_remote_forms_normalize_to_one_identity(self) -> None:
        forms = [
            "https://gitlab.example.com/Group/Repo.git",
            "git@gitlab.example.com:group/repo.git",
            "ssh://git@gitlab.example.com:2222/group/repo",
            "https://user:secret@gitlab.example.com/group/repo/",
        ]
        self.assertEqual({"gitlab.example.com/group/repo"}, {normalize_remote(form) for form in forms})

    def test_moved_renamed_and_cloned_copies_share_a_project(self) -> None:
        first = make_repository(self.root / "one" / "repo", "https://gitlab.example.com/group/Repo.git")
        second = make_repository(self.root / "elsewhere" / "renamed", "git@gitlab.example.com:group/repo.git")
        from_subfolder = identify(first / "src" / "deep")
        other = identify(second)
        self.assertIsNotNone(from_subfolder)
        self.assertIsNotNone(other)
        self.assertEqual(from_subfolder.id, other.id)
        self.assertEqual("Repo", from_subfolder.name)
        self.assertNotEqual(from_subfolder.path_key, other.path_key)

    def test_worktree_resolves_the_shared_config(self) -> None:
        main = make_repository(self.root / "main", "https://gitlab.example.com/group/repo.git")
        worktree_git = main / ".git" / "worktrees" / "feature"
        worktree_git.mkdir(parents=True)
        (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
        tree = self.root / "feature"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
        self.assertEqual(identify(main).id, identify(tree).id)

    def test_another_remote_is_used_when_there_is_no_origin(self) -> None:
        repo = make_repository(self.root / "repo", "https://gitlab.example.com/group/repo.git", remote_name="backup")
        self.assertEqual("gitlab.example.com/group/repo", identify(repo).remote)

    def test_repository_without_a_remote_is_identified_by_path(self) -> None:
        local = identify(make_repository(self.root / "local-only"))
        other = identify(make_repository(self.root / "other-local"))
        self.assertIsNone(local.remote)
        self.assertEqual("local-only", local.name)
        self.assertNotEqual(local.id, other.id)

    def test_an_empty_folder_left_inside_another_repository_has_no_project(self) -> None:
        # The live case: a project moved out of a folder inside a larger repository and left it empty.
        parent = make_repository(self.root / "Projects", "https://gitlab.example.com/group/projects.git")
        left = parent / "moved-away"
        left.mkdir()
        self.assertIsNone(identify(left))
        (left / "notes.md").write_text("still here\n", encoding="utf-8")
        self.assertEqual(identify(parent).id, identify(left).id)  # a folder with content is part of the repo
        self.assertEqual(identify(parent).id, identify(parent / "not-made-yet").id)
        empty_repo = make_repository(parent / "fresh")
        self.assertEqual(empty_repo.resolve(), find_git_root(empty_repo))  # a repository root is never "empty"

    def test_folder_outside_git_has_no_project(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        if find_git_root(plain) is not None:
            self.skipTest("the temporary folder is inside a Git repository")
        self.assertIsNone(identify(plain))


if __name__ == "__main__":
    unittest.main()
