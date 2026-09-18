"""monitor.py — retry policy, OTP source, resilience and state tests."""

import threading
import unittest
from unittest import mock

from aiswifi import config as config_mod
from aiswifi import monitor as monitor_mod
from aiswifi import network, otp


class _FastEvent(threading.Event):
    """wait() never blocks (to keep the tests fast)."""

    def wait(self, timeout=None):
        return self.is_set()


class _CountingProvider:
    key, name, last_failure = "ais", "AIS SUPER WiFi", "Form submitted but the internet did not come up"

    def __init__(self):
        self.calls = 0

    def matches(self, *a):
        return True

    def login(self, *a, **kw):
        self.calls += 1
        return False


def _monitor(**cfg):
    base = dict(config_mod.DEFAULT_CONFIG)
    base.update(cfg)
    m = monitor_mod.Monitor(base)
    m._stop = _FastEvent()
    m._wake = _FastEvent()
    return m


CAPTIVE = network.ProbeResult(network.CAPTIVE, portal_url="http://10.0.0.1/")


class RetryPolicyTests(unittest.TestCase):
    def run_login(self, method, creds=("0812345678", "pw12")):
        m = _monitor(login_method=method, max_retries=3)
        prov = _CountingProvider()
        m._registry = [prov]
        with mock.patch.object(config_mod, "get_credentials", return_value=creds):
            ok = m._do_login(object(), CAPTIVE, None)
        return m, prov, ok

    def test_otp_makes_single_attempt_per_cycle(self):
        m, prov, ok = self.run_login("otp", ("0812345678", None))
        self.assertFalse(ok)
        self.assertEqual(prov.calls, 1)  # every attempt means a new SMS

    def test_password_retries_and_reports_reason(self):
        m, prov, ok = self.run_login("password")
        self.assertEqual(prov.calls, 3)
        snap = m.state.snapshot()
        self.assertEqual(snap["status"], monitor_mod.ST_ERROR)
        self.assertIn("internet did not come up", snap["message"])

    def test_keychain_error_is_not_reported_as_missing_credentials(self):
        m = _monitor()
        m._registry = [_CountingProvider()]
        err = config_mod.KeychainError("Could not access the Keychain — approve the access prompt")
        with mock.patch.object(config_mod, "get_credentials", side_effect=err):
            self.assertFalse(m._do_login(object(), CAPTIVE, None))
        self.assertEqual(m.state.snapshot()["last_error"], "keychain")


class OtpSourceTests(unittest.TestCase):
    def test_messages_baseline_taken_in_prepare(self):
        m = _monitor(otp_source="messages", otp_wait_timeout=1)
        with mock.patch.object(otp, "current_baseline", return_value=41) as cb, \
                mock.patch.object(otp, "wait_for_new_otp", return_value="482193") as wait:
            prepare, fn = m._make_otp_provider()
            prepare()
            self.assertEqual(fn(), "482193")
        self.assertEqual(cb.call_count, 1)
        self.assertEqual(wait.call_args[0][0], 41)

    def test_unreadable_messages_fall_back_to_asking(self):
        m = _monitor(otp_source="messages")
        m.set_ask_otp_callback(lambda: "999999")
        with mock.patch.object(otp, "current_baseline", return_value=None), \
                mock.patch.object(otp, "wait_for_new_otp") as wait:
            prepare, fn = m._make_otp_provider()
            prepare()
            self.assertEqual(fn(), "999999")
        wait.assert_not_called()  # if unreadable, don't wait 90 s for nothing

    def test_ask_source(self):
        m = _monitor(otp_source="ask")
        m.set_ask_otp_callback(lambda: "1234")
        prepare, fn = m._make_otp_provider()
        self.assertIsNone(prepare)
        self.assertEqual(fn(), "1234")


class LoopTests(unittest.TestCase):
    def test_initial_and_set_auto_do_not_claim_online(self):
        m = _monitor(auto_login=True)
        self.assertEqual(m.state.status, monitor_mod.ST_CHECKING)
        m.set_auto(False)
        self.assertEqual(m.state.status, monitor_mod.ST_IDLE)
        m.set_auto(True)
        self.assertEqual(m.state.status, monitor_mod.ST_CHECKING)
        self.assertFalse(m._login_now.is_set())  # a wake-up, not a forced login

    def test_unexpected_error_does_not_kill_thread(self):
        m = _monitor(auto_login=True)
        calls = []

        def cycle(session, backoff):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("unexpected")
            m._stop.set()
            return 0.0

        m._cycle = cycle
        with mock.patch.object(network, "new_session"):
            m._run()
        self.assertEqual(len(calls), 2)

    def test_otp_failure_backoff_floor(self):
        m = _monitor(auto_login=True, login_method="otp", poll_interval=15)
        m._do_login = lambda *a: False
        with mock.patch.object(network, "get_ssid", return_value=None), \
                mock.patch.object(network, "probe_connectivity", return_value=CAPTIVE):
            backoff = m._cycle(object(), 0.0)
        self.assertGreaterEqual(backoff, monitor_mod.OTP_MIN_BACKOFF)


if __name__ == "__main__":
    unittest.main()
