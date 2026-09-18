"""
Menu bar (tray) interface — built with rumps.

- The icon shows the connection status (connected / disconnected / logging in).
- From the menu: connect now, toggle automatic login, enter credentials,
  choose the method, open the log file, quit.
- Network work runs in the background (Monitor thread); the UI only shows
  the status. The status is read safely by a Timer on the main loop.
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

STATUS_TEXT = {
    ST_CHECKING: "Checking…",
    ST_ONLINE: "Connected",
    ST_OFFLINE: "No network",
    ST_CAPTIVE: "Connection lost",
    ST_LOGGING_IN: "Logging in…",
    ST_ERROR: "Error",
    ST_IDLE: "Auto login off",
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

        # --- Menu ---------------------------------------------------------------
        self.status_item = rumps.MenuItem("Status: —")
        self.detail_item = rumps.MenuItem("")
        self.time_item = rumps.MenuItem("Time left: —")

        self.login_now_item = rumps.MenuItem("Connect Now", callback=self._on_login_now)
        self.auto_item = rumps.MenuItem("Auto Connect", callback=self._on_toggle_auto)
        self.auto_item.state = 1 if self.cfg.get("auto_login") else 0
        # Off by default; the user opts in. macOS keeps the real state (it can
        # also be changed in System Settings → General → Login Items).
        self.login_item_menu = rumps.MenuItem("Open at Login", callback=self._on_toggle_login_item)
        self._sync_login_item()

        self.creds_item = rumps.MenuItem("Enter Credentials…", callback=self._on_set_credentials)

        # Login method submenu
        self.method_pw = rumps.MenuItem("Password (recommended)", callback=self._on_method_password)
        self.method_otp = rumps.MenuItem("SMS OTP", callback=self._on_method_otp)
        self._sync_method_checks()

        # Where the SMS OTP code comes from (only used with the SMS OTP method).
        self.otp_src_messages = rumps.MenuItem("Read code from Messages",
                                               callback=self._on_otp_source_messages)
        self.otp_src_ask = rumps.MenuItem("Ask me in a window",
                                          callback=self._on_otp_source_ask)
        self._sync_otp_source_checks()

        # Permissions submenu — shows live state and opens the right Settings
        # pane. The parent gets a ⚠️ when the current settings need a missing one.
        # (Only Full Disk Access is actionable: the app never requests Location,
        # so it does not appear in that list, and the SSID is only cosmetic.)
        self.perm_menu = rumps.MenuItem("Permissions")
        self.perm_fda = rumps.MenuItem("Full Disk Access", callback=self._on_open_fda)
        self.perm_menu.add(self.perm_fda)
        self._sync_permissions()

        self.log_item = rumps.MenuItem("Open Logs", callback=self._on_open_log)
        self.about_item = rumps.MenuItem(f"About (v{__version__})", callback=self._on_about)
        self.quit_item = rumps.MenuItem("Quit", callback=self._on_quit)

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
            {"Login Method": [self.method_pw, self.method_otp]},
            {"SMS OTP Code": [self.otp_src_messages, self.otp_src_ask]},
            self.perm_menu,
            None,
            self.log_item,
            self.about_item,
            None,
            self.quit_item,
        ]

        self._last_status: Optional[str] = None
        self._error_notified = False  # no repeated notifications for the same problem
        self._ticks = 0

        # Start the background monitor
        self.monitor.start()

        # Refresh the UI periodically (on the main loop) — thread-safe.
        self._ui_timer = rumps.Timer(self._refresh_ui, 1.0)
        self._ui_timer.start()

    # ---- UI refresh --------------------------------------------------------------

    def _refresh_ui(self, _timer) -> None:
        snap = self.monitor.state.snapshot()
        status = snap["status"]
        icon = STATUS_ICON.get(status, "🛜")

        # Remaining session time (the portal countdown). It is measured only
        # every poll cycle; here it is ticked down locally each second from the
        # measurement time, so the display counts down without extra requests.
        label = ""
        if status == ST_ONLINE:
            if snap.get("remaining_unlimited"):
                label = "Unlimited"
            elif snap.get("remaining_seconds") is not None and snap.get("remaining_at"):
                live = snap["remaining_seconds"] - (time.monotonic() - snap["remaining_at"])
                label = _format_remaining(live)
        if label and label != "Unlimited" and self.cfg.get("show_time_in_menubar", True):
            self.title = f"{icon} {label}"
        elif label == "Unlimited" and self.cfg.get("show_time_in_menubar", True):
            self.title = f"{icon} ∞"
        else:
            self.title = icon
        self.time_item.title = f"Time left: {label}" if label else "Time left: —"

        st_text = STATUS_TEXT.get(status, status)
        ssid = snap.get("ssid") or "—"
        self.status_item.title = f"Status: {st_text}"

        detail = snap.get("message") or ""
        prov = snap.get("provider")
        parts = []
        if ssid and ssid != "—":
            parts.append(f"Network: {ssid}")
        if prov:
            parts.append(prov)
        if detail:
            parts.append(detail)
        self.detail_item.title = "  •  ".join(parts) if parts else "Ready"

        # Notify on status changes
        if status != self._last_status:
            self._notify_transition(self._last_status, status, detail)
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
            self._notify("Connected", "Internet access is back.")
        elif new == ST_CAPTIVE and old in (ST_ONLINE, ST_OFFLINE, ST_CHECKING):
            # Only on a real disconnect; not in the failed-attempt loop (ERROR→CAPTIVE).
            self._notify("Connection lost", "Trying to log in automatically…")
        elif new == ST_ERROR and not self._error_notified:
            self._error_notified = True
            self._notify("Login failed", detail or "Will retry.")

    # ---- Menu actions ------------------------------------------------------------

    def _on_login_now(self, _sender) -> None:
        self.monitor.trigger_login()
        self.detail_item.title = "Login request sent…"

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
            rumps.alert(
                "Open at Login",
                "Open at Login is available when the app is installed as a Mac app "
                "(macOS 13 or later).\n\nIn Terminal, in the project folder, run:\n"
                "python3 make_app.py\n\n"
                f"then open “{__app_name__}” from Applications.",
            )
            return
        new_status, error = login_item.set_enabled(current != login_item.ENABLED)
        if error:
            _bring_to_front()
            rumps.alert("Open at Login", f"Could not change the login item:\n{error}")
        elif new_status == login_item.REQUIRES_APPROVAL:
            _bring_to_front()
            clicked = rumps.alert(
                "Open at Login",
                f"macOS needs your approval: turn on “{__app_name__}” in "
                "System Settings → General → Login Items.",
                ok="Open Settings", cancel="Later",
            )
            if clicked == 1:
                login_item.open_login_items_settings()
        self._sync_login_item()

    def _on_method_password(self, _sender) -> None:
        self.cfg["login_method"] = "password"
        config_mod.save_config(self.cfg)
        self._sync_method_checks()

    def _on_method_otp(self, _sender) -> None:
        self.cfg["login_method"] = "otp"
        config_mod.save_config(self.cfg)
        self._sync_method_checks()
        if self.cfg.get("otp_source") == "messages" and not otp.can_read_messages():
            self._explain_full_disk_access()

    def _explain_full_disk_access(self) -> None:
        """
        macOS never prompts for Full Disk Access (reads are silently denied),
        so the only thing the app can do is explain it and open the right
        System Settings pane.
        """
        _bring_to_front()
        if login_item.running_as_app():
            who = (f"add “{__app_name__}” (from the Applications folder), "
                   "then quit and reopen this app.")
        else:
            who = ("add the app you started this from (e.g. Terminal), then quit and "
                   "restart it. Tip: install it as a Mac app with make_app.py so the "
                   f"permission belongs to “{__app_name__}” itself.")
        clicked = rumps.alert(
            title="Full Disk Access needed",
            message=(
                "To read the SMS code automatically, the app needs Full Disk Access "
                "to the Messages database. macOS does not let apps ask for this "
                "permission, so it has to be granted manually:\n\n"
                f"System Settings → Privacy & Security → Full Disk Access → {who}\n\n"
                "Until then, the app will ask you for the code in a dialog."
            ),
            ok="Open Settings", cancel="Later",
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
        # Only meaningful with the SMS OTP method; warn if Messages is unreadable.
        if self.cfg.get("login_method") == "otp" and not otp.can_read_messages():
            self._explain_full_disk_access()

    def _on_otp_source_ask(self, _sender) -> None:
        self.cfg["otp_source"] = "ask"
        config_mod.save_config(self.cfg)
        self._sync_otp_source_checks()

    def _sync_otp_source_checks(self) -> None:
        source = self.cfg.get("otp_source", "messages")
        self.otp_src_messages.state = 1 if source == "messages" else 0
        self.otp_src_ask.state = 1 if source == "ask" else 0

    def _sync_permissions(self) -> None:
        """Reflect permission state in the Permissions submenu (live)."""
        fda_ok = otp.can_read_messages()
        self.perm_fda.state = 1 if fda_ok else 0
        self.perm_fda.title = ("Full Disk Access: granted" if fda_ok
                               else "Full Disk Access: not granted — for SMS OTP auto-read")
        # A ⚠️ on the parent only when the current settings actually need it:
        # SMS OTP method reading the code from Messages, but access is missing.
        needs_fda = (self.cfg.get("login_method") == "otp"
                     and self.cfg.get("otp_source") == "messages" and not fda_ok)
        self.perm_menu.title = "Permissions  ⚠️" if needs_fda else "Permissions"

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
            title=f"{provider.name} — Phone Number",
            message="Enter the phone number registered with AIS (e.g. 08xxxxxxxx):",
            default_text=(saved_phone or ""),
            ok="Next", cancel="Cancel", dimensions=(300, 24),
        )
        resp = phone_win.run()
        if not resp.clicked:
            return
        phone = resp.text.strip()
        if not phone:
            rumps.alert("Missing information", "The phone number cannot be empty.")
            return

        pass_win = rumps.Window(
            title=f"{provider.name} — Password",
            message="Enter your AIS SUPER WiFi password.\n"
                    "(Leave it empty to keep the saved password; a password is not "
                    "needed if you only use SMS OTP.)",
            default_text="",
            ok="Save", cancel="Cancel", dimensions=(300, 24),
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
            self._notify("Saved",
                         f"{provider.name} credentials were saved to the Keychain.")
            self.monitor.trigger_login()
        else:
            rumps.alert("Error",
                        "Could not save the credentials. Is 'keyring' installed?\n"
                        "Terminal: pip install keyring")

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
                self._notify("SMS OTP required", "Enter the code sent to your phone in the dialog.")
                _bring_to_front()
                win = rumps.Window(
                    title="SMS OTP",
                    message="Enter the verification code sent to your phone:",
                    ok="Submit", cancel="Cancel", dimensions=(160, 24),
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
            rumps.alert("Could not open logs", str(exc))

    def _on_about(self, _sender) -> None:
        rumps.alert(
            f"{__app_name__} v{__version__}",
            "Automatically logs in to AIS SUPER WiFi and similar captive portals.\n\n"
            "• Automatic re-login when the connection drops\n"
            "• Password or SMS OTP method\n"
            "• Extensible with new providers\n\n"
            "Your credentials are stored in the macOS Keychain.",
        )

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
