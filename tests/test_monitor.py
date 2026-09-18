"""monitor.py — retry policy, OTP source, resilience and state tests."""

import threading
import unittest
from unittest import mock

from aiswifi import config as config_mod
from aiswifi import monitor as monitor_mod
from aiswifi import network


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


class OtpProviderTests(unittest.TestCase):
    def test_otp_always_asks_the_user(self):
        # The code is requested from the UI; there is no Messages auto-read
        # (the SMS can't reach the Mac on a captive portal) and no prepare step.
        m = _monitor()
        m.set_ask_otp_callback(lambda: "1234")
        prepare, fn = m._make_otp_provider()
        self.assertIsNone(prepare)
        self.assertEqual(fn(), "1234")

    def test_no_callback_returns_none(self):
        m = _monitor()  # no ask callback set (e.g. headless)
        prepare, fn = m._make_otp_provider()
        self.assertIsNone(prepare)
        self.assertIsNone(fn())


class _StatusProvider:
    key, name, supports_status, last_failure = "ais", "AIS SUPER WiFi", True, ""
    def __init__(self, info):
        self.info, self.calls = info, 0
    def matches(self, *a):
        return True
    def session_status(self, session, timeout):
        self.calls += 1
        return self.info


class RemainingTimeTests(unittest.TestCase):
    def test_online_updates_remaining_from_status_provider(self):
        m = _monitor()
        m._status_provider = _StatusProvider({"online": True, "remaining_seconds": 642})
        m._update_remaining(object())
        snap = m.state.snapshot()
        self.assertEqual(snap["remaining_seconds"], 642)
        self.assertGreater(snap["remaining_at"], 0)   # measurement time recorded for local ticking
        self.assertFalse(snap["remaining_unlimited"])

    def test_unlimited_account(self):
        m = _monitor()
        m._status_provider = _StatusProvider({"online": True, "remaining_seconds": None,
                                              "unlimited": True})
        m._update_remaining(object())
        self.assertTrue(m.state.snapshot()["remaining_unlimited"])

    def test_queries_ais_even_without_ssid_or_login(self):
        # macOS often hides the SSID; the countdown must still appear.
        m = _monitor()
        m._registry = [_StatusProvider({"online": True, "remaining_seconds": 60})]
        m._update_remaining(object())
        self.assertEqual(m.state.snapshot()["remaining_seconds"], 60)

    def test_non_ais_network_gives_up_after_one_query(self):
        m = _monitor()
        prov = _StatusProvider({"online": False})
        m._registry = [prov]
        m._update_remaining(object())
        m._update_remaining(object())  # should not re-query until reconnect
        self.assertIsNone(m.state.snapshot()["remaining_seconds"])
        self.assertEqual(prov.calls, 1)
        self.assertTrue(m._status_gave_up)

    def test_keeps_polling_after_login_even_if_status_blips_false(self):
        m = _monitor()
        prov = _StatusProvider({"online": False})
        m._status_provider = prov
        m._update_remaining(object())
        m._update_remaining(object())
        self.assertEqual(prov.calls, 2)  # logged-in provider is not given up on
        self.assertFalse(m._status_gave_up)


class LoopTests(unittest.TestCase):
    def test_initial_and_set_auto_do_not_claim_online(self):
        m = _monitor(auto_login=True)
        self.assertEqual(m.state.status, monitor_mod.ST_CHECKING)
        m.set_auto(False)  # does not claim ONLINE; keeps probing to show real status
        self.assertNotEqual(m.state.status, monitor_mod.ST_ONLINE)
        self.assertIn("off", m.state.snapshot()["message"])
        m.set_auto(True)
        self.assertEqual(m.state.status, monitor_mod.ST_CHECKING)
        self.assertFalse(m._login_now.is_set())  # a wake-up, not a forced login

    def test_manual_login_shows_countdown_without_auto(self):
        # Auto login off + user logs in manually: the loop must still probe
        # ONLINE and populate the countdown (the reported bug).
        m = _monitor(auto_login=False, poll_interval=15)
        m._registry = [_StatusProvider({"online": True, "remaining_seconds": 300})]
        online = network.ProbeResult(network.ONLINE)
        with mock.patch.object(network, "get_ssid", return_value=None), \
                mock.patch.object(network, "probe_connectivity", return_value=online):
            m._cycle(object(), 0.0)
        snap = m.state.snapshot()
        self.assertEqual(snap["status"], monitor_mod.ST_ONLINE)
        self.assertEqual(snap["remaining_seconds"], 300)

    def test_captive_without_auto_does_not_login(self):
        m = _monitor(auto_login=False)
        m._do_login = lambda *a: (_ for _ in ()).throw(AssertionError("must not auto-login"))
        captive = network.ProbeResult(network.CAPTIVE, portal_url="http://10.0.0.1/")
        with mock.patch.object(network, "get_ssid", return_value=None), \
                mock.patch.object(network, "probe_connectivity", return_value=captive):
            m._cycle(object(), 0.0)
        self.assertEqual(m.state.snapshot()["status"], monitor_mod.ST_CAPTIVE)

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

    def test_forced_attempt_is_followed_by_a_full_wait(self):
        # 'Connect Now' must not be followed immediately by an automatic attempt
        # (with OTP that would trigger two SMS back to back).
        m = _monitor(auto_login=True, poll_interval=15)
        waits, attempts = [], []

        class RecordingEvent(threading.Event):
            def wait(self, timeout=None):
                waits.append(timeout)
                return self.is_set()

        m._wake = RecordingEvent()
        m._attempt_cycle = lambda session, forced: attempts.append(forced)
        m.trigger_login()
        online = network.ProbeResult(network.ONLINE)
        with mock.patch.object(network, "get_ssid", return_value=None), \
                mock.patch.object(network, "probe_connectivity", return_value=online):
            m._cycle(object(), 0.0)
        self.assertEqual(attempts, [True])
        self.assertEqual(waits, [15.0, 15.0])

    def test_otp_failure_backoff_floor(self):
        m = _monitor(auto_login=True, login_method="otp", poll_interval=15)
        m._do_login = lambda *a: False
        with mock.patch.object(network, "get_ssid", return_value=None), \
                mock.patch.object(network, "probe_connectivity", return_value=CAPTIVE):
            backoff = m._cycle(object(), 0.0)
        self.assertGreaterEqual(backoff, monitor_mod.OTP_MIN_BACKOFF)


if __name__ == "__main__":
    unittest.main()
