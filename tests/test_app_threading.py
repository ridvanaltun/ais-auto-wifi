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
class OpenAtLoginMenuTests(unittest.TestCase):
    def toggle(self, current, after=None, clicked=0):
        li = app_mod.login_item
        fake_app = mock.Mock()
        fake_app.cfg = {"language": "en"}
        fake_app._t = app_mod.AISWifiApp._t.__get__(fake_app)  # real translations
        with mock.patch.object(li, "status", return_value=current), \
                mock.patch.object(li, "set_enabled", return_value=(after, None)) as set_enabled, \
                mock.patch.object(li, "open_login_items_settings") as open_settings, \
                mock.patch.object(app_mod.rumps, "alert", return_value=clicked) as alert, \
                mock.patch.object(app_mod.AppKit, "NSApplication"):
            app_mod.AISWifiApp._on_toggle_login_item(fake_app, None)
        return set_enabled, open_settings, alert

    def test_off_by_default_and_toggles(self):
        li = app_mod.login_item
        set_enabled, _, alert = self.toggle(li.DISABLED, li.ENABLED)
        set_enabled.assert_called_once_with(True)
        alert.assert_not_called()
        set_enabled, _, _ = self.toggle(li.ENABLED, li.DISABLED)
        set_enabled.assert_called_once_with(False)

    def test_explains_how_to_install_when_not_an_app(self):
        set_enabled, _, alert = self.toggle(app_mod.login_item.UNAVAILABLE)
        set_enabled.assert_not_called()
        self.assertIn("make_app.py", alert.call_args[0][1])

    def test_requires_approval_offers_settings(self):
        li = app_mod.login_item
        _, open_settings, _ = self.toggle(li.DISABLED, li.REQUIRES_APPROVAL, clicked=1)
        open_settings.assert_called_once()


@unittest.skipIf(app_mod is None, "requires macOS + rumps")
class RemainingFormatTests(unittest.TestCase):
    def test_format_minutes_above_5_and_seconds_below(self):
        f = app_mod._format_remaining
        self.assertEqual(f(642), "10m")   # >= 5 min: minutes only
        self.assertEqual(f(300), "5m")
        self.assertEqual(f(299), "4:59")  # < 5 min: live M:SS
        self.assertEqual(f(59), "0:59")
        self.assertEqual(f(0), "0:00")
        self.assertEqual(f(-5), "0:00")
        self.assertEqual(f(None), "")

    def test_refresh_ticks_down_locally(self):
        app = mock.Mock()
        app.cfg = {"show_time_in_menubar": True, "language": "en"}
        app._t = app_mod.AISWifiApp._t.__get__(app)  # real translations
        snap = {
            "status": app_mod.ST_ONLINE, "ssid": None, "provider": "AIS SUPER WiFi",
            "message": "Connected", "remaining_seconds": 120,
            "remaining_at": 1000.0, "remaining_unlimited": False,
        }
        with mock.patch.object(app_mod.time, "monotonic", return_value=1005.0):
            app_mod.AISWifiApp._render(app, snap)
        # 120 measured, exactly 5 s elapsed → 1:55, shown in the bar and the menu.
        self.assertEqual(app.title, "🛜 1:55")
        self.assertEqual(app.time_item.title, "Time left: 1:55")

    def test_render_thai_status_label(self):
        app = mock.Mock()
        app.cfg = {"show_time_in_menubar": True, "language": "th"}
        app._t = app_mod.AISWifiApp._t.__get__(app)
        app_mod.AISWifiApp._render(app, {"status": app_mod.ST_ONLINE, "ssid": None,
                                         "provider": None, "message": ""})
        self.assertIn("เชื่อมต่อแล้ว", app.status_item.title)  # "Connected" in Thai


@unittest.skipIf(app_mod is None, "requires macOS + rumps")
class LanguageMenuTests(unittest.TestCase):
    def test_selecting_thai_persists_and_reapplies(self):
        app = mock.Mock()
        app.cfg = {"language": "en"}
        with mock.patch.object(app_mod.config_mod, "save_config") as save:
            app_mod.AISWifiApp._set_language(app, "th")
        self.assertEqual(app.cfg["language"], "th")
        save.assert_called_once()
        app._apply_language.assert_called_once()

    def test_sync_lang_checks(self):
        app = mock.Mock()
        app.cfg = {"language": "th"}
        app_mod.AISWifiApp._sync_lang_checks(app)
        self.assertEqual((app.lang_en.state, app.lang_th.state), (0, 1))

    def test_retitle_sets_all_titles_without_missing_keys(self):
        app = mock.Mock()
        app.cfg = {"language": "th"}
        app._t = app_mod.AISWifiApp._t.__get__(app)
        app_mod.AISWifiApp._retitle(app)
        self.assertEqual(app.login_now_item.title, "เชื่อมต่อเดี๋ยวนี้")  # Connect Now
        self.assertEqual(app.quit_item.title, "ออก")                      # Quit
        self.assertIn("v", app.about_item.title)                          # About (v…)

    def test_apply_language_reapplies_everything(self):
        app = mock.Mock()
        app.cfg = {"language": "en"}
        app_mod.AISWifiApp._apply_language(app)
        app._retitle.assert_called_once()
        app._render.assert_called_once()
        app._sync_login_item.assert_called_once()


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
