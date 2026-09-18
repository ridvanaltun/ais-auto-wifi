"""
Provider base class.

Every Wi-Fi provider derives from this class. To add a new free Wi-Fi
hotspot all you need to do is:
  - set `key` and `name`,
  - return whether we belong to this provider in `matches(...)`,
  - customise `login_url`/the success check if needed.

The default `login()` flow uses the generic form engine in `portal.py` and
works for most captive portals without rewriting anything.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from .. import network, portal

logger = logging.getLogger("aiswifi.provider")


class BaseProvider:
    key: str = "base"
    name: str = "Generic Provider"

    # Subclasses can put the login page URL here if they know it.
    login_url: Optional[str] = None

    # Short reason for the last failed attempt, shown to the user.
    # (Provider objects are only used on the monitor thread.)
    last_failure: str = ""

    # Whether session_status() can report the remaining session time.
    supports_status: bool = False

    # ---- Detection ---------------------------------------------------------------

    def matches(self, ssid: Optional[str], portal_url: Optional[str],
                page_html: Optional[str]) -> bool:
        """
        Does this provider match the current network/portal?
        Subclasses can look at the SSID, the portal domain or the page content.
        The base class matches nothing (only the fallback provider returns True).
        """
        return False

    # ---- Login -------------------------------------------------------------------

    def resolve_login_url(self, portal_url: Optional[str]) -> Optional[str]:
        """Determine the login URL to use."""
        return self.login_url or portal_url

    def login_url_candidates(self, portal_url: Optional[str]) -> List[str]:
        """Login page URLs to try, in order (default: just `resolve_login_url`)."""
        url = self.resolve_login_url(portal_url)
        return [url] if url else []

    def is_trusted_submit_url(self, url: str) -> bool:
        """May credentials be sent to this URL? Subclasses can restrict it."""
        return True

    def login(self, session, ctx: "portal.LoginContext",
              portal_url: Optional[str] = None,
              method: str = "password",
              http_timeout: float = 12.0) -> bool:
        """
        Generic login flow. Returns True on success.

        Steps:
          1) Download the login page and find the form (the candidate URLs
             are tried in order; meta/JavaScript redirects are followed).
          2) method="password": submit phone + password in one step.
             method="otp":      request an OTP with the phone, get the code,
                                submit it in a second step.
          3) Success check: probe again to see whether the internet is really
             reachable. (The page content is not inspected: words like
             "success/online" also appear on portal pages and would lead to a
             false "connected".)
        """
        self.last_failure = ""
        candidates = self.login_url_candidates(portal_url)
        if not candidates:
            logger.error("[%s] Could not determine the login URL.", self.key)
            self.last_failure = "Could not determine the login URL"
            return False

        untrusted_host = None
        for url in candidates:
            page = self._fetch_login_form(session, url, ctx, http_timeout)
            if page is None:
                continue
            form, base_url = page
            target = portal.form_action_url(form, base_url)
            if not self.is_trusted_submit_url(target):
                untrusted_host = self._warn_untrusted(target)
                continue

            if method == "otp":
                ok = self._login_with_otp(session, form, ctx, base_url, http_timeout)
            else:
                ok = self._login_with_password(session, form, ctx, base_url, http_timeout)
            if not ok:
                return False
            # The strongest proof: can we really reach the internet?
            return self._verify_online(session, http_timeout)

        if untrusted_host:
            self.last_failure = f"Portal host '{untrusted_host}' is not trusted (Open Logs)"
        return False

    def _fetch_login_form(self, session, url: str, ctx: "portal.LoginContext",
                          timeout: float, max_hops: int = 2) -> Optional[Tuple["portal.Form", str]]:
        """
        Download the login page and return (form, page URL), or None.
        Interstitial pages that only redirect via meta refresh/JavaScript are followed.
        """
        resp = None
        for _ in range(max_hops + 1):
            try:
                resp = session.get(url, timeout=timeout, allow_redirects=True)
            except Exception as exc:
                self._report_request_error("Could not download the login page", url, exc, ctx)
                return None
            base_url = resp.url or url
            form = portal.choose_login_form(portal.parse_forms(resp.text))
            if form is not None:
                return form, base_url
            nxt = network.find_redirect_url(resp.text, base_url)
            if not nxt or nxt == url:
                break
            logger.info("[%s] Following the portal page redirect to %s", self.key, portal.redact(nxt))
            url = nxt
        logger.error("[%s] No login form found on the page (HTTP %s).",
                     self.key, getattr(resp, "status_code", "?"))
        self.last_failure = "Login form not found"
        return None

    def _warn_untrusted(self, target: str) -> str:
        """Log that credentials were withheld from an untrusted host; return the host."""
        host = urlsplit(target).hostname or target
        logger.warning(
            "[%s] The login form submits to '%s', which is not a trusted host for this "
            "provider; credentials were NOT sent. If this really is the portal of your "
            "network, add \"%s\" to \"trusted_portal_hosts\" in config.json and restart the app.",
            self.key, host, host)
        return host

    # ---- Methods -----------------------------------------------------------------

    def _login_with_password(self, session, form, ctx, base_url, timeout) -> bool:
        if not ctx.phone or not ctx.password:
            logger.error("[%s] The password method needs a phone number and a password.", self.key)
            self.last_failure = "Phone number or password missing"
            return False
        values = {"phone": ctx.phone, "password": ctx.password}
        try:
            resp = portal.submit_form(session, form, values, base_url, timeout,
                                      intent="login", extra=ctx.extra_fields)
        except Exception as exc:
            self._report_request_error("Could not submit the login form", base_url, exc, ctx)
            return False
        logger.info("[%s] Password login form submitted (HTTP %s).",
                    self.key, getattr(resp, "status_code", "?"))
        return True

    def _login_with_otp(self, session, form, ctx, base_url, timeout) -> bool:
        if not ctx.phone:
            logger.error("[%s] The OTP method needs a phone number.", self.key)
            self.last_failure = "Phone number missing"
            return False
        if ctx.otp_provider is None:
            logger.error("[%s] No OTP provider function is set.", self.key)
            self.last_failure = "No OTP source configured"
            return False

        # The reference point must be taken BEFORE the SMS is triggered;
        # otherwise a fast SMS arriving before the step-1 response returns is
        # considered "old" and missed.
        if ctx.otp_prepare is not None:
            ctx.otp_prepare()

        # Step 1: submit the number (this usually triggers the SMS OTP).
        step1_values = {"phone": ctx.phone}
        try:
            resp = portal.submit_form(session, form, step1_values, base_url, timeout,
                                      intent="request_otp", extra=ctx.extra_fields)
        except Exception as exc:
            self._report_request_error("Could not request the OTP", base_url, exc, ctx)
            return False
        status = getattr(resp, "status_code", 200)
        if isinstance(status, int) and status >= 400:
            # The SMS was most likely not sent; don't wait ~90 s for nothing.
            logger.error("[%s] OTP request rejected (HTTP %s).", self.key, status)
            self.last_failure = f"OTP request rejected (HTTP {status})"
            return False

        base_url = resp.url or base_url

        # If the first form already has an OTP field, no need for a second page.
        forms2 = portal.parse_forms(resp.text)
        otp_form = next((f for f in forms2 if portal.form_needs_otp(f)), None)
        if otp_form is None:
            otp_form = portal.choose_login_form(forms2) or form

        # Step 2: get the OTP code (from Messages or from the user).
        logger.info("[%s] Waiting for the OTP...", self.key)
        code = ctx.otp_provider()
        if not code:
            logger.error("[%s] Could not get the OTP code.", self.key)
            self.last_failure = "Could not get the OTP code"
            return False

        step2_values = {"otp": code, "phone": ctx.phone}
        if ctx.password:
            step2_values["password"] = ctx.password
        target = portal.form_action_url(otp_form, base_url)
        if not self.is_trusted_submit_url(target):
            host = self._warn_untrusted(target)
            self.last_failure = f"Portal host '{host}' is not trusted (Open Logs)"
            return False
        try:
            portal.submit_form(session, otp_form, step2_values, base_url, timeout,
                               intent="login", extra=ctx.extra_fields)
        except Exception as exc:
            self._report_request_error("Could not submit the OTP", base_url, exc, ctx, code)
            return False
        logger.info("[%s] OTP login form submitted.", self.key)
        return True

    def session_status(self, session, http_timeout: float = 8.0) -> Optional[dict]:
        """
        While online, report the current session, or None if unknown.

        Returns a dict with at least:
          - "remaining_seconds": Optional[int]  (None means unlimited/unknown)
          - "remaining_text":    str            (e.g. "00:10:42" or "Unlimited")
        The base provider does not know how; portals that expose it override this.
        """
        return None

    def _verify_online(self, session, timeout: float, attempts: int = 3) -> bool:
        """Verify that the internet is really reachable after the login."""
        for i in range(attempts):
            result = network.probe_connectivity(session, timeout=min(timeout, 8.0))
            if result.state == network.ONLINE:
                logger.info("[%s] Login successful: internet access verified.", self.key)
                return True
            if i < attempts - 1:
                time.sleep(2)
        logger.warning("[%s] Could not verify internet access after the login.", self.key)
        self.last_failure = "Form submitted but the internet did not come up"
        return False

    # ---- Helpers -----------------------------------------------------------------

    def _report_request_error(self, what: str, url: str, exc: Exception,
                              ctx: "portal.LoginContext", *secrets: str) -> None:
        """
        Log a request error without leaking the password/OTP and set the user
        message. On a certificate error the fingerprint of the presented
        certificate is logged too, so the user can pin it if they want to
        (verification is NEVER turned off).
        """
        safe = portal.redact(exc, ctx.phone, ctx.password, *secrets)
        if isinstance(exc, requests.exceptions.SSLError):
            fp = network.cert_fingerprint(url)
            host = urlsplit(url).hostname or url
            logger.error("[%s] %s: the portal certificate could not be verified (%s): %s",
                         self.key, what, host, safe)
            if fp:
                logger.error(
                    "[%s] SHA-256 fingerprint of the '%s' certificate: %s. If you trust "
                    "this portal and have verified the fingerprint through another "
                    "channel, add {\"%s\": \"%s\"} to the \"portal_cert_pins\" field in "
                    "config.json and restart the app.",
                    self.key, host, fp, host, fp)
            self.last_failure = "Portal certificate could not be verified (Open Logs)"
            return
        logger.error("[%s] %s: %s", self.key, what, safe)
        self.last_failure = what
