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
from typing import Optional
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

    def login(self, session, ctx: "portal.LoginContext",
              portal_url: Optional[str] = None,
              method: str = "password",
              http_timeout: float = 12.0) -> bool:
        """
        Generic login flow. Returns True on success.

        Steps:
          1) Download the login page and find the form.
          2) method="password": submit phone + password in one step.
             method="otp":      request an OTP with the phone, get the code,
                                submit it in a second step.
          3) Success check: probe again to see whether the internet is really
             reachable. (The page content is not inspected: words like
             "success/online" also appear on portal pages and would lead to a
             false "connected".)
        """
        self.last_failure = ""
        url = self.resolve_login_url(portal_url)
        if not url:
            logger.error("[%s] Could not determine the login URL.", self.key)
            self.last_failure = "Could not determine the login URL"
            return False

        try:
            resp = session.get(url, timeout=http_timeout, allow_redirects=True)
        except Exception as exc:
            self._report_request_error("Could not download the login page", url, exc, ctx)
            return False

        base_url = resp.url or url
        forms = portal.parse_forms(resp.text)
        form = portal.choose_login_form(forms)
        if form is None:
            logger.error("[%s] No login form found on the page (HTTP %s).",
                         self.key, getattr(resp, "status_code", "?"))
            self.last_failure = "Login form not found"
            return False

        if method == "otp":
            ok = self._login_with_otp(session, form, ctx, base_url, http_timeout)
        else:
            ok = self._login_with_password(session, form, ctx, base_url, http_timeout)

        if not ok:
            return False

        # The strongest proof: can we really reach the internet?
        return self._verify_online(session, http_timeout)

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
        try:
            portal.submit_form(session, otp_form, step2_values, base_url, timeout,
                               intent="login", extra=ctx.extra_fields)
        except Exception as exc:
            self._report_request_error("Could not submit the OTP", base_url, exc, ctx, code)
            return False
        logger.info("[%s] OTP login form submitted.", self.key)
        return True

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
