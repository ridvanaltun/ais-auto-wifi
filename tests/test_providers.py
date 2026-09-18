"""Providers — AIS detection/URL merging and the generic OTP/password flow tests."""

import unittest
from unittest import mock

import requests

from aiswifi import network, portal
from aiswifi import providers as providers_mod
from aiswifi.providers.ais import DEFAULT_AIS_LOGIN_URL, AISProvider
from aiswifi.providers.generic import GenericProvider


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
