#!/usr/bin/env python3
"""
AIS Wi-Fi Auto-Login — launcher.

Usage:
  python3 run.py             # start the menu bar app (requires rumps)
  python3 run.py --diagnose  # network/OTP/SSID diagnostics (no UI)
  python3 run.py --version   # print the version

Note: --diagnose and --version work even without rumps installed.
"""

from __future__ import annotations

import sys


def _mask(code: str) -> str:
    """Don't print the full OTP to the screen/a report (it may still be valid)."""
    return "•" * max(0, len(code) - 2) + code[-2:]


def _diagnose() -> int:
    """Test the network state and helpers without opening the UI."""
    from aiswifi import config as config_mod
    from aiswifi import login_item, network, otp
    from aiswifi import providers as providers_mod

    config_mod.setup_logging(verbose=True)
    print("== AIS Wi-Fi Auto-Login — Diagnostics ==\n")

    cfg = config_mod.load_config()
    print(f"Config file  : {config_mod.CONFIG_PATH}")
    print(f"Log file     : {config_mod.LOG_PATH}")
    print(f"Method       : {cfg.get('login_method')}")
    print(f"OTP source   : {cfg.get('otp_source')}")
    print(f"Running as   : {'Mac app' if login_item.running_as_app() else 'script'}")
    print(f"Open at Login: {login_item.status().replace('_', ' ')}\n")

    ssid = network.get_ssid()
    print(f"SSID         : {ssid or '(unreadable — Location permission may be required)'}")

    iface = network.get_wifi_interface()
    print(f"Wi-Fi iface  : {iface}")

    print("\nProbing connectivity…")
    result = network.probe_connectivity(timeout=8.0)
    print(f"Status       : {result.state}")
    if result.portal_url:
        print(f"Portal URL   : {result.portal_url}")
    if result.state == network.CAPTIVE and result.body:
        # The portal page helps to debug logins; it contains no credentials.
        snapshot = config_mod.CONFIG_DIR / "last_portal.html"
        try:
            snapshot.write_text(result.body, encoding="utf-8")
            print(f"Portal page  : saved to {snapshot}")
        except OSError as exc:
            print(f"Portal page  : could not be saved ({exc})")

    registry = providers_mod.build_registry(cfg.get("ais_login_url"),
                                            cfg.get("trusted_portal_hosts"),
                                            cfg.get("ais_status_url"))
    provider = providers_mod.detect_provider(
        registry, ssid, result.portal_url, result.body,
        preferred_key=cfg.get("preferred_provider"),
    )
    print(f"Provider     : {provider.name if provider else '(none found)'}")

    # Query the AIS session status directly (it needs no SSID/portal), so the
    # countdown shows even when the detected provider is the generic fallback.
    status_provider = next((p for p in registry if getattr(p, "supports_status", False)), None)
    if status_provider is not None:
        info = status_provider.session_status(network.new_session())
        if info and info.get("online"):
            extra = f" (session {info['session_text']})" if info.get("session_text") else ""
            print(f"Time left    : {info.get('remaining_text') or '—'}{extra}")
        elif info is not None:
            print("Time left    : not logged in on this network")
        else:
            print("Time left    : unavailable (not on the AIS network?)")

    if provider:
        try:
            phone, password = config_mod.get_credentials(provider.key, raise_errors=True)
            print(f"Phone saved  : {'yes' if phone else 'NO'}")
            print(f"Password set : {'yes' if password else 'NO'}")
        except config_mod.KeychainError as exc:
            print(f"Credentials  : unreadable ({exc})")

    print("\nChecking Messages (SMS) access…")
    if not otp.can_read_messages():
        print("Messages     : unreadable (no Full Disk Access, or chat.db not found)")
        print("               macOS never asks for this permission; grant it manually in")
        print("               System Settings → Privacy & Security → Full Disk Access to the")
        print("               app you run this from (e.g. Terminal), then restart that app.")
        print("               Only needed for the SMS OTP method.")
    else:
        print("Messages     : readable")
        code = otp.read_latest_otp(within_seconds=300)
        if code:
            print(f"OTP (last 5m): {_mask(code)} ({len(code)} digits)")
        else:
            print("OTP (last 5m): not found (is Text Message Forwarding on?)")

    print("\nDiagnostics complete.")
    return 0


def _version() -> int:
    from aiswifi import __app_name__, __version__
    print(f"{__app_name__} v{__version__}")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if "--version" in argv or "-v" in argv:
        return _version()
    if "--diagnose" in argv or "-d" in argv:
        return _diagnose()

    # Normal mode: menu bar app
    try:
        from aiswifi.app import run
    except ImportError as exc:
        print("The menu bar app requires 'rumps'.", file=sys.stderr)
        print(f"Details: {exc}", file=sys.stderr)
        print("Install: pip install rumps", file=sys.stderr)
        print("For testing only: python3 run.py --diagnose", file=sys.stderr)
        return 1
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
