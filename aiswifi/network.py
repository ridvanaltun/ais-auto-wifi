"""
Network helpers: internet/captive-portal detection, SSID reading, interface lookup.

Design notes:
- Captive portal detection follows an approach similar to Apple's: a known
  "probe" URL is requested. With real internet access it returns a fixed
  "Success" response. If a portal intercepts, the response differs (a
  redirect or the portal page) → a login is required.
- Reading the SSID may require Location permission on macOS Sonoma+, so it is
  NOT critical; the provider can also be detected from the portal's redirect
  URL.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import ssl
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urljoin, urlsplit

import requests
import urllib3.util.connection as _u3_connection
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

logger = logging.getLogger("aiswifi.network")

# Captive portals are almost always IPv4-only. On macOS, when the host has an
# IPv4-mapped IPv6 address (or the network advertises IPv6 with no route), the
# default AF_UNSPEC resolution makes urllib3 try IPv6 first and fail with
# "[Errno 51] Network is unreachable". Forcing IPv4 avoids that.
_ORIG_GAI_FAMILY = _u3_connection.allowed_gai_family
_FORCE_IPV4 = True


def set_ip_family(force_ipv4: bool) -> None:
    """Choose whether HTTP connections are restricted to IPv4 (default: yes)."""
    global _FORCE_IPV4
    _FORCE_IPV4 = force_ipv4


def _apply_ip_family() -> None:
    _u3_connection.allowed_gai_family = (
        (lambda: socket.AF_INET) if _FORCE_IPV4 else _ORIG_GAI_FAMILY
    )

# The URL macOS uses for captive portal detection. With real internet access
# it returns a small HTML page whose body is exactly "Success".
APPLE_PROBE_URL = "http://captive.apple.com/hotspot-detect.html"
APPLE_PROBE_EXPECT = "Success"

# Fallback probes: URLs expected to return no content (204).
FALLBACK_PROBES = [
    "http://www.gstatic.com/generate_204",
    "http://connectivitycheck.gstatic.com/generate_204",
]

# Hosts of the probe URLs: these are NEVER treated as a portal URL and
# certificate pinning is never applied to them.
PROBE_HOSTS = frozenset(urlsplit(u).hostname for u in [APPLE_PROBE_URL, *FALLBACK_PROBES])

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# States
ONLINE = "online"     # real internet access
CAPTIVE = "captive"   # network available but a portal blocks it → login required
OFFLINE = "offline"   # no network/access at all (e.g. Wi-Fi is off)

# In-page JavaScript redirect: location.href = "..." / location.replace("...").
# The lookbehind keeps XML/HTML attributes such as
# `noNamespaceSchemaLocation="…"` from being mistaken for `location=…`.
_JS_REDIRECT_RE = re.compile(
    r"""(?<![\w.])(?:window\.|document\.|self\.|top\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']"""
    r"""|(?<![\w.])(?:window\.|document\.|self\.|top\.)?location\.(?:replace|assign)\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)

# HTML comments — a captive page may carry a WISPr XML block in one, which is
# data, not a redirect for us to follow.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass
class ProbeResult:
    state: str                      # ONLINE / CAPTIVE / OFFLINE
    portal_url: Optional[str] = None  # portal URL reached, if captive
    body: Optional[str] = None        # portal page HTML, if captive


def new_session() -> requests.Session:
    """A browser-like HTTP session that follows redirects."""
    _apply_ip_family()
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"})
    s.trust_env = False  # ignore system proxy settings (can break behind a portal)
    return s


def _is_probe_host(url: Optional[str]) -> bool:
    try:
        return (urlsplit(url or "").hostname or "") in PROBE_HOSTS
    except ValueError:
        return False


def find_redirect_url(html: Optional[str], base_url: str) -> Optional[str]:
    """
    Find the meta-refresh or JavaScript redirect target on a portal page.
    Some portals use these with a 200 response instead of an HTTP redirect.
    """
    if not html:
        return None
    target = None
    try:
        soup = BeautifulSoup(html, "html.parser")
        for meta in soup.find_all("meta"):
            if (meta.get("http-equiv") or "").strip().lower() == "refresh":
                m = re.search(r"url\s*=\s*['\"]?([^'\";]+)", meta.get("content") or "", re.I)
                if m:
                    target = m.group(1).strip()
                    break
    except Exception as exc:  # broken HTML must not break the probe
        logger.debug("Could not parse the portal page: %s", exc)
    if target is None:
        m = _JS_REDIRECT_RE.search(_HTML_COMMENT_RE.sub(" ", html))
        if m:
            target = (m.group(1) or m.group(2) or "").strip()
    if not target:
        return None
    url = urljoin(base_url, target)
    if urlsplit(url).scheme not in ("http", "https") or _is_probe_host(url):
        return None
    return url


def probe_connectivity(session: Optional[requests.Session] = None,
                       timeout: float = 8.0) -> ProbeResult:
    """
    Return the internet connectivity state.

    Logic:
      1) Request the Apple probe URL.
         - Body is "Success" → ONLINE.
         - Different content / a redirect → CAPTIVE (with the portal HTML).
      2) If the Apple probe is unreachable, the fallback 204 URLs are tried.
         - A 204 → ONLINE.
      3) If none of them worked → OFFLINE.
    """
    if session is not None:
        return _probe(session, timeout)
    sess = new_session()
    try:
        return _probe(sess, timeout)
    finally:
        sess.close()


def _probe(sess: requests.Session, timeout: float) -> ProbeResult:
    # 1) Apple probe
    try:
        resp = sess.get(APPLE_PROBE_URL, timeout=timeout, allow_redirects=True)
        text = (resp.text or "").strip()
        if resp.status_code == 200 and APPLE_PROBE_EXPECT in text and len(text) < 200:
            return ProbeResult(ONLINE)
        # 200 without the expected content, or we were redirected → a portal
        # intercepted. The host is compared: "captive.apple.com" appearing in
        # the portal URL's query (e.g. ?url=http://captive.apple.com/...) is very common.
        if resp.url and not _is_probe_host(resp.url):
            portal_url = resp.url
        else:
            portal_url = find_redirect_url(resp.text, resp.url or APPLE_PROBE_URL)
        return ProbeResult(CAPTIVE, portal_url=portal_url, body=resp.text)
    except requests.RequestException as exc:
        logger.debug("Apple probe failed: %s", exc)

    # 2) Fallback 204 probes
    for url in FALLBACK_PROBES:
        try:
            r = sess.get(url, timeout=timeout, allow_redirects=False)
            if r.status_code == 204:
                return ProbeResult(ONLINE)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                return ProbeResult(CAPTIVE, portal_url=urljoin(url, loc) if loc else None)
            # 511 = "Network Authentication Required" (RFC 6585, captive portal).
            if r.status_code == 511 or (r.status_code == 200 and r.text):
                return ProbeResult(CAPTIVE, portal_url=find_redirect_url(r.text, url),
                                   body=r.text)
        except requests.RequestException:
            continue

    # 3) Nothing was reachable
    return ProbeResult(OFFLINE)


# --- Portal certificate pinning (optional) ------------------------------------

class _PinnedCertAdapter(HTTPAdapter):
    """
    For a SINGLE portal host only: instead of the certificate chain, the
    connection is allowed if the SHA-256 fingerprint the user put into the
    config matches exactly. If it does not match, urllib3 aborts the
    connection (SSLError). Other hosts, and redirects to other hosts, do NOT
    use this adapter; they go through normal verification.
    """

    def __init__(self, fingerprint: str, **kwargs):
        self._fingerprint = fingerprint
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["assert_fingerprint"] = self._fingerprint
        super().init_poolmanager(*args, **kwargs)

    def send(self, request, **kwargs):
        # assert_fingerprint is checked instead of chain/hostname (see init_poolmanager).
        kwargs["verify"] = False
        return super().send(request, **kwargs)


def apply_cert_pins(session: requests.Session, pins: Optional[Dict[str, str]]) -> None:
    """
    Apply the `portal_cert_pins` entries ({host: sha256_hex}) from
    config.json to the session. Pinning is refused for the probe hosts.
    """
    if not pins or not isinstance(pins, dict):
        return
    for host, fp in pins.items():
        host_l = str(host).strip().lower()
        fp_hex = re.sub(r"[^0-9a-f]", "", str(fp).lower())
        if not host_l or "/" in host_l or len(fp_hex) != 64:
            logger.warning("Ignored invalid portal_cert_pins entry: %r", host)
            continue
        if host_l in PROBE_HOSTS:
            logger.warning("Connectivity probe hosts cannot be pinned, ignored: %s", host_l)
            continue
        adapter = _PinnedCertAdapter(fp_hex)
        session.mount(f"https://{host_l}/", adapter)
        session.mount(f"https://{host_l}:", adapter)
        logger.warning("Certificate pinning enabled for '%s' (this portal only).", host_l)


def cert_fingerprint(url: str, timeout: float = 5.0) -> Optional[str]:
    """
    Return the SHA-256 fingerprint of the certificate PRESENTED by an HTTPS URL.
    For information only (so the user can see and verify it in the log):
    only a TLS handshake is performed, NO data is sent over this connection.
    """
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            return None
        ctx = ssl.create_default_context()
        # Needed to be able to SHOW the fingerprint of an unverifiable certificate.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=parts.hostname) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ValueError) as exc:
        logger.debug("Could not get the certificate fingerprint: %s", exc)
        return None
    return hashlib.sha256(der).hexdigest() if der else None


# --- SSID / interface detection (macOS) ------------------------------------------

def get_wifi_interface() -> str:
    """
    Find the name of the Wi-Fi hardware interface (usually 'en0').
    Parses the output of `networksetup -listallhardwareports`.
    """
    try:
        out = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        # "Hardware Port: Wi-Fi\nDevice: en0\n..."
        blocks = out.split("Hardware Port:")
        for b in blocks:
            if "Wi-Fi" in b or "AirPort" in b:
                m = re.search(r"Device:\s*(\w+)", b)
                if m:
                    return m.group(1)
    except Exception as exc:
        logger.debug("Wi-Fi interface not found: %s", exc)
    return "en0"


def get_ssid() -> Optional[str]:
    """
    Return the name (SSID) of the connected Wi-Fi network, or None.

    Tried in order:
      1) CoreWLAN (PyObjC) — most reliable, but needs Location permission on Sonoma+.
      2) `networksetup -getairportnetwork <iface>`
      3) The old `airport -I` tool (may be missing on newer macOS).
    """
    # 1) CoreWLAN
    try:
        from CoreWLAN import CWWiFiClient  # type: ignore
        iface = CWWiFiClient.sharedWiFiClient().interface()
        if iface is not None:
            ssid = iface.ssid()
            if ssid:
                return str(ssid)
    except Exception as exc:
        logger.debug("Could not read SSID via CoreWLAN: %s", exc)

    # 2) networksetup
    try:
        iface = get_wifi_interface()
        out = subprocess.run(
            ["networksetup", "-getairportnetwork", iface],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        # "Current Wi-Fi Network: AIS SUPER WIFI". Error output also contains
        # ":" ("** Error: Error obtaining wireless information."), so the prefix is matched.
        m = re.search(r"Current (?:Wi-Fi|AirPort) Network:\s*(.+)", out)
        if m:
            return m.group(1).strip() or None
    except Exception as exc:
        logger.debug("Could not read SSID via networksetup: %s", exc)

    # 3) Old airport tool
    airport = ("/System/Library/PrivateFrameworks/Apple80211.framework/"
               "Versions/Current/Resources/airport")
    try:
        out = subprocess.run([airport, "-I"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"^\s*SSID:\s*(.+)$", out, re.MULTILINE)
        if m:
            return m.group(1).strip() or None
    except Exception:
        pass

    return None
