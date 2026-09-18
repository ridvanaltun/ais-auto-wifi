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

import re
from typing import Optional
from urllib.parse import parse_qsl, unquote_plus, urlsplit, urlunsplit

from .base import BaseProvider

# Default login URL used when none comes from the configuration.
DEFAULT_AIS_LOGIN_URL = "https://ext-activities.ais.co.th/apps/wifigen/login.aspx"

# "AIS" must start a word in the SSID (".@ AIS SUPER WiFi", "AIS_WiFi");
# names like "Thais Cafe" or "Raisin" must not match.
_AIS_SSID_RE = re.compile(r"(?<![a-z])ais", re.IGNORECASE)


def _is_ais_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "ais.co.th" or host.endswith(".ais.co.th")


class AISProvider(BaseProvider):
    key = "ais"
    name = "AIS SUPER WiFi"

    def __init__(self, login_url: Optional[str] = None):
        self.login_url = login_url or DEFAULT_AIS_LOGIN_URL

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
