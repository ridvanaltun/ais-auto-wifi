#!/usr/bin/env python3
"""AIS Wi-Fi Auto-Login — paketleme betiği.

Yerel kurulum (geliştirme):
    pip3 install -e .

Ardından komut satırından:
    aiswifi --diagnose
    aiswifi
"""

from setuptools import setup, find_packages
import pathlib

_here = pathlib.Path(__file__).parent
_readme = (_here / "README.md")
_long_description = _readme.read_text(encoding="utf-8") if _readme.exists() else ""

setup(
    name="aiswifi",
    version="1.0.0",
    description="AIS SUPER WiFi ve benzeri captive portal ağlarına otomatik yeniden giriş yapan macOS menü çubuğu uygulaması.",
    long_description=_long_description,
    long_description_content_type="text/markdown",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "rumps>=0.4.0",
        "requests>=2.28",
        "beautifulsoup4>=4.11",
        "keyring>=24.0",
        "pyobjc-framework-CoreWLAN>=9.0",
    ],
    entry_points={
        "console_scripts": [
            "aiswifi=run:main",
        ],
    },
    include_package_data=True,
    classifiers=[
        "Environment :: MacOS X",
        "Operating System :: MacOS :: MacOS X",
        "Programming Language :: Python :: 3",
        "Intended Audience :: End Users/Desktop",
    ],
)
