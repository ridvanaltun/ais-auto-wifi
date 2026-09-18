"""
Menu bar (tray) interface — built with rumps.

- The icon shows the connection status (connected / disconnected / logging in).
- From the menu: connect now, toggle automatic login, enter credentials,
  choose the method, pick the language, open the log file, quit.
- Network work runs in the background (Monitor thread); the UI only shows
  the status. The status is read safely by a Timer on the main loop.
- All user-facing text goes through `i18n` (English default, Thai optional).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Optional

import AppKit  # type: ignore
import rumps
from Foundation import NSThread  # type: ignore
from PyObjCTools import AppHelper  # type: ignore

from . import __app_name__, __version__
from . import config as config_mod
from . import i18n
from . import monitor as monitor_mod
from .monitor import (
    ST_CAPTIVE, ST_CHECKING, ST_ERROR, ST_IDLE, ST_LOGGING_IN, ST_OFFLINE, ST_ONLINE,
)
from . import login_item, otp
from . import providers as providers_mod

logger = logging.getLogger("aiswifi.app")

# System Settings → Privacy & Security → Full Disk Access
FULL_DISK_ACCESS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"

# Menu bar title (short icon) per status. Emoji are used instead of text.
STATUS_ICON = {
    ST_CHECKING: "⏳",
    ST_ONLINE: "🛜",
    ST_OFFLINE: "📴",
    ST_CAPTIVE: "🔒",
    ST_LOGGING_IN: "🔄",
    ST_ERROR: "⚠️",
    ST_IDLE: "⏸️",
}

# Status code -> i18n key for the human status word.
STATUS_KEY = {
    ST_CHECKING: "status.checking",
    ST_ONLINE: "status.online",
    ST_OFFLINE: "status.offline",
    ST_CAPTIVE: "status.captive",
    ST_LOGGING_IN: "status.logging_in",
    ST_ERROR: "status.error",
    ST_IDLE: "status.idle",
}


def _format_remaining(seconds: Optional[int]) -> str:
    """
    Format the remaining session time so it does not read like a wall clock:
    - 5 minutes or more → whole minutes only, e.g. "12m" (changes once a minute),
    - under 5 minutes   → a live "M:SS" that ticks every second (e.g. "4:59"),
    - zero/negative     → "0:00".
    """
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    if seconds >= 300:
        return f"{seconds // 60}m"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _bring_to_front() -> None:
    """
    Bring the app to the front before opening a dialog. A menu bar app runs
    in the background; if it is not active, the dialog is visible but the
    keyboard focus stays with the frontmost app and typing never reaches the
    text field.
    """
    nsapp = AppKit.NSApplication.sharedApplication()
    nsapp.activateIgnoringOtherApps_(True)
    if nsapp.respondsToSelector_("activate"):  # macOS 14+ API
        nsapp.activate()


class AISWifiApp(rumps.App):
    def __init__(self):
        super().__init__(name=__app_name__, title="🛜", quit_button=None)
        self.cfg = config_mod.load_config()

        # Set up the monitor
        self.monitor = monitor_mod.Monitor(self.cfg)
        self.monitor.set_ask_otp_callback(self._ask_otp)

        self._last_status: Optional[str] = None
        self._error_notified = False  # no repeated notifications for the same problem
        self._ticks = 0

        # --- Menu items ---------------------------------------------------------
        # rumps keys each menu item by its title, so titles must be set (and be
        # distinct within a menu) BEFORE the items are wired together — hence
        # _retitle()/_render() run before .add(...) and before self.menu.
        self.status_item = rumps.MenuItem("")
        self.detail_item = rumps.MenuItem("")
        self.time_item = rumps.MenuItem("")
        self.login_now_item = rumps.MenuItem("", callback=self._on_login_now)
        self.auto_item = rumps.MenuItem("", callback=self._on_toggle_auto)
        self.auto_item.state = 1 if self.cfg.get("auto_login") else 0
        self.login_item_menu = rumps.MenuItem("", callback=self._on_toggle_login_item)
        self.creds_item = rumps.MenuItem("", callback=self._on_set_credentials)
        self.method_menu = rumps.MenuItem("")
        self.method_pw = rumps.MenuItem("", callback=self._on_method_password)
        self.method_otp = rumps.MenuItem("", callback=self._on_method_otp)
        self.otp_src_menu = rumps.MenuItem("")
        self.otp_src_messages = rumps.MenuItem("", callback=self._on_otp_source_messages)
        self.otp_src_ask = rumps.MenuItem("", callback=self._on_otp_source_ask)
        self.perm_menu = rumps.MenuItem("")
        self.perm_fda = rumps.MenuItem("", callback=self._on_open_fda)
        self.lang_menu = rumps.MenuItem("")
        # Language labels are always shown in their own script (constant).
        self.lang_en = rumps.MenuItem(i18n.LANGUAGES["en"], callback=self._on_lang_en)
        self.lang_th = rumps.MenuItem(i18n.LANGUAGES["th"], callback=self._on_lang_th)
        self.log_item = rumps.MenuItem("", callback=self._on_open_log)
        self.about_item = rumps.MenuItem("", callback=self._on_about)
        self.quit_item = rumps.MenuItem("", callback=self._on_quit)

        # Titles first (so submenu/menu keys are distinct), then wire it up.
        self._retitle()
        self._render(self.monitor.state.snapshot())
        self.method_menu.add(self.method_pw)
        self.method_menu.add(self.method_otp)
        self.otp_src_menu.add(self.otp_src_messages)
        self.otp_src_menu.add(self.otp_src_ask)
        self.perm_menu.add(self.perm_fda)
        self.lang_menu.add(self.lang_en)
        self.lang_menu.add(self.lang_th)

        self.menu = [
            self.status_item,
            self.detail_item,
            self.time_item,
            None,  # separator
            self.login_now_item,
            self.auto_item,
            self.login_item_menu,
            None,
            self.creds_item,
            self.method_menu,
            self.otp_src_menu,
            self.perm_menu,
            None,
            self.lang_menu,
            self.log_item,
            self.about_item,
            None,
            self.quit_item,
        ]

        # Reflect current settings/state in the checkmarks and dynamic titles.
        self._sync_method_checks()
        self._sync_otp_source_checks()
        self._sync_lang_checks()
        self._sync_login_item()
        self._sync_permissions()

        # Start the background monitor
        self.monitor.start()

        # Refresh the UI periodically (on the main loop) — thread-safe.
        self._ui_timer = rumps.Timer(self._refresh_ui, 1.0)
        self._ui_timer.start()

    # ---- Localization ------------------------------------------------------------

    def _t(self, key: str, **kw) -> str:
        return i18n.t(key, self.cfg.get("language", "en"), **kw)

    def _retitle(self) -> None:
        """Set every static menu title for the current language (display only)."""
        self.login_now_item.title = self._t("menu.connect_now")
        self.auto_item.title = self._t("menu.auto_connect")
        self.login_item_menu.title = self._t("menu.open_at_login")
        self.creds_item.title = self._t("menu.enter_credentials")
        self.method_menu.title = self._t("menu.login_method")
        self.method_pw.title = self._t("menu.method_password")
        self.method_otp.title = self._t("menu.method_otp")
        self.otp_src_menu.title = self._t("menu.otp_source")
        self.otp_src_messages.title = self._t("menu.otp_messages")
        self.otp_src_ask.title = self._t("menu.otp_ask")
        self.lang_menu.title = self._t("menu.language")
        self.perm_menu.title = self._t("perm.menu")        # ⚠️ added by _sync_permissions
        self.perm_fda.title = self._t("perm.fda_denied")   # corrected by _sync_permissions
        self.log_item.title = self._t("menu.open_logs")
        self.about_item.title = self._t("menu.about", version=__version__)
        self.quit_item.title = self._t("menu.quit")

    def _apply_language(self) -> None:
        """Re-render every user-facing string for the current language."""
        self._retitle()
        self._sync_method_checks()
        self._sync_otp_source_checks()
        self._sync_lang_checks()
        self._sync_login_item()
        self._sync_permissions()
        self._render(self.monitor.state.snapshot())

    def _sync_lang_checks(self) -> None:
        lang = i18n.normalize(self.cfg.get("language", "en"))
        self.lang_en.state = 1 if lang == "en" else 0
        self.lang_th.state = 1 if lang == "th" else 0

    def _on_lang_en(self, _sender) -> None:
        self._set_language("en")

    def _on_lang_th(self, _sender) -> None:
        self._set_language("th")

    def _set_language(self, lang: str) -> None:
        self.cfg["language"] = lang
        config_mod.save_config(self.cfg)
        self._apply_language()  # monitor messages follow on their next cycle

    # ---- UI refresh --------------------------------------------------------------

    def _render(self, snap: dict) -> None:
        """Update the status/detail/time items and the menu bar title."""
        status = snap["status"]
        icon = STATUS_ICON.get(status, "🛜")

        # Remaining session time (the portal countdown). It is measured only
        # every poll cycle; here it is ticked down locally each second from the
        # measurement time, so the display counts down without extra requests.
        label = ""
        if status == ST_ONLINE:
            if snap.get("remaining_unlimited"):
                label = self._t("time.unlimited")
            elif snap.get("remaining_seconds") is not None and snap.get("remaining_at"):
                live = snap["remaining_seconds"] - (time.monotonic() - snap["remaining_at"])
                label = _format_remaining(live)
        show_bar = self.cfg.get("show_time_in_menubar", True)
        if label and label != self._t("time.unlimited") and show_bar:
            self.title = f"{icon} {label}"
        elif label and show_bar:  # unlimited
            self.title = f"{icon} ∞"
        else:
            self.title = icon
        self.time_item.title = "{0}: {1}".format(
            self._t("label.time_left"), label if label else "—")

        st_text = self._t(STATUS_KEY.get(status, "status.checking"))
        self.status_item.title = "{0}: {1}".format(self._t("label.status"), st_text)

        detail = snap.get("message") or ""
        prov = snap.get("provider")
        ssid = snap.get("ssid")
        parts = []
        if ssid:
            parts.append("{0}: {1}".format(self._t("label.network"), ssid))
        if prov:
            parts.append(prov)
        if detail:
            parts.append(detail)
        self.detail_item.title = "  •  ".join(parts) if parts else self._t("label.ready")

    def _refresh_ui(self, _timer) -> None:
        snap = self.monitor.state.snapshot()
        self._render(snap)

        # Notify on status changes
        status = snap["status"]
        if status != self._last_status:
            self._notify_transition(self._last_status, status, snap.get("message") or "")
            self._last_status = status

        # Pick up Login Items / permission changes made in System Settings.
        self._ticks += 1
        if self._ticks % 10 == 0:
            self._sync_login_item()
            self._sync_permissions()

    def _notify(self, title: str, message: str) -> None:
        """Show a notification (main thread). Silently skip if there is no notification center."""
        if not self.cfg.get("notifications", True):
            return
        try:
            rumps.notification(__app_name__, title, message)
        except Exception as exc:  # e.g. no CFBundleIdentifier in Info.plist
            logger.debug("Could not show notification: %s", exc)

    def _notify_transition(self, old: Optional[str], new: str, detail: str) -> None:
        if new in (ST_ONLINE, ST_OFFLINE, ST_IDLE):
            self._error_notified = False  # the problem period is over
        if old is None:
            return  # no notification on first launch
        if new == ST_ONLINE and old in (ST_CAPTIVE, ST_LOGGING_IN, ST_ERROR):
            self._notify(self._t("notif.connected.title"), self._t("notif.connected.body"))
        elif new == ST_CAPTIVE and old in (ST_ONLINE, ST_OFFLINE, ST_CHECKING):
            # Only on a real disconnect; not in the failed-attempt loop (ERROR→CAPTIVE).
            self._notify(self._t("notif.lost.title"), self._t("notif.lost.body"))
        elif new == ST_ERROR and not self._error_notified:
            self._error_notified = True
            self._notify(self._t("notif.failed.title"), detail or self._t("notif.failed.body"))

    # ---- Menu actions ------------------------------------------------------------

    def _on_login_now(self, _sender) -> None:
        self.monitor.trigger_login()
        self.detail_item.title = self._t("msg.login_request_sent")

    def _on_toggle_auto(self, sender) -> None:
        new_val = not bool(sender.state)
        sender.state = 1 if new_val else 0
        self.cfg["auto_login"] = new_val
        config_mod.save_config(self.cfg)
        self.monitor.set_auto(new_val)

    def _sync_login_item(self) -> None:
        self.login_item_menu.state = 1 if login_item.status() == login_item.ENABLED else 0

    def _on_toggle_login_item(self, _sender) -> None:
        current = login_item.status()
        if current == login_item.UNAVAILABLE:
            _bring_to_front()
            rumps.alert(self._t("dlg.login_item.title"),
                        self._t("dlg.login_item.unavailable", app=__app_name__))
            return
        new_status, error = login_item.set_enabled(current != login_item.ENABLED)
        if error:
            _bring_to_front()
            rumps.alert(self._t("dlg.login_item.title"),
                        self._t("dlg.login_item.error", error=error))
        elif new_status == login_item.REQUIRES_APPROVAL:
            _bring_to_front()
            clicked = rumps.alert(
                self._t("dlg.login_item.title"),
                self._t("dlg.login_item.approval", app=__app_name__),
                ok=self._t("btn.open_settings"), cancel=self._t("btn.later"),
            )
            if clicked == 1:
                login_item.open_login_items_settings()
        self._sync_login_item()

    def _on_method_password(self, _sender) -> None:
        self.cfg["login_method"] = "password"
        config_mod.save_config(self.cfg)
        self._sync_method_checks()
        self._sync_permissions()

    def _on_method_otp(self, _sender) -> None:
        self.cfg["login_method"] = "otp"
        config_mod.save_config(self.cfg)
        self._sync_method_checks()
        self._sync_permissions()
        if self.cfg.get("otp_source") == "messages" and not otp.can_read_messages():
            self._explain_full_disk_access()

    def _explain_full_disk_access(self) -> None:
        """
        macOS never prompts for Full Disk Access (reads are silently denied),
        so the only thing the app can do is explain it and open the right
        System Settings pane.
        """
        _bring_to_front()
        who_key = "dlg.fda.who_app" if login_item.running_as_app() else "dlg.fda.who_terminal"
        who = self._t(who_key, app=__app_name__)
        clicked = rumps.alert(
            title=self._t("dlg.fda.title"),
            message=self._t("dlg.fda.msg", who=who),
            ok=self._t("btn.open_settings"), cancel=self._t("btn.later"),
        )
        if clicked == 1:
            subprocess.run(["open", FULL_DISK_ACCESS_URL], check=False)

    def _sync_method_checks(self) -> None:
        is_pw = self.cfg.get("login_method", "password") == "password"
        self.method_pw.state = 1 if is_pw else 0
        self.method_otp.state = 0 if is_pw else 1

    def _on_otp_source_messages(self, _sender) -> None:
        self.cfg["otp_source"] = "messages"
        config_mod.save_config(self.cfg)
        self._sync_otp_source_checks()
        self._sync_permissions()
        # Only meaningful with the SMS OTP method; warn if Messages is unreadable.
        if self.cfg.get("login_method") == "otp" and not otp.can_read_messages():
            self._explain_full_disk_access()

    def _on_otp_source_ask(self, _sender) -> None:
        self.cfg["otp_source"] = "ask"
        config_mod.save_config(self.cfg)
        self._sync_otp_source_checks()
        self._sync_permissions()

    def _sync_otp_source_checks(self) -> None:
        source = self.cfg.get("otp_source", "messages")
        self.otp_src_messages.state = 1 if source == "messages" else 0
        self.otp_src_ask.state = 1 if source == "ask" else 0

    def _sync_permissions(self) -> None:
        """Reflect permission state in the Permissions submenu (live)."""
        fda_ok = otp.can_read_messages()
        self.perm_fda.state = 1 if fda_ok else 0
        self.perm_fda.title = self._t("perm.fda_granted" if fda_ok else "perm.fda_denied")
        # A ⚠️ on the parent only when the current settings actually need it:
        # SMS OTP method reading the code from Messages, but access is missing.
        needs_fda = (self.cfg.get("login_method") == "otp"
                     and self.cfg.get("otp_source") == "messages" and not fda_ok)
        self.perm_menu.title = self._t("perm.menu") + ("  ⚠️" if needs_fda else "")

    def _on_open_fda(self, _sender) -> None:
        self._explain_full_disk_access()

    def _on_set_credentials(self, _sender) -> None:
        # For which provider? The preferred one if set, otherwise AIS.
        registry = providers_mod.build_registry(self.cfg.get("ais_login_url"),
                                                self.cfg.get("trusted_portal_hosts"),
                                                self.cfg.get("ais_status_url"))
        pref = self.cfg.get("preferred_provider") or "ais"
        provider = providers_mod.get_provider_by_key(registry, pref) or registry[0]
        saved_phone, saved_password = config_mod.get_credentials(provider.key)

        _bring_to_front()
        phone_win = rumps.Window(
            title=self._t("dlg.phone.title", provider=provider.name),
            message=self._t("dlg.phone.msg"),
            default_text=(saved_phone or ""),
            ok=self._t("btn.next"), cancel=self._t("btn.cancel"), dimensions=(300, 24),
        )
        resp = phone_win.run()
        if not resp.clicked:
            return
        phone = resp.text.strip()
        if not phone:
            rumps.alert(self._t("dlg.missing.title"), self._t("dlg.missing.msg"))
            return

        pass_win = rumps.Window(
            title=self._t("dlg.password.title", provider=provider.name),
            message=self._t("dlg.password.msg"),
            default_text="",
            ok=self._t("btn.save"), cancel=self._t("btn.cancel"), dimensions=(300, 24),
            secure=True,  # so nobody looking at the screen in a café sees the password
        )
        _bring_to_front()
        resp2 = pass_win.run()
        if not resp2.clicked:
            return
        # Don't wipe the password of a user who only wants to update the phone number.
        password = resp2.text.strip() or (saved_password or "")

        ok = config_mod.set_credentials(provider.key, phone, password)
        if ok:
            self._notify(self._t("notif.saved.title"),
                         self._t("notif.saved.body", provider=provider.name))
            self.monitor.trigger_login()
        else:
            rumps.alert(self._t("dlg.creds_error.title"), self._t("dlg.creds_error.msg"))

    def _ask_otp(self) -> Optional[str]:
        """
        Ask the user for the OTP. Called from the monitor (worker) thread.

        AppKit rule: windows may ONLY be opened on the main thread. rumps.Timer
        cannot be used here — start() adds the timer to the calling thread's
        run loop (which never runs), so the dialog would never open. That is
        why AppHelper.callAfter (performSelectorOnMainThread) is used.
        """
        result: dict = {}
        done = threading.Event()

        def show() -> None:
            try:
                self._notify(self._t("notif.otp.title"), self._t("notif.otp.body"))
                _bring_to_front()
                win = rumps.Window(
                    title=self._t("dlg.otp.title"),
                    message=self._t("dlg.otp.msg"),
                    ok=self._t("btn.submit"), cancel=self._t("btn.cancel"), dimensions=(160, 24),
                )
                r = win.run()
                result["code"] = r.text.strip() if r.clicked else None
            except Exception as exc:
                logger.error("Could not open the OTP dialog: %s", exc)
            finally:
                done.set()  # never leave the waiting thread hanging, even on errors

        if NSThread.isMainThread():
            show()
            return result.get("code") or None

        try:
            AppHelper.callAfter(show)
        except Exception as exc:
            # Opening the window on the worker thread would crash AppKit; give up.
            logger.error("Could not hand the OTP dialog over to the main thread: %s", exc)
            return None
        # Wait for the user while the dialog is open, so that no second dialog
        # and no new SMS are triggered when a timeout expires. Give up if the app is quitting.
        while not done.wait(0.5):
            if self.monitor.stopping:
                return None
        return result.get("code") or None

    def _on_open_log(self, _sender) -> None:
        try:
            subprocess.run(["open", str(config_mod.LOG_PATH)], check=False)
        except Exception as exc:
            rumps.alert(self._t("dlg.logs_error.title"), str(exc))

    def _on_about(self, _sender) -> None:
        rumps.alert(self._t("dlg.about.title", app=__app_name__, version=__version__),
                    self._t("dlg.about.msg"))

    def _on_quit(self, _sender) -> None:
        try:
            self.monitor.stop()
        finally:
            rumps.quit_application()


def run() -> None:
    config_mod.setup_logging(verbose=False)
    logger.info("Starting %s v%s", __app_name__, __version__)
    # A non-framework Python (pyenv, Homebrew etc.) GUI process starts with
    # the "Prohibited" activation policy: it can never become active and text
    # fields in its dialogs receive no keyboard input. The right policy for a
    # menu bar app is Accessory (no Dock icon, but windows can take focus).
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory)
    AISWifiApp().run()
