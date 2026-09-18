#!/usr/bin/env python3
"""
Bump the project version (single source of truth: aiswifi/__init__.py).

Usage:
    python3 scripts/bump_version.py patch   # 1.0.0 -> 1.0.1
    python3 scripts/bump_version.py minor   # 1.0.3 -> 1.1.0
    python3 scripts/bump_version.py major   # 1.4.2 -> 2.0.0
    python3 scripts/bump_version.py 2.3.0   # set an explicit version

It rewrites `__version__` in aiswifi/__init__.py and prints the new version to
stdout (so CI can capture it). With `--current` it only prints the current
version and changes nothing.
"""

from __future__ import annotations

import pathlib
import re
import sys

INIT_PATH = pathlib.Path(__file__).resolve().parent.parent / "aiswifi" / "__init__.py"
_VERSION_RE = re.compile(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', re.MULTILINE)
_EXPLICIT_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read_version(text: str) -> str:
    m = _VERSION_RE.search(text)
    if not m:
        raise ValueError("__version__ (X.Y.Z) not found in aiswifi/__init__.py")
    return ".".join(m.groups())


def bump(version: str, part: str) -> str:
    """Return the next version. `part` is patch/minor/major or an explicit X.Y.Z."""
    if _EXPLICIT_RE.match(part):
        return part
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unknown bump part: {part!r} (use patch/minor/major or X.Y.Z)")


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    text = INIT_PATH.read_text(encoding="utf-8")
    current = read_version(text)
    if not argv or argv[0] == "--current":
        print(current)
        return 0
    new = bump(current, argv[0])
    if new != current:
        INIT_PATH.write_text(_VERSION_RE.sub(f'__version__ = "{new}"', text, count=1),
                             encoding="utf-8")
    print(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
