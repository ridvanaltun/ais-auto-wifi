"""make_app.py — builds a real, signed app bundle whose launcher runs the code."""

import contextlib
import io
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aiswifi import __bundle_id__, __version__


# make_app builds a launcher that embeds the LOCAL Python's home path, so the
# built .app runs only where that Python lives. On CI the interpreter comes
# from actions/setup-python and is laid out differently, so this heavy
# integration test is skipped there (the release ships a py2app bundle instead).
@unittest.skipIf(os.environ.get("CI"),
                 "make_app builds a machine-local launcher; skipped on CI")
@unittest.skipUnless(sys.platform == "darwin" and shutil.which("clang") and shutil.which("codesign"),
                     "requires macOS with the Xcode Command Line Tools")
class MakeAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import make_app
        cls.dir = tempfile.mkdtemp()
        with contextlib.redirect_stdout(io.StringIO()):
            cls.app = make_app.build(Path(cls.dir))
        cls.executable = cls.app / "Contents" / "MacOS" / make_app.EXECUTABLE

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_bundle_metadata(self):
        with open(self.app / "Contents" / "Info.plist", "rb") as fh:
            info = plistlib.load(fh)
        self.assertEqual(info["CFBundleIdentifier"], __bundle_id__)
        self.assertEqual(info["CFBundleShortVersionString"], __version__)
        self.assertTrue(info["LSUIElement"])  # menu bar app, no Dock icon
        self.assertTrue(os.access(self.executable, os.X_OK))
        self.assertTrue((self.app / "Contents" / "Resources" / "app" / "run.py").exists())
        self.assertFalse(list(self.app.rglob("__pycache__")))

    def test_signature_is_valid_even_after_running(self):
        out = subprocess.run([str(self.executable), "--version"], capture_output=True, text=True)
        self.assertIn(__version__, out.stdout)
        verify = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(self.app)],
                                capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stderr)


if __name__ == "__main__":
    unittest.main()
