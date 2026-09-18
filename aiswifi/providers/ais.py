"""
AIS SUPER WiFi provider.

Portal: wifi.ais.co.th / ext-activities.ais.co.th (ASP.NET based).
There are two login methods:
  - Number + password  (recommended: no OTP needed, fully automatic)
  - Number + SMS OTP

The login flow itself comes from the generic engine (BaseProvider); only
detection (matches) and the default login URL are customised here.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable, List, Optional
from urllib.parse import parse_qsl, unquote_plus, urlsplit, urlunsplit

from .base import BaseProvider

logger = logging.getLogger("aiswifi.provider.ais")

# Default login URL used when none comes from the configuration.
DEFAULT_AIS_LOGIN_URL = "https://ext-activities.ais.co.th/apps/wifigen/login.aspx"

# The AIS portal (wifi.ais.co.th) reports the logged-in session — including the
# countdown that ends the connection — from this endpoint. A POST returns JSON
# keyed by the client's MAC/IP, so it needs no cookies or credentials.
DEFAULT_AIS_STATUS_URL = "https://wifi.ais.co.th/checkStatusLogon"

_HMS_RE = re.compile(r"^\s*(\d+):([0-5]?\d):([0-5]?\d)\s*$")


def _first(value):
    """The portal returns each field as a one-element list; unwrap it."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _hms_to_seconds(text: Optional[str]) -> Optional[int]:
    """'HH:MM:SS' → total seconds, or None if it does not match."""
    if not isinstance(text, str):
        return None
    m = _HMS_RE.match(text)
    if not m:
        return None
    h, mm, ss = (int(g) for g in m.groups())
    return h * 3600 + mm * 60 + ss


def parse_status(payload) -> Optional[dict]:
    """
    Parse a checkStatusLogon response into a session dict, or None.

    The body is a JSON string that itself contains JSON, and every value is a
    one-element list, e.g. {"logonStatus":["true"],"remainingTime":["00:10:42"]}.
    """
    data = payload
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if not isinstance(data, dict):
        return None
    online = str(_first(data.get("logonStatus")) or "").strip().lower() == "true"
    unlimited = str(_first(data.get("unlimitedAccount")) or "").strip().lower() == "true"
    remaining_text = _first(data.get("remainingTime"))
    remaining_seconds = None if unlimited else _hms_to_seconds(remaining_text)
    return {
        "online": online,
        "unlimited": unlimited,
        "remaining_seconds": remaining_seconds,
        "remaining_text": "Unlimited" if unlimited else (remaining_text or None),
        "session_text": _first(data.get("sessionTime")),
    }

# "AIS" must start a word in the SSID (".@ AIS SUPER WiFi", "AIS_WiFi");
# names like "Thais Cafe" or "Raisin" must not match.
_AIS_SSID_RE = re.compile(r"(?<![a-z])ais", re.IGNORECASE)


def _is_ais_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "ais.co.th" or host.endswith(".ais.co.th")


class AISProvider(BaseProvider):
    key = "ais"
    name = "AIS SUPER WiFi"
    supports_status = True

    def __init__(self, login_url: Optional[str] = None,
                 trusted_hosts: Optional[Iterable[str]] = None,
                 status_url: Optional[str] = None):
        self.login_url = login_url or DEFAULT_AIS_LOGIN_URL
        self.status_url = status_url or DEFAULT_AIS_STATUS_URL
        # Extra hosts (besides *.ais.co.th) that AIS credentials may be sent to,
        # from "trusted_portal_hosts" in config.json.
        self.trusted_hosts = {h.strip().lower() for h in (trusted_hosts or [])
                              if isinstance(h, str) and h.strip()}

    def login_url_candidates(self, portal_url: Optional[str]) -> List[str]:
        """
        1) The page the network actually redirected to: before logging in, the
           fixed URL is often unreachable because it is outside the portal's
           walled garden (DNS fails or the connection is reset).
        2) The fixed login URL, with the portal's session parameters.
        Credentials are only submitted to trusted hosts (is_trusted_submit_url).
        """
        urls = [portal_url] if portal_url else []
        fixed = self.resolve_login_url(portal_url)
        if fixed and fixed not in urls:
            urls.append(fixed)
        return urls

    def is_trusted_submit_url(self, url: str) -> bool:
        """AIS credentials only go to AIS domains or hosts the user explicitly trusts."""
        host = (urlsplit(url).hostname or "").lower()
        return _is_ais_host(url) or host in self.trusted_hosts

    def session_status(self, session, http_timeout: float = 8.0) -> Optional[dict]:
        """Query checkStatusLogon for the current session and remaining time."""
        try:
            resp = session.post(
                self.status_url,
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Referer": "https://wifi.ais.co.th/"},
                timeout=min(http_timeout, 8.0),
            )
        except Exception as exc:  # not on the AIS network, or endpoint unreachable
            logger.debug("[ais] Could not query the session status: %s", exc)
            return None
        if getattr(resp, "status_code", 0) != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        try:
            return parse_status(payload)
        except (ValueError, TypeError) as exc:
            logger.debug("[ais] Could not parse the session status: %s", exc)
            return None

    def matches(self, ssid: Optional[str], portal_url: Optional[str],
                page_html: Optional[str]) -> bool:
        # The SSID contains AIS
        if ssid and _AIS_SSID_RE.search(ssid):
            return True
        # The portal redirected to an AIS domain
        if portal_url and _is_ais_host(portal_url):
            return True
        # The page content has AIS traces
        if page_html and ("ais.co.th" in page_html.lower() or "wifi by ais" in page_html.lower()):
            return True
        return False

    def resolve_login_url(self, portal_url: Optional[str]) -> Optional[str]:
        """
        Use the fixed login URL, but KEEP the session-specific query
        parameters (e.g. mac/ip/nasid) from the portal's redirect URL.

        Scheme/host/path always come from the fixed URL: credentials only ever
        go to AIS's HTTPS address (no downgrade to HTTP, no other host).
        Parameters are appended raw (so signed queries are not broken); for a
        parameter with the same name, the fixed URL's value wins.
        """
        if not self.login_url or not portal_url:
            return self.login_url
        base = urlsplit(self.login_url)
        own_keys = {k for k, _ in parse_qsl(base.query, keep_blank_values=True)}
        extra = [seg for seg in urlsplit(portal_url).query.split("&")
                 if seg and unquote_plus(seg.split("=", 1)[0]) not in own_keys]
        if not extra:
            return self.login_url
        query = "&".join([base.query] + extra if base.query else extra)
        return urlunsplit(base._replace(query=query))
