"""
AIS SUPER WiFi provider.

Portal: wifi.ais.co.th — an Angular single-page app, so there is no HTML form
to scrape. It authenticates through a small JSON API instead, which this
provider talks to directly:
  - Number + password  (recommended: no OTP needed, fully automatic)
  - Number + SMS OTP    (register triggers the SMS; the code is the password)

Only the AIS origin (or a host the user explicitly trusts) ever receives the
credentials.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .base import BaseProvider

logger = logging.getLogger("aiswifi.provider.ais")

# The AIS captive portal (an Angular app on wifi.ais.co.th) authenticates
# through a small JSON API rather than an HTML form, so this provider talks to
# that API directly. The API lives at the portal's own origin.
DEFAULT_AIS_API_BASE = "https://wifi.ais.co.th"
DEFAULT_AIS_LOGIN_URL = DEFAULT_AIS_API_BASE  # kept for config compatibility

# Endpoint that reports the logged-in session — including the countdown that
# ends the connection. A POST returns JSON keyed by the client's MAC/IP, so it
# needs no cookies or credentials.
DEFAULT_AIS_STATUS_URL = DEFAULT_AIS_API_BASE + "/checkStatusLogon"

# Relative API endpoints (see the portal's own bundle).
_LOGIN_PATH = "login"        # POST txtUsername/txtPassword -> JSON logon result
_REGISTER_PATH = "register"  # POST txtMobile -> sends the SMS password

# A successful RADIUS reply carries this marker in replyMessage.
_SUCCESS_MARKER = "SBR-0000"

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


class _Secrets:
    """Minimal carrier so _report_request_error can redact phone/password."""
    __slots__ = ("phone", "password")

    def __init__(self, phone, password):
        self.phone, self.password = phone, password


def _as_dict(payload) -> Optional[dict]:
    """The API returns a JSON string that itself contains JSON; decode both."""
    data = payload
    for _ in range(3):
        if isinstance(data, dict):
            return data
        if isinstance(data, (str, bytes)):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                return None
        else:
            return None
    return data if isinstance(data, dict) else None


def parse_status(payload) -> Optional[dict]:
    """
    Parse a checkStatusLogon response into a session dict, or None.

    Every value is a one-element list, e.g.
    {"logonStatus":["true"],"remainingTime":["00:10:42"]}.
    """
    data = _as_dict(payload)
    if data is None:
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


def _human_reply(reply: str) -> str:
    """Pull the human part out of a RADIUS reply like '"SBR-2400";"…message…"'."""
    quoted = re.findall(r'"([^"]*)"', reply or "")
    for part in quoted:
        if part and not part.startswith("SBR-"):
            return part.strip()
    return (reply or "").strip()


def parse_logon(payload) -> Optional[dict]:
    """
    Parse a login/register response into {ok, logon, code, reply, message}.

    Success is signalled by the SBR-0000 marker in replyMessage (also returned
    when the IP is already logged on).
    """
    data = _as_dict(payload)
    if data is None:
        return None
    code = str(_first(data.get("responseCode")) or "")
    reply = str(_first(data.get("replyMessage")) or "")
    resp_msg = str(_first(data.get("responseMessage")) or "")
    logon = str(_first(data.get("logonStatus")) or "").strip().lower() == "true"
    username = _first(data.get("username")) or _first(data.get("userName"))
    ok = _SUCCESS_MARKER in reply or logon or resp_msg == "REGISTERED_SUCCESS"
    return {"ok": ok, "logon": logon, "code": code, "reply": reply,
            "username": str(username) if username else None,
            "message": _human_reply(reply) or resp_msg}

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

    # ---- Login (AIS JSON API, not an HTML form) ---------------------------------

    def _api_base(self, portal_url: Optional[str]) -> str:
        """
        The API origin. Prefer the origin the network redirected us to (so
        session query params and the exact host are honoured), but only when it
        is an AIS host or one the user explicitly trusts; otherwise the default.
        """
        for candidate in (portal_url, self.login_url):
            if not candidate:
                continue
            parts = urlsplit(candidate)
            host = (parts.hostname or "").lower()
            if parts.scheme in ("http", "https") and (_is_ais_host(candidate) or host in self.trusted_hosts):
                return f"{parts.scheme}://{parts.netloc}"
        return DEFAULT_AIS_API_BASE

    def _post_api(self, session, base: str, path: str, data: dict, timeout: float):
        return session.post(
            f"{base}/{path}", data=data,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": base + "/"},
            timeout=timeout, allow_redirects=True,
        )

    def login(self, session, ctx, portal_url: Optional[str] = None,
              method: str = "password", http_timeout: float = 12.0) -> bool:
        self.last_failure = ""
        base = self._api_base(portal_url)
        # Load the portal once so any session cookie it sets is in place.
        try:
            session.get(portal_url or base, timeout=http_timeout, allow_redirects=True)
        except Exception as exc:
            logger.debug("[ais] Could not open the portal page first: %s", exc)

        if method == "otp":
            ok = self._login_otp(session, ctx, base, http_timeout)
        else:
            ok = self._login_password(session, ctx, base, http_timeout)
        if not ok:
            return False
        return self._verify_online(session, http_timeout)

    def _submit_logon(self, session, base: str, username: str, password: str,
                      timeout: float) -> bool:
        try:
            resp = self._post_api(session, base, _LOGIN_PATH, {
                "txtUsername": username, "txtPassword": password,
                "chkRememberMe": "false",
            }, timeout)
        except Exception as exc:
            self._report_request_error("Could not submit the login", base, exc,
                                       ctx=_Secrets(username, password))
            return False
        try:
            info = parse_logon(resp.text)
        except (ValueError, TypeError):
            info = None
        if info is None:
            logger.error("[ais] Unexpected login response (HTTP %s).",
                         getattr(resp, "status_code", "?"))
            self.last_failure = "Unexpected portal response"
            return False
        if info["ok"]:
            logger.info("[ais] Login accepted by the portal.")
            return True
        logger.error("[ais] Login rejected: %s", info["message"] or info["code"])
        self.last_failure = info["message"] or "Login rejected by the portal"
        return False

    def _login_password(self, session, ctx, base: str, timeout: float) -> bool:
        if not ctx.phone or not ctx.password:
            logger.error("[ais] The password method needs a phone number and a password.")
            self.last_failure = "Phone number or password missing"
            return False
        return self._submit_logon(session, base, ctx.phone, ctx.password, timeout)

    def _login_otp(self, session, ctx, base: str, timeout: float) -> bool:
        if not ctx.phone:
            logger.error("[ais] The OTP method needs a phone number.")
            self.last_failure = "Phone number missing"
            return False
        if ctx.otp_provider is None:
            logger.error("[ais] No OTP provider function is set.")
            self.last_failure = "No OTP source configured"
            return False

        # Take the SMS baseline BEFORE requesting the code.
        if ctx.otp_prepare is not None:
            ctx.otp_prepare()
        # Register the number to trigger the SMS. The portal creates/looks up
        # the account and returns the username to log in with (which may differ
        # from the raw phone, e.g. "<phone>@aisads"); reuse it for the login.
        username = ctx.phone
        try:
            resp = self._post_api(session, base, _REGISTER_PATH, {
                "txtMobile": ctx.phone, "ddlOperator": "AIS", "ddlAge": "25-34",
                "txtLanguage": "EN", "chkAgree": "1",
            }, timeout)
            info = parse_logon(resp.text)
            if info and info.get("username"):
                username = info["username"]
            if info and not info["ok"]:
                # Not fatal: the account may already exist, but the SMS may
                # still have been sent. Log and keep waiting for the code.
                logger.info("[ais] Register replied: %s", info["message"] or info["code"])
        except Exception as exc:
            self._report_request_error("Could not request the SMS code", base, exc,
                                       ctx=_Secrets(ctx.phone, None))
            return False

        code = ctx.otp_provider()
        if not code:
            logger.error("[ais] Could not get the OTP code.")
            self.last_failure = "Could not get the OTP code"
            return False
        # The SMS code is the account's password.
        return self._submit_logon(session, base, username, code, timeout)

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
