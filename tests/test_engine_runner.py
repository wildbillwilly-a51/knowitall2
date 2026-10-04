"""The learning engine as a child process: how it is found, started, fed, and stopped."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (puts src on the path)

from knowitall2.learning.extractor import (
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    ClaudeCliExtractor,
    CodexCliExtractor,
    ExtractionError,
    codex_listed_models,
    find_claude_cli,
    find_codex_cli,
)

WINDOWS = sys.platform == "win32"
# The interpreter itself, not a virtual environment's launcher, so the fake engine's parent is the test.
PYTHON = Path(getattr(sys, "_base_executable", sys.executable))

# Records how it was started; with KNOWITALL2_FAKE_ENGINE_HANG set, it starts a child that holds the
# output pipes, and both wait far past any test's time limit without reading their input.
FAKE_ENGINE = r'''
import json, os, subprocess, sys, time

record = os.environ["KNOWITALL2_FAKE_ENGINE_RECORD"]
args = sys.argv[1:]
if os.environ.get("KNOWITALL2_FAKE_ENGINE_HANG"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], stdin=subprocess.DEVNULL,
                             stdout=sys.stdout, stderr=sys.stderr)
    with open(record, "w", encoding="utf-8") as handle:
        json.dump({"pids": [os.getpid(), child.pid]}, handle)
    time.sleep(20)
    sys.exit(0)
prompt = None
if "--system-prompt-file" in args:
    with open(args[args.index("--system-prompt-file") + 1], encoding="utf-8") as handle:
        prompt = handle.read()
elif "--system-prompt" in args:
    prompt = args[args.index("--system-prompt") + 1]
text = sys.stdin.buffer.read().decode("utf-8")
with open(record, "w", encoding="utf-8") as handle:
    json.dump({"argv": args, "prompt": prompt, "stdin": text, "parent": os.getppid()}, handle)
sys.stdout.write(json.dumps({"structured_output": {"memories": []}}))
'''


def write_npm_script(folder: Path, name: str, target: str, *, program: str = "node") -> Path:
    """The command script npm writes for ``name`` (cmd-shim's template), running ``target`` with ``program``."""

    folder.mkdir(parents=True, exist_ok=True)
    script = folder / f"{name}.cmd"
    target = target.replace("/", "\\")
    script.write_bytes((
        "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\nCALL :find_dp0\r\n"
        "\r\n"
        f'IF EXIST "%dp0%\\{program}.exe" (\r\n'
        f'  SET "_prog=%dp0%\\{program}.exe"\r\n'
        ") ELSE (\r\n"
        f'  SET "_prog={program}"\r\n'
        "  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n"
        ")\r\n"
        "\r\n"
        "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "
        f'"%_prog%"  "%dp0%\\node_modules\\{target}" %*\r\n'
    ).encode("ascii"))
    return script


def write_own_script(folder: Path, command: str) -> Path:
    """A ``claude.cmd`` that is not npm's (it sets a variable of its own), so it runs through cmd.exe as it is."""

    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "claude.cmd"
    script.write_bytes(f"@echo off\r\nset KNOWITALL2_OWN_SCRIPT=1\r\n{command} %*\r\n".encode("ascii"))
    return script


def write_fake_engine(folder: Path) -> Path:
    """An engine that records how it was started: npm's script around Python on Windows, the script elsewhere."""

    if WINDOWS:
        script = folder / "node_modules" / "fake" / "cli.py"
        script.parent.mkdir(parents=True)
        script.write_text(FAKE_ENGINE, encoding="utf-8")
        return write_npm_script(folder, "claude", "fake/cli.py", program=PYTHON.stem)
    folder.mkdir(parents=True, exist_ok=True)
    engine = folder / "claude"
    engine.write_text(f"#!{sys.executable}\n{FAKE_ENGINE}", encoding="utf-8")
    engine.chmod(0o755)
    return engine


def running(pid: int) -> bool:
    if WINDOWS:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # A process that has ended but is not yet collected by its parent no longer runs.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def still_running(pids: list[int], *, within: float = 3.0) -> list[int]:
    deadline = time.monotonic() + within
    while True:
        alive = [pid for pid in pids if running(pid)]
        if not alive or time.monotonic() > deadline:
            return alive
        time.sleep(0.1)


def stop(pids: list[int]) -> None:
    for pid in pids:
        if not running(pid):
            continue
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, signal.SIGKILL)


class FakeEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.engine = write_fake_engine(root / "engine")
        self.record = root / "record.json"
        environment = mock.patch.dict(os.environ, {
            "KNOWITALL2_FAKE_ENGINE_RECORD": str(self.record),
            # npm's script runs its program by name, as it would run node.
            "PATH": str(PYTHON.parent) + os.pathsep + os.environ.get("PATH", ""),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def started(self, engine: Path) -> dict:
        """What the fake engine recorded of a learning call."""

        extractor = ClaudeCliExtractor(engine, effort="high")
        self.assertEqual([], extractor.extract(mock.Mock(text="session text é", known=[])))
        record = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(SYSTEM_PROMPT, record["prompt"])
        self.assertEqual("high", record["argv"][record["argv"].index("--effort") + 1])
        self.assertEqual("session text é", record["stdin"])
        return record

    @unittest.skipUnless(WINDOWS, "npm's command scripts run through cmd.exe on Windows")
    def test_npms_script_gets_the_whole_multi_line_system_prompt_as_an_argument(self) -> None:
        record = self.started(self.engine)
        self.assertEqual(SYSTEM_PROMPT, record["argv"][record["argv"].index("--system-prompt") + 1])
        self.assertNotIn("--system-prompt-file", record["argv"])
        # Started directly with node (here Python), not through cmd.exe.
        self.assertEqual(os.getpid(), record["parent"])

    @unittest.skipUnless(WINDOWS, "command scripts run through cmd.exe on Windows")
    def test_a_script_run_through_cmd_gets_the_system_prompt_in_a_file(self) -> None:
        folder = Path(self.temporary.name) / "own"
        folder.mkdir()
        (folder / "cli.py").write_text(FAKE_ENGINE, encoding="utf-8")
        record = self.started(write_own_script(folder, f'"{PYTHON}" "%~dp0\\cli.py"'))
        self.assertIn("--system-prompt-file", record["argv"])
        self.assertNotIn("--system-prompt", record["argv"])
        self.assertNotEqual(os.getpid(), record["parent"])  # cmd.exe

    def assert_a_timeout_stops_the_whole_tree(self) -> None:
        for engine_class in (ClaudeCliExtractor, CodexCliExtractor):
            with self.subTest(engine=engine_class.name):
                self.record.unlink(missing_ok=True)
                started = time.monotonic()
                with self.assertRaises(ExtractionError) as caught:
                    # More input than a pipe holds, which the engine never reads.
                    engine_class(self.engine, timeout=2).run("x" * 300_000, schema=OUTPUT_SCHEMA, system_prompt="p")
                elapsed = time.monotonic() - started
                pids = json.loads(self.record.read_text(encoding="utf-8"))["pids"]
                self.addCleanup(stop, pids)
                self.assertIn("timed out", str(caught.exception))
                self.assertFalse(caught.exception.blocking)
                self.assertLess(elapsed, 5)
                self.assertEqual([], still_running(pids))

    def test_a_timeout_stops_the_engine_and_everything_it_started(self) -> None:
        with mock.patch.dict(os.environ, {"KNOWITALL2_FAKE_ENGINE_HANG": "1"}):
            self.assert_a_timeout_stops_the_whole_tree()

    @unittest.skipUnless(WINDOWS, "job objects are Windows only")
    def test_the_engine_runs_in_a_job_object(self) -> None:
        from knowitall2.learning import runner

        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        self.addCleanup(process.wait)
        job = runner._job_for(process)
        self.addCleanup(stop, [process.pid])
        self.assertIsNotNone(job)
        self.assertTrue(job.terminate())
        job.close()
        self.assertEqual(1, process.wait(timeout=5))

    @unittest.skipUnless(WINDOWS, "job objects are Windows only")
    def test_without_a_job_object_the_whole_tree_is_still_stopped(self) -> None:
        with mock.patch.dict(os.environ, {"KNOWITALL2_FAKE_ENGINE_HANG": "1"}), \
                mock.patch("knowitall2.learning.runner._job_for", return_value=None):
            self.assert_a_timeout_stops_the_whole_tree()


class SystemPromptTests(unittest.TestCase):
    """The documented --system-prompt argument, except through cmd.exe, which would end it at its first newline."""

    def runner(self, seen: dict):
        def run(command, **options):
            seen.update(command=command, cwd=options["cwd"], files=list(Path(options["cwd"]).iterdir()))
            if "--system-prompt-file" in command:
                prompt = Path(command[command.index("--system-prompt-file") + 1])
                seen.update(prompt=prompt, text=prompt.read_bytes().decode("utf-8"))
            return subprocess.CompletedProcess(command, 0, b'{"structured_output": {"memories": []}}', b"")

        return run

    def calls(self, engine: Path, project: Path) -> tuple[dict, dict]:
        """What a learning call and a look-up in ``project`` started."""

        learning: dict = {}
        ClaudeCliExtractor(engine, runner=self.runner(learning)).extract(mock.Mock(text="t", known=[]))
        lookup: dict = {}
        ClaudeCliExtractor(engine, runner=self.runner(lookup)).explore(
            "x", folder=project, schema={"type": "object"}, system_prompt="look\nthings up")
        return learning, lookup

    def test_a_direct_launch_gets_the_prompt_as_an_argument(self) -> None:
        with tempfile.TemporaryDirectory() as project:
            learning, lookup = self.calls(Path("claude.exe"), Path(project))
        self.assertEqual(SYSTEM_PROMPT, learning["command"][learning["command"].index("--system-prompt") + 1])
        self.assertEqual("look\nthings up", lookup["command"][lookup["command"].index("--system-prompt") + 1])
        for seen in (learning, lookup):
            self.assertNotIn("--system-prompt-file", seen["command"])
            self.assertEqual([], seen["files"])

    @unittest.skipUnless(WINDOWS, "command scripts run through cmd.exe on Windows")
    def test_through_cmd_the_prompt_is_in_a_file_outside_the_working_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = write_own_script(Path(temporary) / "own", "claude.exe")
            project = Path(temporary) / "project"
            project.mkdir()
            learning, lookup = self.calls(engine, project)
            self.assertEqual([], list(project.iterdir()))
        self.assertEqual(SYSTEM_PROMPT, learning["text"])
        self.assertEqual("look\nthings up", lookup["text"])
        for seen in (learning, lookup):
            self.assertNotIn("--system-prompt", seen["command"])
            self.assertEqual([], seen["files"])
            self.assertFalse(seen["prompt"].exists())


@unittest.skipUnless(WINDOWS, "npm's command scripts are how npm installs commands on Windows")
class EngineDiscoveryTests(unittest.TestCase):
    """A native engine wins over npm's command script, whatever the PATH order (decision D13)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.npm = self.root / "npm"
        self.claude_script = write_npm_script(self.npm, "claude", "@anthropic-ai/claude-code/cli.js")
        self.codex_script = write_npm_script(self.npm, "codex", "@openai/codex/bin/codex.js")
        for target in ("@anthropic-ai/claude-code/cli.js", "@openai/codex/bin/codex.js"):
            self.file(self.npm / "node_modules" / target)
        self.node = self.file(self.root / "nodejs" / "node.exe")
        self.native = self.root / "native"
        self.native.mkdir()
        self.home = self.root / "home"
        environment = mock.patch.dict(os.environ, {
            "PATH": os.pathsep.join([str(self.npm), str(self.node.parent), str(self.native)]),
            "APPDATA": str(self.root / "Roaming"), "LOCALAPPDATA": str(self.root / "Local"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        home = mock.patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def file(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    def started(self, extractor_class, executable: Path, **options) -> list[str]:
        """The command an engine call starts."""

        calls = []

        def runner(command, **options):
            calls.append(command)
            if "-o" in command:
                Path(command[command.index("-o") + 1]).write_text('{"memories": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b'{"structured_output": {"memories": []}}', b"")

        extractor_class(executable, runner=runner, **options).run("x", schema=OUTPUT_SCHEMA, system_prompt="p")
        return calls[0]

    def test_a_native_claude_later_on_path_wins(self) -> None:
        native = self.file(self.native / "claude.exe")
        self.assertEqual(native, find_claude_cli())

    def test_anthropics_installer_location_wins(self) -> None:
        official = self.file(self.home / ".local" / "bin" / "claude.exe")
        self.assertEqual(official, find_claude_cli())

    def test_the_desktop_apps_engine_wins(self) -> None:
        bundled = self.file(self.root / "Roaming" / "Claude" / "claude-code" / "2.1.286" / "claude.exe")
        self.assertEqual(Path(os.path.realpath(bundled)), find_claude_cli())

    def test_npms_claude_alone_is_started_with_node(self) -> None:
        self.assertEqual(self.claude_script, find_claude_cli())
        script = str(self.npm / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js")
        command = self.started(ClaudeCliExtractor, self.claude_script)
        self.assertEqual([str(self.node), script], command[:2])
        self.assertEqual("p", command[command.index("--system-prompt") + 1])
        # npm's script prefers a node.exe beside itself.
        beside = self.file(self.npm / "node.exe")
        self.assertEqual([str(beside), script], self.started(ClaudeCliExtractor, self.claude_script)[:2])

    def test_a_script_that_is_not_npms_runs_as_it_is(self) -> None:
        wrapper = write_own_script(self.root / "wrapper", f'"{self.node}" "%~dp0\\cli.js"')
        command = self.started(ClaudeCliExtractor, wrapper)
        self.assertEqual(str(wrapper), command[0])
        self.assertIn("--system-prompt-file", command)

    def test_a_native_codex_wins_over_a_newer_npm_codex(self) -> None:
        native = self.file(self.root / "Local" / "OpenAI" / "Codex" / "bin" / "1" / "codex.exe")
        asked = []

        def runner(command, **options):
            asked.append(command)
            version = b"codex-cli 0.200.0\n" if command[0] == str(self.node) else b"codex-cli 0.150.0\n"
            return subprocess.CompletedProcess(command, 0, version, b"")

        self.assertEqual(native, find_codex_cli(runner=runner))
        self.assertNotIn(str(self.codex_script), [command[0] for command in asked])

    def test_npms_codex_alone_is_started_with_node(self) -> None:
        asked = []

        def runner(command, **options):
            asked.append(command)
            return subprocess.CompletedProcess(command, 0, b'codex-cli 0.150.0\n{"models": []}', b"")

        self.assertEqual(self.codex_script, find_codex_cli(runner=runner))
        script = str(self.npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
        self.assertEqual([str(self.node), script, "--version"], asked[0])
        codex_listed_models(self.codex_script, runner=runner)
        self.assertEqual([str(self.node), script, "debug", "models"], asked[1])
        self.assertEqual([str(self.node), script, "exec"], self.started(CodexCliExtractor, self.codex_script)[:3])

    def test_doctor_names_the_engine_that_runs(self) -> None:
        from knowitall2 import doctor

        settings = mock.Mock(enabled=True, backend="claude-cli")
        with mock.patch("knowitall2.learning.state.load_settings", return_value=settings):
            alone = doctor._engine_check()
            native = self.file(self.native / "claude.exe")
            both = doctor._engine_check()
        self.assertTrue(alone.ok)
        self.assertIn(str(self.claude_script).casefold(), alone.detail.casefold())
        self.assertIn("node", alone.detail)
        self.assertEqual(f"Claude Code at {native}", both.detail)


if __name__ == "__main__":
    unittest.main()
