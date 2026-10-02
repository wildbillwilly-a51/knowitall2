import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (puts src on the path)

from knowitall2.cli import main
from knowitall2.learning.extractor import (
    OUTPUT_SCHEMA,
    CodexCliExtractor,
    ExtractionError,
    codex_default_model,
    codex_engine_environment,
    find_codex_cli,
    strict_schema,
)
from knowitall2.learning.state import LearnerSettings, load_settings, save_settings


def answering(payload: object, *, returncode: int = 0, stderr: bytes = b"", calls: list | None = None):
    """A fake runner that writes ``payload`` where ``codex exec -o`` would, and records the call."""

    def runner(command, **options):
        if calls is not None:
            calls.append((command, options))
        if payload is not None:
            Path(command[command.index("-o") + 1]).write_text(
                payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, returncode, b"", stderr)

    return runner


class CodexEngineTests(unittest.TestCase):
    def test_the_call_is_sealed_and_reads_the_answer_file(self) -> None:
        calls = []
        engine = CodexCliExtractor(Path("codex.exe"), model="gpt-light", runner=answering({"memories": []}, calls=calls))
        self.assertEqual([], engine.extract(mock.Mock(text="session text", known=[])))
        command, options = calls[0]
        for flag in ("exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
            self.assertIn(flag, command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertIn("hooks", command[command.index("--disable") + 1])
        self.assertIn('approval_policy="never"', command)
        self.assertEqual("gpt-light", command[command.index("-m") + 1])
        self.assertEqual("-", command[-1])
        instructions = [part for part in command if part.startswith("developer_instructions=")][0]
        self.assertIn("Do not run commands", json.loads(instructions.split("=", 1)[1]))
        self.assertEqual(b"session text", options["input"])
        self.assertEqual(str(Path(command[command.index("-C") + 1])), options["cwd"])

    def test_the_schema_drops_keywords_structured_output_rejects(self) -> None:
        self.assertNotIn("maxItems", json.dumps(strict_schema(OUTPUT_SCHEMA)))
        self.assertIn("additionalProperties", json.dumps(strict_schema(OUTPUT_SCHEMA)))
        self.assertIn("maxItems", json.dumps(OUTPUT_SCHEMA))

    def test_failures_become_extraction_errors(self) -> None:
        cases = [
            (answering(None, returncode=1, stderr=b"Error: Not logged in. Run codex login."), True),
            (answering(None, returncode=1, stderr=b"You've hit your usage limit."), True),
            (answering(None, returncode=1, stderr=b"stream disconnected"), False),
            (answering("not json"), False),
            (answering(None), False),
        ]
        for runner, blocking in cases:
            with self.subTest(blocking=blocking), self.assertRaises(ExtractionError) as caught:
                CodexCliExtractor(Path("codex.exe"), runner=runner).run("text", schema=OUTPUT_SCHEMA, system_prompt="p")
            self.assertEqual(blocking, caught.exception.blocking)

    def test_a_timeout_blames_the_session(self) -> None:
        def slow(command, **options):
            raise subprocess.TimeoutExpired(command, 1)

        with self.assertRaises(ExtractionError) as caught:
            CodexCliExtractor(Path("codex.exe"), runner=slow).run("text", schema=OUTPUT_SCHEMA, system_prompt="p")
        self.assertFalse(caught.exception.blocking)

    def test_the_engine_does_not_inherit_a_codex_sessions_variables(self) -> None:
        environment = codex_engine_environment({
            "PATH": "C:\\Windows", "CODEX_HOME": "C:\\Users\\me\\.codex", "CODEX_THREAD_ID": "abc",
            "CODEX_SANDBOX_NETWORK_DISABLED": "1", "OPENAI_API_KEY": "users-own-key",
        })
        self.assertEqual(
            {"PATH": "C:\\Windows", "CODEX_HOME": "C:\\Users\\me\\.codex", "OPENAI_API_KEY": "users-own-key"},
            environment,
        )

    def test_the_newest_codex_engine_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary)
            for folder in ("old", "new"):
                (local / "OpenAI" / "Codex" / "bin" / folder).mkdir(parents=True)
                (local / "OpenAI" / "Codex" / "bin" / folder / "codex.exe").write_bytes(b"")
            versions = {"old": b"codex-cli 0.144.6\n", "new": b"codex-cli 0.158.0-alpha.2.1\n", "path": b"codex-cli 0.150.1\n"}

            def runner(command, **options):
                key = "path" if command[0] == "C:\\on-path\\codex.exe" else Path(command[0]).parent.name
                return subprocess.CompletedProcess(command, 0, versions[key], b"")

            # A real Codex in the Linux install locations must not take part.
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temporary}), \
                    mock.patch("shutil.which", return_value="C:\\on-path\\codex.exe"), \
                    mock.patch("knowitall2.learning.extractor._posix_locations", return_value=[]):
                found = find_codex_cli(runner=runner)
        self.assertEqual("new", found.parent.name)

    def test_the_default_model_is_codexs_listed_light_model(self) -> None:
        catalog = {"models": [
            {"slug": "big", "visibility": "list", "priority": 1, "description": "Frontier intelligence."},
            {"slug": "light", "visibility": "list", "priority": 3, "description": "Fast and affordable model."},
            {"slug": "hidden", "visibility": "hide", "priority": 2, "description": "Fast and affordable."},
            {"slug": "old-light", "visibility": "list", "priority": 8, "description": "Older fast model."},
        ]}

        def runner(command, **options):
            return subprocess.CompletedProcess(command, 0, json.dumps(catalog).encode(), b"")

        # Without an everyday model, the fast one.
        self.assertEqual("light", codex_default_model(Path("codex.exe"), runner=runner))
        catalog["models"].append({"slug": "sol", "visibility": "list", "priority": 2,
                                  "description": "Workhorse model for coding and everyday work."})
        self.assertEqual("sol", codex_default_model(Path("codex.exe"), runner=runner))
        broken = lambda command, **options: subprocess.CompletedProcess(command, 1, b"oops", b"")  # noqa: E731
        self.assertIsNone(codex_default_model(Path("codex.exe"), runner=broken))

    def test_learning_calls_use_medium_effort_by_default(self) -> None:
        from knowitall2.learning.extractor import CodexCliExtractor

        command = CodexCliExtractor(Path("codex.exe"), model="sol").command(
            workspace=Path("w"), schema_path=Path("s"), answer_path=Path("a"), system_prompt="x")
        self.assertIn('model_reasoning_effort="medium"', command)


@unittest.skipIf(sys.platform == "win32", "Linux install locations")
class LinuxEngineLocationTests(unittest.TestCase):
    def test_engines_in_user_install_folders_are_found_without_path(self) -> None:
        from knowitall2.learning.extractor import find_claude_cli

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".local" / "bin").mkdir(parents=True)
            for name in ("claude", "codex"):
                engine = home / ".local" / "bin" / name
                engine.write_text("#!/bin/sh\necho 'codex-cli 0.158.0'\n", encoding="utf-8")
                engine.chmod(0o755)
            with mock.patch("shutil.which", return_value=None), mock.patch.object(Path, "home", return_value=home), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": "", "APPDATA": ""}):
                self.assertEqual(home / ".local" / "bin" / "claude", find_claude_cli())
                self.assertEqual(home / ".local" / "bin" / "codex", find_codex_cli())


class CatalogTurnTests(unittest.TestCase):
    def test_the_catalog_waits_while_another_computer_tidies_up(self) -> None:
        from contextlib import contextmanager

        from knowitall2.learning import command

        @contextmanager
        def not_ours():
            yield False

        settings = mock.Mock(enabled=True, backend="claude-cli", model=None)
        output = io.StringIO()
        with mock.patch.object(command, "load_settings", return_value=settings), \
                mock.patch.object(command, "build_engine", return_value=object()), \
                mock.patch.object(command, "maintenance_turn", not_ours), \
                mock.patch.object(command, "run_catalog") as catalog, redirect_stdout(output):
            self.assertEqual(command.run_catalog_command(mock.Mock(dry_run=False)), 0)
        catalog.assert_not_called()
        self.assertIn("Another computer", output.getvalue())


class DesktopEngineLocationTests(unittest.TestCase):
    """The Claude desktop app's own engine folders, whatever their depth."""

    def find(self, appdata: Path, *, home: Path | None = None, platform: str = "win32") -> Path | None:
        from knowitall2.learning import extractor

        with mock.patch("shutil.which", return_value=None), \
                mock.patch.object(extractor, "_posix_locations", return_value=[]), \
                mock.patch.object(Path, "home", return_value=home or appdata / "no-home"), \
                mock.patch.object(extractor.sys, "platform", platform), \
                mock.patch.dict(os.environ, {"APPDATA": str(appdata), "LOCALAPPDATA": ""}):
            return extractor.find_claude_cli()

    def test_anthropics_installer_location_comes_before_the_desktop_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.engine(root / "Claude" / "claude-code" / "2.1.286" / "635c1867224a", recorded=8)
            official = self.engine(root / "home" / ".local" / "bin")
            self.assertEqual(official, self.find(root, home=root / "home"))

    def engine(self, folder: Path, *, size: int = 8, recorded: int | None = None) -> Path:
        folder.mkdir(parents=True)
        engine = folder / "claude.exe"
        engine.write_bytes(b"x" * size)
        if recorded is not None:
            (folder / ".payload").write_text(json.dumps({"sha256": "0" * 64, "size": recorded}), encoding="utf-8")
        return engine

    def test_engines_one_folder_deeper_are_found(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Claude" / "claude-code"
            self.engine(root / "2.1.284" / "3f4bed3e44ad", recorded=8)
            newest = self.engine(root / "2.1.286" / "635c1867224a", recorded=8)
            self.assertEqual(Path(os.path.realpath(newest)), self.find(Path(temporary)))

    def test_the_older_layout_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = self.engine(Path(temporary) / "Claude" / "claude-code" / "2.1.200")
            self.assertEqual(Path(os.path.realpath(engine)), self.find(Path(temporary)))

    def test_an_incomplete_download_is_passed_over(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Claude" / "claude-code"
            complete = self.engine(root / "2.1.284" / "aaaa", recorded=8)
            self.engine(root / "2.1.286" / "bbbb", size=3, recorded=8)
            self.assertEqual(Path(os.path.realpath(complete)), self.find(Path(temporary)))

    def test_nothing_found_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(self.find(Path(temporary)))


class BackendChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": self.temporary.name})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def enable(self, *arguments: str, claude=None, codex=None) -> str:
        output = io.StringIO()
        with mock.patch("knowitall2.learning.command.find_claude_cli", return_value=claude), \
                mock.patch("knowitall2.learning.command.find_codex_cli", return_value=codex), \
                mock.patch("knowitall2.learning.command.codex_default_model", return_value="light"), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            main(["learn", "--enable", *arguments])
        return output.getvalue()

    def test_an_installed_engine_is_chosen_when_the_current_one_is_missing(self) -> None:
        output = self.enable(codex=Path("codex.exe"))
        self.assertIn("Learning is on: backend codex-cli, model light.", output)
        settings = load_settings()
        self.assertEqual(("codex-cli", "light", True), (settings.backend, settings.model, settings.enabled))

    def test_the_backend_can_be_chosen_and_keeps_its_own_default_model(self) -> None:
        self.enable(claude=Path("claude.exe"), codex=Path("codex.exe"))
        self.assertEqual(("claude-cli", "sonnet"), (load_settings().backend, load_settings().model))
        self.enable("--backend", "codex-cli", claude=Path("claude.exe"), codex=Path("codex.exe"))
        self.assertEqual(("codex-cli", "light"), (load_settings().backend, load_settings().model))
        self.enable("--backend", "claude-cli", "--model", "sonnet", claude=Path("claude.exe"))
        self.assertEqual(("claude-cli", "sonnet"), (load_settings().backend, load_settings().model))

    def test_no_engine_is_reported(self) -> None:
        self.assertIn("Warning: no Claude Code engine was found", self.enable())

    def test_an_update_moves_learning_off_a_former_default_model_once(self) -> None:
        from knowitall2.learning.command import adopt_current_model

        save_settings(LearnerSettings(enabled=True, backend="claude-cli", model="haiku"))
        self.assertEqual("sonnet", adopt_current_model())
        self.assertEqual("sonnet", load_settings().model)
        self.assertIsNone(adopt_current_model())
        save_settings(LearnerSettings(enabled=True, backend="claude-cli", model="opus"))
        self.assertIsNone(adopt_current_model())  # a model chosen on purpose stays
        listed = [{"slug": "luna", "description": "Fast and affordable model."},
                  {"slug": "sol", "description": "Workhorse model."}]
        with mock.patch("knowitall2.learning.command.find_codex_cli", return_value=Path("codex.exe")), \
                mock.patch("knowitall2.learning.command.codex_default_model", return_value="sol"), \
                mock.patch("knowitall2.learning.command.codex_listed_models", return_value=listed):
            save_settings(LearnerSettings(enabled=True, backend="codex-cli", model="luna"))
            self.assertEqual("sol", adopt_current_model())
            save_settings(LearnerSettings(enabled=True, backend="codex-cli", model="astra"))
            self.assertIsNone(adopt_current_model())


if __name__ == "__main__":
    unittest.main()
