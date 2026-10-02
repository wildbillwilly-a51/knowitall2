import os
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from knowitall2.paths import data_home, in_package_storage


class DataHomeTests(unittest.TestCase):
    def test_default_is_in_the_user_profile(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "KNOWITALL2_HOME"}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(Path.home() / ".knowitall2", data_home())

    def test_environment_variable_overrides_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"KNOWITALL2_HOME": "D:\\memories"}):
            self.assertEqual(Path("D:\\memories"), data_home())

    @unittest.skipUnless(os.name == "nt", "Windows app package storage")
    def test_recognizes_windows_app_package_storage(self) -> None:
        packaged = r"C:\Users\someone\AppData\Local\Packages\Claude_abc123\LocalCache\Local\KnowItAll2"
        self.assertTrue(in_package_storage(packaged))
        self.assertFalse(in_package_storage(r"C:\Users\someone\.knowitall2"))
        self.assertFalse(in_package_storage(r"C:\Users\someone\AppData\Local\Programs\Python"))


if __name__ == "__main__":
    unittest.main()
