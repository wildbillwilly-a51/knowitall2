import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _support import make_repository

from knowitall2 import __version__
from knowitall2.cli import main


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.home = root / "home"
        self.project = make_repository(root / "alpha", "https://gitlab.example.com/team/alpha.git")
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.home)})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(list(arguments))
        return code, output.getvalue(), errors.getvalue()

    def test_output_to_a_pipe_is_utf8(self) -> None:
        # An agent reads the command line's output through a pipe, as UTF-8; on Windows a pipe got the ANSI code
        # page, so "é" arrived as a byte UTF-8 has no letter for and "山田" as "山田".
        text = "José's café is on the Grand-Rue → 山田 keeps the key."
        environment = {name: value for name, value in os.environ.items()
                       if name not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        for arguments in (["remember", text, "--project-path", str(self.project)],
                          ["recall", "café key", "--project-path", str(self.project)]):
            done = subprocess.run([sys.executable, "-B", "-m", "knowitall2", *arguments], capture_output=True,
                                  env=environment, cwd=str(self.project), timeout=60)
            self.assertEqual(0, done.returncode, done.stderr.decode("utf-8", errors="replace"))
        self.assertIn(text, done.stdout.decode("utf-8"))

    def test_remember_recall_brief_and_stats(self) -> None:
        project = str(self.project)
        code, output, _ = self.run_cli(
            "remember", "Vaultwarden holds all credentials.", "--kind", "rule", "--source", "user",
            "--project-path", project,
        )
        self.assertEqual(0, code)
        self.assertIn("Saved [k-", output)
        code, output, _ = self.run_cli("recall", "vaultwarden", "--project-path", project)
        self.assertEqual(0, code)
        self.assertIn("stated by the user via cli", output)
        code, output, _ = self.run_cli("brief", "--project-path", project)
        self.assertIn("Your rules:", output)
        code, output, _ = self.run_cli("stats")
        self.assertIn("1 active", output)
        self.assertIn(str(self.home), output)
        self.assertTrue((self.home / "knowitall2.db").is_file())

    def test_move_files_memories_under_another_project_and_system(self) -> None:
        from knowitall2.paths import database_path
        from knowitall2.store import Store

        beta = make_repository(Path(self.temporary.name) / "beta", "https://gitlab.example.com/team/beta.git")
        self.run_cli("brief", "--project-path", str(beta))  # KnowItAll2 has seen project beta
        _, output, _ = self.run_cli("remember", "Beta ships with make release.", "--scope", "project",
                                    "--project-path", str(self.project))
        record_id = output.split("[", 1)[1].split("]", 1)[0]
        store = Store.open(database_path())
        store.upsert_system(system_id="sys-old", name="old", area="Other", kind="project", aliases=[], now="2026-09-30")
        store.upsert_system(system_id="sys-beta", name="Beta app", area="Other", kind="software", aliases=["beta"],
                            now="2026-09-30")
        store.set_note(record_id, headline="Ships with make", system_id="sys-old", facet="howto", written_by="t",
                       now="2026-09-30")
        store.close()
        code, output, _ = self.run_cli("move", record_id, "k-0000000000", "--project", "BETA", "--system", "beta")
        self.assertEqual(0, code)
        self.assertIn(f"Moved [{record_id}] from project alpha to project beta.", output)
        self.assertIn("Not moved: There is no memory [k-0000000000].", output)
        store = Store.open(database_path())
        self.assertEqual("sys-beta", store.notes_for([record_id])[record_id]["system_id"])
        store.close()
        code, _, errors = self.run_cli("move", record_id, "--project", "gamma")
        self.assertEqual(1, code)
        self.assertIn("no single project named 'gamma'", errors)

    def test_remember_counts_as_the_users_words_only_when_told(self) -> None:
        # Agents run this command too, so a memory is a guess unless --source says otherwise.
        project = str(self.project)
        code, _, _ = self.run_cli("remember", "The NAS is nas01.", "--project-path", project)
        self.assertEqual(0, code)
        _, output, _ = self.run_cli("recall", "nas01", "--project-path", project)
        self.assertIn("unverified via cli", output)
        code, output, errors = self.run_cli("remember", "Always tag releases.", "--kind", "rule",
                                            "--project-path", project)
        self.assertEqual((1, ""), (code, output))
        self.assertIn("pass --source user", errors)

    def test_errors_exit_nonzero_with_a_message(self) -> None:
        code, output, errors = self.run_cli("remember", "password=hunter22")
        self.assertEqual(1, code)
        self.assertEqual("", output)
        self.assertIn("never stores secrets", errors)

    def test_version(self) -> None:
        self.assertEqual((0, f"knowitall2 {__version__}\n", ""), self.run_cli("version"))


if __name__ == "__main__":
    unittest.main()
