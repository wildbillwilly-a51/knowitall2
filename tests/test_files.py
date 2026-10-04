"""Files several processes share: replacing one that is open, reading one being replaced, BOMs, links, modes."""

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from knowitall2 import connected
from knowitall2.files import read_text, write_text_atomic
from knowitall2.learning.state import LearnerSettings, load_settings, save_settings

BOM = "﻿"


class FilesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root / "data")})
        self.environment.start()
        self.addCleanup(self.environment.stop)


class ReplacingTests(FilesTestCase):
    def test_a_write_waits_for_a_reader_that_holds_the_file_open(self) -> None:
        # Windows refuses to replace a file another handle has open; the reader lets go after 50 ms.
        target = self.root / "state.json"
        write_text_atomic(target, "{}\n")
        reader = open(target, encoding="utf-8")
        letting_go = threading.Timer(0.05, reader.close)
        letting_go.start()
        try:
            write_text_atomic(target, '{"a": 1}\n')
        finally:
            letting_go.join()
            reader.close()
        self.assertEqual('{"a": 1}\n', target.read_text(encoding="utf-8"))
        self.assertEqual(["state.json"], [path.name for path in self.root.iterdir() if path.is_file()])

    def test_a_reader_racing_a_writer_never_falls_back_to_the_defaults(self) -> None:
        save_settings(LearnerSettings(enabled=True))
        stop = threading.Event()
        failures: list[BaseException] = []

        def rewrite() -> None:
            minutes = 0
            while not stop.is_set():
                minutes += 1
                try:
                    save_settings(LearnerSettings(enabled=True, idle_minutes=minutes))
                except OSError as exc:
                    failures.append(exc)

        writer = threading.Thread(target=rewrite)
        writer.start()
        reads = defaults = 0
        try:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                reads += 1
                if not load_settings().enabled:
                    defaults += 1
        finally:
            stop.set()
            writer.join()
        self.assertEqual(0, defaults, f"{defaults} of {reads} reads fell back to the defaults")
        self.assertEqual([], failures[:3])

    def test_reading_text_accepts_a_byte_order_mark(self) -> None:
        path = self.root / "notes.txt"
        path.write_bytes((BOM + "first\r\nsecond\n").encode("utf-8"))
        self.assertEqual("first\nsecond\n", read_text(path))

    def test_writing_through_a_link_keeps_the_link(self) -> None:
        real = self.root / "dotfiles" / "CLAUDE.md"
        real.parent.mkdir()
        real.write_text("before\n", encoding="utf-8")
        link = self.root / "CLAUDE.md"
        try:
            os.symlink(real, link)
        except OSError as exc:  # WinError 1314: creating links needs Developer Mode or an administrator
            self.skipTest(f"symbolic links cannot be created here: {exc}")
        write_text_atomic(link, "after\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual("after\n", real.read_text(encoding="utf-8"))
        self.assertEqual(["CLAUDE.md"], [path.name for path in real.parent.iterdir()])

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes")
    def test_a_rewrite_keeps_the_files_mode(self) -> None:
        path = self.root / "settings.json"
        path.write_text("{}\n", encoding="utf-8")
        os.chmod(path, 0o644)
        write_text_atomic(path, '{"a": 1}\n')
        self.assertEqual(0o644, stat.S_IMODE(path.stat().st_mode))
        fresh = self.root / "fresh.json"
        write_text_atomic(fresh, "{}\n")
        self.assertEqual(0o600, stat.S_IMODE(fresh.stat().st_mode))  # a new file stays private


class ByteOrderMarkTests(FilesTestCase):
    """Windows editors may save a UTF-8 file with a byte order mark; it must not change what the file says."""

    def test_settings_saved_with_a_bom_are_honoured(self) -> None:
        config = self.root / "data" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_bytes((BOM + json.dumps({"theme": "dark", "learning": {"enabled": True}})).encode("utf-8"))
        self.assertTrue(load_settings().enabled)
        save_settings(LearnerSettings(enabled=True, idle_minutes=5))
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual("dark", saved["theme"])  # the rest of the file is kept
        self.assertEqual(5, saved["learning"]["idle_minutes"])

    def test_a_connection_saved_with_a_bom_still_counts_and_its_key_is_clean(self) -> None:
        server = self.root / "data" / "server"
        (server / "keys").mkdir(parents=True)
        (server / "connection.json").write_bytes((BOM + json.dumps({"address": "https://kia.example.lan"}))
                                                 .encode("utf-8"))
        (server / "keys" / "codex.key").write_bytes((BOM + "kia_secret_value\r\n").encode("utf-8"))
        self.assertTrue(connected.is_connected())
        found = connected.load()
        self.assertIsNotNone(found)
        self.assertEqual("https://kia.example.lan", found.address)
        self.assertEqual({"codex": "kia_secret_value"}, found.keys)


if __name__ == "__main__":
    unittest.main()
