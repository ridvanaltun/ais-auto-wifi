"""
Background monitor.

Continuously, in a separate thread:
  - probes the connection,
  - logs in automatically with the matching provider when a captive portal
    is detected,
  - keeps the status in a thread-safe `State` object.

The user interface (menu bar) reads this `State`; network work never blocks
the UI. This keeps the network (thread) and the UI (main loop) clearly
separated.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from . import network, otp, portal
from . import providers as providers_mod
from . import config as config_mod

logger = logging.getLogger("aiswifi.monitor")

# Status codes shown to the user
ST_IDLE = "idle"                # automatic login is off
ST_CHECKING = "checking"       # status not known yet, probing
ST_ONLINE = "online"           # internet access
ST_OFFLINE = "offline"         # no network (Wi-Fi off etc.)
ST_CAPTIVE = "captive"         # portal detected, login required
ST_LOGGING_IN = "logging_in"   # logging in
ST_ERROR = "error"             # last attempt failed

# With the OTP method every attempt triggers a NEW SMS; after a failure wait
# at least this long (to avoid an SMS flood and the carrier's rate limit).
OTP_MIN_BACKOFF = 60.0


@dataclass
class State:
    """Lock-protected shared state read by the user interface."""
    status: str = ST_IDLE
    ssid: Optional[str] = None
    provider: Optional[str] = None
    message: str = ""
    last_login_ts: float = 0.0
    last_error: str = ""
    # Remaining session time (portal countdown), when known.
    remaining_seconds: Optional[int] = None
    remaining_text: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "ssid": self.ssid,
                "provider": self.provider,
                "message": self.message,
                "last_login_ts": self.last_login_ts,
                "last_error": self.last_error,
                "remaining_seconds": self.remaining_seconds,
                "remaining_text": self.remaining_text,
            }


class Monitor:
    def __init__(self, cfg: Dict[str, Any],
                 on_change: Optional[Callable[[Dict[str, Any]], None]] = None):
        # The cfg dict is SHARED with the UI: it is written only on the main
        # (UI) thread; this class's thread only reads individual keys.
        self.cfg = cfg
        self.state = State()
        # Called when the status changes (optional). NOTE: it is called from
        # the monitor thread; it must NOT call AppKit/rumps.
        self.on_change = on_change

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()       # wake the loop immediately
        self._login_now = threading.Event()  # on wake-up, force a login attempt
        self._ask_otp_cb: Optional[Callable[[], Optional[str]]] = None
        self._was_auto = bool(cfg.get("auto_login"))
        self._registry = providers_mod.build_registry(cfg.get("ais_login_url"),
                                                      cfg.get("trusted_portal_hosts"),
                                                      cfg.get("ais_status_url"))
        # Provider used to report the remaining session time while online.
        self._status_provider: Optional[Any] = None

        self.state.status = ST_CHECKING if cfg.get("auto_login") else ST_IDLE

    # ---- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aiswifi-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def trigger_login(self) -> None:
        """Called when the user clicks 'Connect Now'."""
        self._login_now.set()
        self._wake.set()

    def set_auto(self, enabled: bool) -> None:
        self.cfg["auto_login"] = enabled
        if not enabled:
            self._set_state(status=ST_IDLE, message="Auto login is off")
        else:
            # "Connected" is not shown before it is verified; the loop probes right away.
            self._set_state(status=ST_CHECKING, message="Checking…")
            self._wake.set()

    # ---- Main loop -------------------------------------------------------------

    def _run(self) -> None:
        session = network.new_session()
        network.apply_cert_pins(session, self.cfg.get("portal_cert_pins"))
        backoff = 0.0
        try:
            while not self._stop.is_set():
                try:
                    backoff = self._cycle(session, backoff)
                except Exception as exc:
                    # An unexpected error must NOT kill the thread; otherwise
                    # monitoring silently stops and the icon freezes (e.g. "Connected").
                    logger.error("Unexpected error in the monitor loop: %s", portal.redact(exc))
                    logger.debug("Details:\n%s", portal.redact(traceback.format_exc()))
                    self._set_state(status=ST_ERROR, message="Unexpected error (Open Logs)",
                                    last_error="internal")
                    self._stop.wait(5.0)
        finally:
            session.close()

    def _cycle(self, session, backoff: float) -> float:
        """One iteration of the loop; returns the backoff for the next iteration."""
        interval = float(self.cfg.get("poll_interval", 15))

        if not self.cfg.get("auto_login"):
            if self._was_auto:
                # A cycle still running when auto was turned off may have overwritten IDLE.
                self._was_auto = False
                self._set_state(status=ST_IDLE, message="Auto login is off")
            # Auto is off: only wait for a 'Connect Now' request.
            if self._wake.wait(timeout=1.0):
                self._wake.clear()
                if self._take_login_request():
                    self._attempt_cycle(session, forced=True)
            return 0.0
        self._was_auto = True

        # Refresh the SSID (informational; not required for detection).
        ssid = network.get_ssid()

        result = network.probe_connectivity(session, timeout=8.0)
        if result.state == network.ONLINE:
            backoff = 0.0
            self._set_state(status=ST_ONLINE, ssid=ssid,
                            message="Connected", last_error="")
            self._update_remaining(session, ssid)
        elif result.state == network.OFFLINE:
            backoff = 0.0
            self._set_state(status=ST_OFFLINE, ssid=ssid,
                            message="No network (Wi-Fi may be off)",
                            remaining_seconds=None, remaining_text="")
        else:  # CAPTIVE
            self._set_state(status=ST_CAPTIVE, ssid=ssid,
                            message="Connection lost, logging in…",
                            remaining_seconds=None, remaining_text="")
            logger.info("Captive portal detected: %s",
                        portal.redact(result.portal_url or "(no portal URL)"))
            ok = self._do_login(session, result, ssid)
            if ok:
                backoff = 0.0
            else:
                # Exponential backoff (wait before retrying after a failure)
                base = float(self.cfg.get("backoff_base", 5))
                cap = float(self.cfg.get("backoff_max", 120))
                backoff = min(cap, base if backoff == 0 else backoff * 2)
                if self.cfg.get("login_method") == "otp":
                    backoff = max(backoff, OTP_MIN_BACKOFF)

        # If 'Connect Now' arrives while waiting, wake up and try right away;
        # then start the wait over, so the forced attempt is not immediately
        # followed by an automatic one (with OTP that would be a second SMS).
        wait_for = max(interval, backoff)
        while self._wake.wait(timeout=wait_for):
            self._wake.clear()
            if self._stop.is_set() or not self._take_login_request():
                break  # stopping, or a plain wake-up (e.g. auto login turned on): probe now
            self._attempt_cycle(session, forced=True)
        return backoff

    def _take_login_request(self) -> bool:
        """If a 'Connect Now' request is pending, consume it and return True."""
        if self._login_now.is_set():
            self._login_now.clear()
            return True
        return False

    def _attempt_cycle(self, session, forced: bool) -> None:
        """One-off probe-and-log-in-if-needed cycle."""
        ssid = network.get_ssid()
        result = network.probe_connectivity(session, timeout=8.0)
        if result.state == network.ONLINE:
            self._set_state(status=ST_ONLINE, ssid=ssid, message="Already connected")
            return
        if result.state == network.OFFLINE and not forced:
            self._set_state(status=ST_OFFLINE, ssid=ssid, message="No network")
            return
        self._set_state(status=ST_LOGGING_IN, ssid=ssid, message="Logging in…")
        self._do_login(session, result, ssid)

    # ---- Login -----------------------------------------------------------------

    def _do_login(self, session, probe_result, ssid: Optional[str]) -> bool:
        portal_url = probe_result.portal_url
        page_html = probe_result.body

        provider = providers_mod.detect_provider(
            self._registry, ssid, portal_url, page_html,
            preferred_key=self.cfg.get("preferred_provider"),
        )
        if provider is None:
            self._set_state(status=ST_ERROR,
                            message="No suitable provider found",
                            last_error="no_provider")
            return False

        try:
            phone, password = config_mod.get_credentials(provider.key, raise_errors=True)
        except config_mod.KeychainError as exc:
            self._set_state(status=ST_ERROR, provider=provider.name,
                            message=str(exc), last_error="keychain")
            return False
        method = self.cfg.get("login_method", "password")

        if not phone:
            self._set_state(status=ST_ERROR,
                            provider=provider.name,
                            message="No credentials — use 'Enter Credentials…'",
                            last_error="no_credentials")
            return False
        if method == "password" and not password:
            self._set_state(status=ST_ERROR,
                            provider=provider.name,
                            message="No password saved — use 'Enter Credentials…'",
                            last_error="no_password")
            return False

        otp_prepare, otp_fn = self._make_otp_provider() if method == "otp" else (None, None)
        ctx = portal.LoginContext(
            phone=phone,
            password=password,
            otp_provider=otp_fn,
            otp_prepare=otp_prepare,
        )

        self._set_state(status=ST_LOGGING_IN, provider=provider.name,
                        message=f"{provider.name}: logging in…")

        # OTP: every attempt triggers a NEW SMS and waits ~otp_wait_timeout
        # seconds for the code; so only ONE attempt is made per cycle and the
        # retry is left to the monitor loop's backoff (at least OTP_MIN_BACKOFF).
        retries = 1 if method == "otp" else max(1, int(self.cfg.get("max_retries", 3)))
        timeout = float(self.cfg.get("http_timeout", 12))
        for attempt in range(1, retries + 1):
            if self._stop.is_set():
                return False
            logger.info("Login attempt %d/%d (%s, method=%s)",
                        attempt, retries, provider.key, method)
            try:
                ok = provider.login(session, ctx, portal_url=portal_url,
                                    method=method, http_timeout=timeout)
            except Exception as exc:
                logger.error("Error during login: %s", portal.redact(exc, phone, password))
                logger.debug("Details:\n%s",
                             portal.redact(traceback.format_exc(), phone, password))
                ok = False
            if ok:
                if getattr(provider, "supports_status", False):
                    self._status_provider = provider
                self._set_state(status=ST_ONLINE, provider=provider.name,
                                message="Login successful 🎉",
                                last_login_ts=time.time(), last_error="")
                self._update_remaining(session, ssid)
                return True
            if attempt < retries:
                self._stop.wait(2)

        reason = getattr(provider, "last_failure", "") or "Login failed"
        self._set_state(status=ST_ERROR, provider=provider.name,
                        message=f"{reason} (will retry)",
                        last_error="login_failed")
        return False

    def _status_capable_provider(self, ssid: Optional[str]):
        """
        The provider whose remaining time we can query while online: the one we
        logged in through, else the one matching the current network (so the
        countdown also shows when the app starts already connected).
        """
        if self._status_provider is not None:
            return self._status_provider
        provider = providers_mod.detect_provider(
            self._registry, ssid, None, None,
            preferred_key=self.cfg.get("preferred_provider"))
        if provider is not None and getattr(provider, "supports_status", False):
            return provider
        return None

    def _update_remaining(self, session, ssid: Optional[str]) -> None:
        """While online, refresh the remaining session time (portal countdown)."""
        provider = self._status_capable_provider(ssid)
        if provider is None:
            self._set_state(remaining_seconds=None, remaining_text="")
            return
        try:
            info = provider.session_status(session, float(self.cfg.get("http_timeout", 12)))
        except Exception as exc:
            logger.debug("Could not read the session status: %s", exc)
            info = None
        if not info or not info.get("online"):
            # Reachable but not an AIS session (e.g. home Wi-Fi): no countdown.
            self._set_state(remaining_seconds=None, remaining_text="")
            return
        self._set_state(remaining_seconds=info.get("remaining_seconds"),
                        remaining_text=info.get("remaining_text") or "")

    def _make_otp_provider(self) -> Tuple[Optional[Callable[[], None]],
                                          Callable[[], Optional[str]]]:
        """
        Build a (prepare, get-code) function pair based on the configuration:
          - "messages": read the SMS from the Messages database automatically;
                        if it cannot be read or the time runs out, ask the
                        user (if possible),
          - "ask":      ask via the UI (injected by the app),
          - "none":     no OTP.
        The prepare function is called BEFORE the request that triggers the SMS.
        """
        source = self.cfg.get("otp_source", "messages")
        timeout = int(self.cfg.get("otp_wait_timeout", 90))
        ask = self._ask_otp_cb

        def ask_user() -> Optional[str]:
            if ask is None:
                return None
            self._set_state(message="Waiting for the OTP code (enter it in the dialog)…")
            return ask()

        if source == "messages":
            box: Dict[str, Optional[int]] = {}

            def prepare() -> None:
                box["baseline"] = otp.current_baseline()

            def provider_fn() -> Optional[str]:
                if "baseline" in box:
                    baseline = box["baseline"]
                else:
                    # prepare() was not called (custom provider): old behaviour.
                    logger.warning("Taking the OTP reference point after the SMS request; "
                                   "a fast SMS may be missed.")
                    baseline = otp.current_baseline()
                if baseline is None:
                    logger.warning("The Messages database cannot be read (Full Disk Access "
                                   "may not be granted); the SMS cannot be read automatically.")
                else:
                    logger.info("Waiting for the SMS OTP (up to %ss)…", timeout)
                    self._set_state(message="Waiting for the SMS OTP…")
                    code = otp.wait_for_new_otp(baseline, timeout=timeout,
                                                stop_event=self._stop)
                    if code:
                        return code
                    logger.warning("No new SMS containing an OTP arrived in time.")
                return ask_user()

            return prepare, provider_fn

        if source == "ask" and ask is not None:
            return None, ask_user

        def none_fn() -> Optional[str]:
            return None
        return None, none_fn

    def set_ask_otp_callback(self, cb: Callable[[], Optional[str]]) -> None:
        """The UI can provide a callback for asking the user for the OTP."""
        self._ask_otp_cb = cb

    # ---- Helpers ---------------------------------------------------------------

    def _set_state(self, **kwargs: Any) -> None:
        self.state.update(**kwargs)
        if self.on_change:
            try:
                self.on_change(self.state.snapshot())
            except Exception:
                pass
