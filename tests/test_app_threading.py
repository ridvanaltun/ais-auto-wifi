"""
app.py — when the OTP dialog is requested from the worker thread, it must
open on the MAIN thread.

No real dialog is shown (rumps.Window is faked); runs only on macOS with
rumps installed.
"""

import threading
import unittest
from unittest import mock

try:
    from Foundation import NSDate, NSRunLoop, NSThread  # type: ignore
    from aiswifi import app as app_mod
except Exception:  # no macOS / rumps
    app_mod = None


def _pump_main_runloop(until, seconds=3.0):
    """Spin the main run loop until `until()` is true (or the time runs out)."""
    end = NSDate.dateWithTimeIntervalSinceNow_(seconds)
    while not until() and NSDate.date().compare_(end) < 0:
        NSRunLoop.mainRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))


@unittest.skipIf(app_mod is None, "requires macOS + rumps")
class AskOtpThreadingTests(unittest.TestCase):
    def setUp(self):
        self.seen = []

        test = self

        class FakeWindow:
            def __init__(self, **kw):
                test.seen.append(NSThread.isMainThread())

            def run(self):
                return mock.Mock(clicked=1, text=" 482193 ")

        for target, attr, value in ((app_mod.rumps, "Window", FakeWindow),
                                    (app_mod.AppKit, "NSApplication", mock.Mock())):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.fake_app = mock.Mock()
        self.fake_app.monitor.stopping = False

    def test_window_opens_on_main_thread_from_worker(self):
        result = {}
        t = threading.Thread(target=lambda: result.update(
            code=app_mod.AISWifiApp._ask_otp(self.fake_app)), daemon=True)
        t.start()
        _pump_main_runloop(lambda: not t.is_alive())
        t.join(1)
        self.assertEqual(self.seen, [True], "the dialog did not open on the main thread")
        self.assertEqual(result.get("code"), "482193")

    def test_worker_gives_up_when_app_is_stopping(self):
        self.fake_app.monitor.stopping = True
        result = {}
        t = threading.Thread(target=lambda: result.update(
            code=app_mod.AISWifiApp._ask_otp(self.fake_app)), daemon=True)
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertIsNone(result.get("code"))
        _pump_main_runloop(lambda: bool(self.seen), 1.0)  # drain the queued call with the fake


@unittest.skipIf(app_mod is None, "requires macOS + rumps")
class ActivationPolicyTests(unittest.TestCase):
    def test_run_makes_app_focusable_before_start(self):
        # With a non-framework Python the default "Prohibited" policy prevents
        # text fields from receiving keyboard input.
        with mock.patch.object(app_mod.AppKit, "NSApplication") as ns, \
                mock.patch.object(app_mod, "AISWifiApp") as app_cls, \
                mock.patch.object(app_mod.config_mod, "setup_logging"):
            app_mod.run()
        ns.sharedApplication.return_value.setActivationPolicy_.assert_called_once_with(
            app_mod.AppKit.NSApplicationActivationPolicyAccessory)
        app_cls.return_value.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
