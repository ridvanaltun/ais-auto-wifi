"""
"Open at Login" support via ServiceManagement's SMAppService (macOS 13+).

It only works when running from the app bundle built by make_app.py: the
login item is the app itself, so it also shows up (and can be turned off)
under System Settings → General → Login Items.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional, Tuple

from . import __bundle_id__

logger = logging.getLogger("aiswifi.login_item")

ENABLED = "enabled"
DISABLED = "disabled"
REQUIRES_APPROVAL = "requires_approval"   # registered, waiting for approval in System Settings
UNAVAILABLE = "unavailable"               # not running as the app, or macOS < 13

LOGIN_ITEMS_SETTINGS_URL = "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"


def running_as_app() -> bool:
    """Is this process running from the app bundle built by make_app.py?"""
    try:
        from Foundation import NSBundle  # type: ignore
    except ImportError:
        return False
    return NSBundle.mainBundle().bundleIdentifier() == __bundle_id__


def _service():
    """The SMAppService for this app, or None if Open at Login is unavailable."""
    if not running_as_app():
        return None
    try:
        from ServiceManagement import SMAppService  # type: ignore
        return SMAppService.mainAppService()
    except Exception as exc:  # bindings missing or macOS < 13
        logger.info("Open at Login is unavailable: %s", exc)
        return None


def status() -> str:
    """ENABLED / DISABLED / REQUIRES_APPROVAL / UNAVAILABLE."""
    service = _service()
    if service is None:
        return UNAVAILABLE
    from ServiceManagement import (  # type: ignore
        SMAppServiceStatusEnabled, SMAppServiceStatusRequiresApproval,
    )
    value = service.status()
    if value == SMAppServiceStatusEnabled:
        return ENABLED
    if value == SMAppServiceStatusRequiresApproval:
        return REQUIRES_APPROVAL
    return DISABLED


def set_enabled(enabled: bool) -> Tuple[str, Optional[str]]:
    """Register or unregister the app as a login item. Returns (new status, error or None)."""
    service = _service()
    if service is None:
        return UNAVAILABLE, None
    if enabled:
        ok, error = service.registerAndReturnError_(None)
    else:
        ok, error = service.unregisterAndReturnError_(None)
    message = None
    if not ok:
        message = str(error.localizedDescription()) if error is not None else "unknown error"
        logger.error("Could not %s the login item: %s",
                     "register" if enabled else "unregister", message)
    return status(), message


def open_login_items_settings() -> None:
    """Open System Settings → General → Login Items."""
    try:
        from ServiceManagement import SMAppService  # type: ignore
        SMAppService.openSystemSettingsLoginItems()
    except Exception:
        subprocess.run(["open", LOGIN_ITEMS_SETTINGS_URL], check=False)
