"""scripts/bump_version.py — pure version-bump logic and reading the version."""

import importlib.util
import pathlib
import unittest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "bump_version.py"
_spec = importlib.util.spec_from_file_location("bump_version", _PATH)
bump_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_version)


class BumpTests(unittest.TestCase):
    def test_patch_minor_major(self):
        self.assertEqual(bump_version.bump("1.0.0", "patch"), "1.0.1")
        self.assertEqual(bump_version.bump("1.0.3", "minor"), "1.1.0")
        self.assertEqual(bump_version.bump("1.4.2", "major"), "2.0.0")

    def test_explicit_version(self):
        self.assertEqual(bump_version.bump("1.0.0", "2.3.0"), "2.3.0")

    def test_unknown_part_raises(self):
        with self.assertRaises(ValueError):
            bump_version.bump("1.0.0", "nope")

    def test_read_version_from_init(self):
        text = '__version__ = "3.4.5"\n__app_name__ = "x"\n'
        self.assertEqual(bump_version.read_version(text), "3.4.5")
        with self.assertRaises(ValueError):
            bump_version.read_version("no version here")

    def test_matches_the_package_version(self):
        from aiswifi import __version__
        self.assertEqual(bump_version.read_version(bump_version.INIT_PATH.read_text()),
                         __version__)


if __name__ == "__main__":
    unittest.main()
