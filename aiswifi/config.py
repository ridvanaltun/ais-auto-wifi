"""
Configuration and credential management.

- Settings are kept in a plain JSON file:  ~/.config/aiswifi/config.json
- Secrets (phone number + password) are stored in the macOS Keychain and are
  NEVER written to the JSON file. Without `keyring` the app keeps running in
  the worst case, but it cannot remember credentials.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("aiswifi.config")

# --- Paths --------------------------------------------------------------------

CONFIG_DIR = Path(os.path.expanduser("~/.config/aiswifi"))
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "aiswifi.log"

KEYRING_SERVICE = "aiswifi"

# --- Default settings ---------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    # Is automatic login enabled?
    "auto_login": True,
    # How often (in seconds) the connection is checked.
    "poll_interval": 15,
    # Preferred login method: "password" (recommended) or "otp". With "otp" the
    # app asks you for the code in a dialog (the SMS cannot be read from
    # Messages on a captive portal — there is no internet to receive it yet).
    "login_method": "password",
    # Preferred provider key; None means automatic detection.
    "preferred_provider": None,
    # How many times a failed login attempt is retried.
    "max_retries": 3,
    # Wait after a failure (seconds) — base and cap of the exponential backoff.
    "backoff_base": 5,
    "backoff_max": 120,
    # Timeout for HTTP requests (seconds).
    "http_timeout": 12,
    # AIS portal login URL (can be changed if needed).
    "ais_login_url": "https://ext-activities.ais.co.th/apps/wifigen/login.aspx",
    # AIS endpoint that reports the logged-in session and its remaining time.
    "ais_status_url": "https://wifi.ais.co.th/checkStatusLogon",
    # Show the remaining session time in the menu bar next to the icon.
    "show_time_in_menubar": True,
    # Restrict HTTP connections to IPv4. Captive portals are IPv4-only and IPv6
    # attempts fail with "Network is unreachable"; turn off only to debug.
    "force_ipv4": True,
    # Show informational notifications?
    "notifications": True,
    # OPTIONAL: certificate pinning for portals that present their own
    # (self-signed/corporate) certificate, e.g. {"portal.example.com": "<SHA-256 hex>"}.
    # Only portal requests to that host are verified against this fingerprint
    # instead of the certificate chain. The connectivity probe is NEVER
    # affected. Empty means full verification.
    "portal_cert_pins": {},
    # Extra hosts (besides *.ais.co.th) that AIS credentials may be sent to,
    # e.g. ["10.0.0.1"] if your AIS hotspot's login form posts to a gateway.
    # Credentials are never sent to any other host.
    "trusted_portal_hosts": [],
    # UI language: "en" (default) or "th" (Thai). Changeable from the menu.
    "language": "en",
}

# Settings that only accept specific values.
_CHOICES: Dict[str, Tuple[str, ...]] = {
    "login_method": ("password", "otp"),
    "language": ("en", "th"),
}


class KeychainError(Exception):
    """The Keychain could not be accessed (the message can be shown to the user)."""


def _is_valid(key: str, value: Any) -> bool:
    """Does a setting's type/range match its default?"""
    default = DEFAULT_CONFIG[key]
    if key in _CHOICES:
        return value in _CHOICES[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        # Zero/negative intervals would turn the loop into a busy wait.
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    if isinstance(default, str):
        return isinstance(value, str) and bool(value.strip())
    if isinstance(default, dict):
        return isinstance(value, dict)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return value is None or isinstance(value, str)  # preferred_provider


def ensure_config_dir() -> None:
    """Make sure the configuration directory exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Dict[str, Any]:
    """Read settings; fill in missing keys with defaults."""
    cfg = {k: (type(v)(v) if isinstance(v, (dict, list)) else v)
           for k, v in DEFAULT_CONFIG.items()}
    try:
        ensure_config_dir()
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k not in DEFAULT_CONFIG:
                        continue
                    if _is_valid(k, v):
                        cfg[k] = v
                    else:
                        # A wrong type (e.g. "poll_interval": "15s") must not
                        # crash the monitor thread: fall back to the default.
                        logger.warning("config.json: invalid value for '%s' (%r), using the default.", k, v)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read config.json, using defaults: %s", exc)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    """Write settings to disk (known keys only)."""
    clean = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        ensure_config_dir()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False, indent=2)
        tmp.replace(CONFIG_PATH)
    except OSError as exc:
        logger.error("Could not write config.json: %s", exc)


# --- Credentials (Keychain) -----------------------------------------------------

def _account_name(provider_key: str, field: str) -> str:
    return f"{provider_key}:{field}"


def set_credentials(provider_key: str, phone: str, password: str) -> bool:
    """Save phone + password to the Keychain. Returns True on success."""
    try:
        import keyring  # type: ignore
    except Exception as exc:  # keyring is not installed
        logger.error("keyring not found, cannot save credentials: %s", exc)
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, _account_name(provider_key, "phone"), phone or "")
        keyring.set_password(KEYRING_SERVICE, _account_name(provider_key, "password"), password or "")
        return True
    except Exception as exc:
        logger.error("Could not write to the Keychain: %s", exc)
        return False


def get_credentials(provider_key: str,
                    raise_errors: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """
    Return the saved (phone, password) pair, or (None, None) if there is none.

    With raise_errors=True an access ERROR is distinguished from "nothing
    saved" and KeychainError is raised, so the user is not wrongly told that
    no credentials exist.
    """
    try:
        import keyring  # type: ignore
    except Exception as exc:
        if raise_errors:
            raise KeychainError("keyring is not installed (pip3 install keyring)") from exc
        return (None, None)
    try:
        phone = keyring.get_password(KEYRING_SERVICE, _account_name(provider_key, "phone"))
        password = keyring.get_password(KEYRING_SERVICE, _account_name(provider_key, "password"))
        return (phone or None, password or None)
    except Exception as exc:
        logger.error("Could not read the Keychain: %s", exc)
        if raise_errors:
            raise KeychainError("Could not access the Keychain — approve the access prompt") from exc
        return (None, None)


def clear_credentials(provider_key: str) -> None:
    """Delete saved credentials."""
    try:
        import keyring  # type: ignore
    except Exception:
        return
    for field in ("phone", "password"):
        try:
            keyring.delete_password(KEYRING_SERVICE, _account_name(provider_key, field))
        except Exception:
            pass


# --- Logging --------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    """Log to both a file and the console."""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("aiswifi")
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_error: Optional[OSError] = None
    try:
        ensure_config_dir()
        # Rotating log: the file must not grow forever in a long-running app.
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:  # read-only home directory etc.: console only
        file_error = exc

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
    if file_error is not None:
        root.warning("Could not open the log file (%s); logging to the console only.", file_error)
