"""Providers — AIS detection/URL merging and the generic OTP/password flow tests."""

import unittest
from unittest import mock

import requests

from aiswifi import network, portal
from aiswifi import providers as providers_mod
from aiswifi.providers.ais import (
    DEFAULT_AIS_LOGIN_URL, AISProvider, _hms_to_seconds, parse_status,
)
from aiswifi.providers.generic import GenericProvider


class SessionStatusTests(unittest.TestCase):
    # The real endpoint returns a JSON string whose values are one-element lists.
    RAW = ('{"logonStatus":["true"],"remainingTime":["00:10:42"],'
           '"sessionTime":["00:19:18"],"unlimitedAccount":["false"],"responseCode":["0000"]}')

    def test_hms_to_seconds(self):
        self.assertEqual(_hms_to_seconds("00:10:42"), 642)
        self.assertEqual(_hms_to_seconds("1:05:09"), 3909)
        self.assertIsNone(_hms_to_seconds("nope"))
        self.assertIsNone(_hms_to_seconds(None))

    def test_parse_nested_json_string_and_lists(self):
        info = parse_status(self.RAW)
        self.assertEqual((info["online"], info["remaining_seconds"], info["remaining_text"]),
                         (True, 642, "00:10:42"))
        self.assertEqual(info["session_text"], "00:19:18")

    def test_parse_unlimited_and_logged_out(self):
        unlimited = parse_status({"logonStatus": ["true"], "unlimitedAccount": ["true"],
                                  "remainingTime": ["00:00:00"]})
        self.assertTrue(unlimited["unlimited"])
        self.assertIsNone(unlimited["remaining_seconds"])
        self.assertEqual(unlimited["remaining_text"], "Unlimited")
        self.assertFalse(parse_status({"logonStatus": ["false"]})["online"])

    def test_session_status_posts_to_status_url(self):
        class S:
            def post(self, url, **kw):
                self.url, self.kw = url, kw
                return type("R", (), {"status_code": 200, "json": lambda self=None: SessionStatusTests.RAW})()
        prov, sess = AISProvider(), S()
        info = prov.session_status(sess)
        self.assertEqual(sess.url, "https://wifi.ais.co.th/checkStatusLogon")
        self.assertEqual(info["remaining_seconds"], 642)

    def test_session_status_handles_errors(self):
        class Boom:
            def post(self, url, **kw):
                raise requests.ConnectionError("not on AIS")
        self.assertIsNone(AISProvider().session_status(Boom()))

        class NotOk:
            def post(self, url, **kw):
                return type("R", (), {"status_code": 404, "text": ""})()
        self.assertIsNone(AISProvider().session_status(NotOk()))

    def test_generic_provider_has_no_status(self):
        self.assertFalse(GenericProvider().supports_status)
        self.assertIsNone(GenericProvider().session_status(object()))


class AISProviderTests(unittest.TestCase):
    def test_resolve_login_url_keeps_session_params(self):
        p = AISProvider()
        self.assertEqual(p.resolve_login_url(None), DEFAULT_AIS_LOGIN_URL)
        portal_url = "http://10.0.0.1/login?mac=aa%3Abb&ip=10.0.0.5&sig=AbC%2F%3D"
        self.assertEqual(p.resolve_login_url(portal_url),
                         DEFAULT_AIS_LOGIN_URL + "?mac=aa%3Abb&ip=10.0.0.5&sig=AbC%2F%3D")

    def test_resolve_login_url_never_downgrades_or_changes_host(self):
        p = AISProvider("https://ext-activities.ais.co.th/apps/wifigen/login.aspx?lang=th")
        out = p.resolve_login_url("http://evil.example/x?lang=en&nasid=7")
        self.assertEqual(out, "https://ext-activities.ais.co.th/apps/wifigen/login.aspx?lang=th&nasid=7")

    def test_matches(self):
        p = AISProvider()
        for ssid in (".@ AIS SUPER WiFi", "AIS_WiFi", "aiswifi"):
            self.assertTrue(p.matches(ssid, None, None), ssid)
        for ssid in ("Thais Cafe", "Raisin Hotel", "Kaiser"):
            self.assertFalse(p.matches(ssid, None, None), ssid)
        self.assertTrue(p.matches(None, "https://wifi.ais.co.th/portal", None))
        self.assertFalse(p.matches(None, "http://evil.example/?r=ais.co.th", None))

    def test_generic_is_last_resort(self):
        reg = providers_mod.build_registry()
        self.assertIsInstance(reg[-1], GenericProvider)
        found = providers_mod.detect_provider(reg, "Hotel", "http://10.0.0.1/", "<form/>")
        self.assertEqual(found.key, "generic")


PAGE = """<form method="post" action="login.aspx">
  <input type="hidden" name="__VIEWSTATE" value="vs1">
  <input name="txtMobile" placeholder="phone"><input name="txtOTP">
  <input type="submit" name="btnRequest" value="Request OTP">
  <input type="submit" name="btnVerify" value="Verify">
</form>"""


class _Resp:
    def __init__(self, url, text=PAGE, status=200):
        self.url, self.text, self.status_code = url, text, status


class _FlowSession:
    def __init__(self, events, post_error=None):
        self.events, self.post_error = events, post_error

    def get(self, url, **kw):
        self.events.append("get")
        return _Resp(url)

    def post(self, url, data=None, **kw):
        if self.post_error:
            raise self.post_error
        self.events.append(("post", data))
        return _Resp(url)


LOGIN_PAGE = """<form method="post" action="%s">
  <input name="txtMobile" placeholder="phone"><input type="password" name="txtPassword">
  <input type="submit" name="btnLogin" value="Login">
</form>"""

DNS_ERROR = requests.ConnectionError("Failed to resolve 'ext-activities.ais.co.th'")


class _RouteSession:
    """URL → page HTML (or exception); records POSTs."""

    def __init__(self, pages):
        self.pages, self.posts = pages, []

    def get(self, url, **kw):
        page = self.pages.get(url, DNS_ERROR)
        if isinstance(page, Exception):
            raise page
        return _Resp(url, page)

    def post(self, url, data=None, **kw):
        self.posts.append((url, data))
        return _Resp(url, "<html></html>")


class PortalCandidateAndTrustTests(unittest.TestCase):
    """The fixed AIS URL is unreachable before login (walled garden)."""

    def setUp(self):
        p = mock.patch.object(network, "probe_connectivity",
                              return_value=network.ProbeResult(network.ONLINE))
        p.start()
        self.addCleanup(p.stop)
        self.ctx = portal.LoginContext(phone="0812345678", password="S3cretPw")

    def login(self, provider, portal_url, pages):
        s = _RouteSession(pages)
        ok = provider.login(s, self.ctx, portal_url=portal_url, method="password")
        return ok, s.posts

    def test_candidates_portal_first_then_fixed_url(self):
        p = AISProvider()
        self.assertEqual(p.login_url_candidates("https://wifi.ais.co.th/login?x=1"),
                         ["https://wifi.ais.co.th/login?x=1", DEFAULT_AIS_LOGIN_URL + "?x=1"])
        self.assertEqual(p.login_url_candidates(None), [DEFAULT_AIS_LOGIN_URL])

    def test_logs_in_via_the_portal_when_fixed_url_is_unreachable(self):
        portal_url = "https://wifi.ais.co.th/login?nasid=7"
        ok, posts = self.login(AISProvider(), portal_url, {portal_url: LOGIN_PAGE % "auth"})
        self.assertTrue(ok)
        self.assertEqual(posts[0][0], "https://wifi.ais.co.th/auth")

    def test_credentials_never_sent_to_untrusted_host(self):
        prov = AISProvider()
        ok, posts = self.login(prov, "http://10.0.0.1/login", {"http://10.0.0.1/login": LOGIN_PAGE % ""})
        self.assertFalse(ok)
        self.assertEqual(posts, [])
        self.assertIn("10.0.0.1", prov.last_failure)

    def test_trusted_portal_hosts_and_forms_posting_to_ais(self):
        url = "http://10.0.0.1/login"
        ok, posts = self.login(AISProvider(trusted_hosts=["10.0.0.1"]), url, {url: LOGIN_PAGE % ""})
        self.assertTrue(ok)
        self.assertEqual(posts[0][0], url)
        # A gateway page whose form posts to an AIS domain is fine without configuration.
        ok, posts = self.login(AISProvider(), url, {url: LOGIN_PAGE % "https://wifi.ais.co.th/auth"})
        self.assertTrue(ok)
        self.assertEqual(posts[0][0], "https://wifi.ais.co.th/auth")

    def test_meta_refresh_interstitial_is_followed(self):
        hop = "http://10.0.0.1/"
        pages = {hop: '<meta http-equiv="refresh" content="0;url=https://wifi.ais.co.th/login">',
                 "https://wifi.ais.co.th/login": LOGIN_PAGE % "auth"}
        ok, posts = self.login(AISProvider(), hop, pages)
        self.assertTrue(ok)
        self.assertEqual(posts[0][0], "https://wifi.ais.co.th/auth")


class OtpFlowTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(network, "probe_connectivity",
                              return_value=network.ProbeResult(network.ONLINE))
        p.start()
        self.addCleanup(p.stop)

    def test_baseline_is_taken_before_sms_is_triggered(self):
        events = []
        ctx = portal.LoginContext(
            phone="0812345678",
            otp_prepare=lambda: events.append("prepare"),
            otp_provider=lambda: events.append("otp") or "482193",
        )
        ok = GenericProvider().login(_FlowSession(events), ctx,
                                     portal_url="http://10.0.0.1/", method="otp")
        self.assertTrue(ok)
        self.assertEqual([e if isinstance(e, str) else e[0] for e in events],
                         ["get", "prepare", "post", "otp", "post"])
        step1, step2 = events[2][1], events[4][1]
        self.assertIn("btnRequest", step1)
        self.assertNotIn("btnVerify", step1)
        self.assertEqual(step1["__VIEWSTATE"], "vs1")
        self.assertEqual(step2["txtOTP"], "482193")
        self.assertIn("btnVerify", step2)
        self.assertNotIn("btnRequest", step2)

    def test_errors_do_not_leak_secrets_and_set_reason(self):
        err = requests.ConnectionError("Max retries exceeded with url: /login?pass=S3cretPw")
        ctx = portal.LoginContext(phone="0812345678", password="S3cretPw")
        prov = GenericProvider()
        with self.assertLogs("aiswifi", level="ERROR") as logs:
            self.assertFalse(prov.login(_FlowSession([], post_error=err), ctx,
                                        portal_url="http://10.0.0.1/", method="password"))
        self.assertNotIn("S3cretPw", "\n".join(logs.output))
        self.assertEqual(prov.last_failure, "Could not submit the login form")

    def test_ssl_error_reports_fingerprint_without_disabling_verification(self):
        class S:
            def get(self, url, **kw):
                self.kw = kw
                raise requests.exceptions.SSLError("certificate verify failed")
        prov, sess = GenericProvider(), S()
        with mock.patch.object(network, "cert_fingerprint", return_value="ab" * 32), \
                self.assertLogs("aiswifi", level="ERROR") as logs:
            self.assertFalse(prov.login(sess, portal.LoginContext(phone="1", password="2"),
                                        portal_url="https://portal.example/"))
        self.assertNotIn("verify", sess.kw)
        self.assertIn("ab" * 32, "\n".join(logs.output))
        self.assertIn("certificate", prov.last_failure)


if __name__ == "__main__":
    unittest.main()
