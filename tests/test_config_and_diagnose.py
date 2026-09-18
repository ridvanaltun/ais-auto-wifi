"""config.py validation/privacy and `run.py --diagnose` (non-macOS environment) tests."""

import contextlib
import io
import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from aiswifi import config as config_mod


class _TempConfigDir(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name, value in (("CONFIG_DIR", self.dir), ("CONFIG_PATH", self.dir / "config.json"),
                            ("LOG_PATH", self.dir / "aiswifi.log")):
            p = mock.patch.object(config_mod, name, value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        root = logging.getLogger("aiswifi")
        for h in root.handlers:
            h.close()
        root.handlers.clear()
        shutil.rmtree(self.dir, ignore_errors=True)


class ConfigTests(_TempConfigDir):
    def test_invalid_values_fall_back_to_defaults(self):
        config_mod.CONFIG_PATH.write_text(json.dumps({
            "poll_interval": "15s", "max_retries": 0, "login_method": "sms",
            "auto_login": "yes", "http_timeout": 20, "unknown": 1,
            "portal_cert_pins": {"portal.example": "ab" * 32},
            "trusted_portal_hosts": ["10.0.0.1", 5],
        }), encoding="utf-8")
        with self.assertLogs("aiswifi.config", level="WARNING"):
            cfg = config_mod.load_config()
        d = config_mod.DEFAULT_CONFIG
        self.assertEqual(cfg["poll_interval"], d["poll_interval"])
        self.assertEqual(cfg["max_retries"], d["max_retries"])
        self.assertEqual(cfg["login_method"], d["login_method"])
        self.assertEqual(cfg["auto_login"], d["auto_login"])
        self.assertEqual(cfg["http_timeout"], 20)  # a valid override is kept
        self.assertEqual(cfg["portal_cert_pins"], {"portal.example": "ab" * 32})
        self.assertEqual(cfg["trusted_portal_hosts"], [])  # a non-string entry → default
        self.assertNotIn("unknown", cfg)

    def test_defaults_are_not_shared_between_loads(self):
        cfg = config_mod.load_config()
        cfg["portal_cert_pins"]["x"] = "y"
        self.assertEqual(config_mod.DEFAULT_CONFIG["portal_cert_pins"], {})

    def test_save_never_writes_secrets(self):
        cfg = config_mod.load_config()
        cfg.update(phone="0812345678", password="S3cretPw", otp="482193")
        config_mod.save_config(cfg)
        text = config_mod.CONFIG_PATH.read_text(encoding="utf-8")
        for secret in ("0812345678", "S3cretPw", "482193"):
            self.assertNotIn(secret, text)


class DiagnoseWithoutMacTests(_TempConfigDir):
    """Diagnostics must not crash WITHOUT CoreWLAN, networksetup, Keychain, Messages and network."""

    def test_diagnose_runs(self):
        import run
        import keyring
        from aiswifi import network

        def no_network(self, url, **kw):
            raise requests.ConnectionError("no network")

        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"CoreWLAN": None}), \
                mock.patch.object(network.subprocess, "run", side_effect=FileNotFoundError), \
                mock.patch.object(requests.Session, "get", no_network), \
                mock.patch.object(keyring, "get_password",
                                  side_effect=keyring.errors.NoKeyringError("no backend")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.main(["--diagnose"]), 0)
        text = out.getvalue()
        self.assertIn("Status       : offline", text)
        self.assertIn("SSID         : (unreadable", text)
        self.assertIn("Credentials  : unreadable", text)
        self.assertIn("Diagnostics complete.", text)

    def test_diagnose_shows_time_left_when_on_ais(self):
        import run
        from aiswifi import network
        from aiswifi.providers.ais import AISProvider

        info = {"online": True, "remaining_text": "00:10:42", "remaining_seconds": 642,
                "session_text": "00:19:18"}
        out = io.StringIO()
        with mock.patch.object(network, "get_ssid", return_value="AIS SUPER WiFi"), \
                mock.patch.object(network, "probe_connectivity",
                                  return_value=network.ProbeResult(network.ONLINE)), \
                mock.patch.object(AISProvider, "session_status", return_value=info), \
                mock.patch.object(config_mod, "get_credentials", return_value=(None, None)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.main(["--diagnose"]), 0)
        self.assertIn("Time left    : 00:10:42 (session 00:19:18)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
