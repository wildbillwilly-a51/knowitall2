"""Running a learning engine (Claude Code or Codex) as a bounded child process.

Every engine call goes through ``run_bounded``: the engine gets its input on
stdin and a time limit, and when the limit passes, the engine and everything
it started are stopped. An engine's helpers can hold its output pipes after
it is stopped, so stopping only the engine would leave the call waiting for
them.

On Windows, npm installs a command as a ``.cmd`` script, which runs through
``cmd.exe``: that ends an argument at its first newline and splits one at
``&``. ``engine_command`` reads npm's script and starts its node script
directly instead.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

# How long the output may take to close once the engine has been stopped.
STOP_WAIT = 5.0

_COMMAND_SCRIPTS = (".cmd", ".bat")
_SCRIPT_SIZE = 64 * 1024
# What a command script of npm's sets: cmd-shim's own variables; any other means a script that is not npm's.
_NPM_SETTINGS = re.compile(r'@?SET\s+(dp0=|"_prog=|PATHEXT=)', re.IGNORECASE)
_SETTING = re.compile(r"@?SET\s", re.IGNORECASE)
_PROGRAM = re.compile(r'SET\s+"_prog=([^"]*)"', re.IGNORECASE)
_WORD = re.compile(r'"([^"]*)"|(\S+)')


def is_command_script(executable: Path) -> bool:
    """Whether ``executable`` is a Windows command script (npm's ``claude.cmd``), which runs through cmd.exe."""

    return executable.suffix.lower() in _COMMAND_SCRIPTS


def engine_command(executable: Path) -> list[str]:
    """The start of the command line that runs ``executable``: npm's command script becomes node and its script."""

    if sys.platform == "win32" and is_command_script(executable):
        return npm_command(executable) or [str(executable)]
    return [str(executable)]


def through_cmd(command: list[str]) -> bool:
    """Whether ``command`` runs through cmd.exe: a command script that is not npm's, which runs as it is."""

    return sys.platform == "win32" and is_command_script(Path(command[0]))


def describe_engine(executable: Path) -> str:
    """Where an engine is, for people; for npm's command script, also what is started in its place."""

    if sys.platform != "win32" or not is_command_script(executable):
        return str(executable)
    started = npm_command(executable)
    if started is None:
        return f"{executable} (a command script, run through cmd.exe)"
    return f"{executable} (installed by npm; started as {subprocess.list2cmdline(started)})"


def npm_command(script: Path) -> list[str] | None:
    """The program, with its script, that npm's command script ``script`` runs; None when it is not npm's.

    npm writes every command script from one template (cmd-shim): its last line
    runs ``"%_prog%"`` (the node.exe beside the script when there is one, else
    node) with the package's script and ``%*``, or runs the package's program
    itself. Older npm wrote the two choices as two lines. A script that sets
    anything else is someone's own, and runs as it is.
    """

    try:
        if script.stat().st_size > _SCRIPT_SIZE:
            return None
        text = script.read_text(encoding="utf-8", errors="replace").replace("%~dp0", "%dp0%")
    except OSError:
        return None
    lines = [line.strip() for line in text.splitlines()]
    if any(_SETTING.match(line) and not _NPM_SETTINGS.match(line) for line in lines):
        return None
    folder = str(script.parent) + "\\"
    programs = [value.replace("%dp0%", folder) for value in _PROGRAM.findall(text)]
    for line in [line for line in lines if line.endswith("%*")]:
        # cmd-shim runs the program after "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & ".
        words = [quoted or bare for quoted, bare in _WORD.findall(line[:-2].rsplit("& ", 1)[-1])]
        if not words:
            continue
        arguments = [os.path.normpath(word.replace("%dp0%", folder)) if "%dp0%" in word else word
                     for word in words[1:]]
        if any("%" in word for word in arguments) or (arguments and not Path(arguments[-1]).is_file()):
            continue
        for program in programs if words[0] == "%_prog%" else [words[0].replace("%dp0%", folder)]:
            found = _program(program)
            if found:
                return [found, *arguments]
    return None


def _program(name: str) -> str | None:
    """A program a command script names, as cmd.exe would find it; never another command script."""

    if "\\" in name or "/" in name:
        path = os.path.normpath(name)
        found = path if Path(path).is_file() else None
    else:
        found = shutil.which(name if Path(name).suffix else name + ".exe")
    if not found or is_command_script(Path(found)):
        return None
    return found


def run_bounded(command: list[str], *, input: bytes | None = None, timeout: float, cwd: str | None = None,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """``command`` with ``input`` on stdin and its output kept, like ``subprocess.run``.

    When ``timeout`` seconds pass, the engine and everything it started are
    stopped and ``subprocess.TimeoutExpired`` is raised.
    """

    if sys.platform == "win32":
        options: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        options = {"start_new_session": True}  # its own process group, stopped as one
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL if input is None else subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=cwd, env=env, **options,
    )
    job = _job_for(process) if sys.platform == "win32" else None
    try:
        if input is not None:
            _feed(process, input)
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:  # the time limit, or an interruption
        _stop_tree(process, job)
        try:
            process.communicate(timeout=STOP_WAIT)
        except subprocess.TimeoutExpired:
            pass  # something outside the tree still holds the output
        raise
    finally:
        if job is not None:
            job.close()
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _feed(process: subprocess.Popen, data: bytes) -> None:
    """Write ``data`` to the engine's stdin from a thread of its own.

    On Windows ``communicate`` writes stdin before it starts waiting, so an
    engine that stops reading would hold it past any time limit.
    """

    stream, process.stdin = process.stdin, None

    def write() -> None:
        try:
            stream.write(data)
        except OSError:
            pass  # the engine ended, or was stopped
        finally:
            try:
                stream.close()
            except OSError:
                pass

    threading.Thread(target=write, name="knowitall2-engine-input", daemon=True).start()


def _stop_tree(process: subprocess.Popen, job: _Job | None) -> None:
    """Stop the engine and everything it started."""

    if job is not None and job.terminate():
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _job_for(process: subprocess.Popen) -> _Job | None:
    """A Windows job object holding ``process`` and whatever it starts, or None when one cannot be made."""

    try:
        return _Job(process.pid)
    except Exception:  # without a job, a timeout stops the tree with taskkill
        return None


class _Job:
    """A Windows job object: the processes in it are stopped together, and when it is closed.

    The engine joins right after it starts, before it has had time to start
    anything, and what it starts later joins too. A program that asks to leave
    the job may, because Windows would refuse to start it otherwise.
    """

    def __init__(self, pid: int) -> None:
        import ctypes

        self._kernel32, limits_type = _windows_api()
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = limits_type()
            limits.Basic.LimitFlags = _KILL_ON_JOB_CLOSE | _BREAKAWAY_OK
            if not self._kernel32.SetInformationJobObject(self._handle, _EXTENDED_LIMIT_INFORMATION,
                                                          ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self._kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self._kernel32.AssignProcessToJobObject(self._handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self._kernel32.CloseHandle(process)
        except BaseException:
            self.close()
            raise

    def terminate(self) -> bool:
        return bool(self._handle) and bool(self._kernel32.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        """Let go of the job; whatever still runs in it is stopped."""

        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


_EXTENDED_LIMIT_INFORMATION = 9
_BREAKAWAY_OK = 0x800
_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


@functools.cache
def _windows_api() -> tuple[Any, type]:
    """kernel32 with the job object functions declared, and JOBOBJECT_EXTENDED_LIMIT_INFORMATION."""

    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("Basic", Basic), ("Io", Counters), ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, result, arguments in (
        ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        ("SetInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
        ("OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
        ("TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
    ):
        function = getattr(kernel32, name)
        function.restype, function.argtypes = result, arguments
    return kernel32, ExtendedLimits
