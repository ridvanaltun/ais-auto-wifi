#!/usr/bin/env python3
"""
Build a self-contained "AIS Wi-Fi Auto-Login.app" with py2app.

Unlike make_app.py (which builds a small launcher tied to your local Python,
for personal use), this bundles the Python interpreter and all dependencies
into a standalone .app that runs on other Macs. It is what the GitHub release
workflow ships.

    pip install py2app
    python3 setup_app.py py2app
    # -> dist/AIS Wi-Fi Auto-Login.app

The app is ad-hoc signed only (no Apple Developer ID), so Gatekeeper will ask
the user to right-click → Open the first time.
"""

from setuptools import setup
import pathlib
import re

_here = pathlib.Path(__file__).parent
_init = (_here / "aiswifi" / "__init__.py").read_text(encoding="utf-8")
APP_NAME = re.search(r'^__app_name__\s*=\s*"([^"]+)"', _init, re.MULTILINE).group(1)
VERSION = re.search(r'^__version__\s*=\s*"([^"]+)"', _init, re.MULTILINE).group(1)
BUNDLE_ID = re.search(r'^__bundle_id__\s*=\s*"([^"]+)"', _init, re.MULTILINE).group(1)

setup(
    app=["run.py"],
    name=APP_NAME,
    setup_requires=["py2app"],
    options={"py2app": {
        "argv_emulation": False,
        # Copy full packages (py2app's modulegraph misses some lazy imports).
        "packages": ["aiswifi", "rumps", "requests", "urllib3", "certifi",
                     "charset_normalizer", "idna", "bs4", "soupsieve", "keyring"],
        # PyObjC frameworks the app loads dynamically.
        "includes": ["CoreWLAN", "ServiceManagement", "Foundation", "AppKit",
                     "PyObjCTools.AppHelper", "Quartz"],
        "plist": {
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSApplicationCategoryType": "public.app-category.utilities",
            "LSMinimumSystemVersion": "11.0",
            "LSUIElement": True,  # menu bar app: no Dock icon
            "NSHighResolutionCapable": True,
        },
    }},
)
