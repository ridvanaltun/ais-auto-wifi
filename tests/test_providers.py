"""Providers — AIS detection/URL merging and the generic OTP/password flow tests."""

import unittest
from unittest import mock

import requests

from aiswifi import network, portal
from aiswifi import providers as providers_mod
from aiswifi.providers.ais import AISProvider, _hms_to_seconds, parse_status
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


LOGON_OK = '{"logonStatus":["true"],"replyMessage":["\\"SBR-0000\\";\\"OK\\""],"responseCode":["0000"]}'
LOGON_BAD = '{"logonStatus":["false"],"replyMessage":["\\"SBR-0408\\";\\"Invalid User/Password\\""],"responseCode":["0000"]}'
REGISTERED = '{"responseCode":["0000"],"responseMessage":["REGISTERED_SUCCESS"],"username":["0812345678@aisads"]}'


class _ApiSession:
    """Fake AIS session recording GET/POST; POSTs answered from `replies`."""

    def __init__(self, replies):
        self.replies = replies  # path -> body text
        self.posts = []
        self.gets = []

    def get(self, url, **kw):
        self.gets.append(url)
        return _Resp(url, "")

    def post(self, url, data=None, **kw):
        self.posts.append((url, data))
        path = url.rsplit("/", 1)[-1]
        return _Resp(url, self.replies.get(path, LOGON_BAD))


class AisJsonLoginTests(unittest.TestCase):
    """AIS authenticates through its JSON API, not an HTML form."""

    def setUp(self):
        p = mock.patch.object(network, "probe_connectivity",
                              return_value=network.ProbeResult(network.ONLINE))
        p.start()
        self.addCleanup(p.stop)

    def test_api_base_prefers_portal_origin_only_when_ais(self):
        p = AISProvider()
        self.assertEqual(p._api_base("https://wifi.ais.co.th/?sid=1"), "https://wifi.ais.co.th")
        # Non-AIS host is ignored → default AIS origin (credentials stay on AIS).
        self.assertEqual(p._api_base("http://10.0.0.1/login"), "https://wifi.ais.co.th")
        self.assertEqual(p._api_base(None), "https://wifi.ais.co.th")
        # A host the user explicitly trusts is honoured.
        self.assertEqual(AISProvider(trusted_hosts=["10.0.0.1"])._api_base("http://10.0.0.1/x"),
                         "http://10.0.0.1")

    def test_password_login_posts_credentials_to_login_endpoint(self):
        s = _ApiSession({"login": LOGON_OK})
        prov = AISProvider()
        ok = prov.login(s, portal.LoginContext(phone="0812345678", password="S3cretPw"),
                        portal_url="https://wifi.ais.co.th", method="password")
        self.assertTrue(ok)
        url, data = s.posts[0]
        self.assertEqual(url, "https://wifi.ais.co.th/login")
        self.assertEqual((data["txtUsername"], data["txtPassword"]), ("0812345678", "S3cretPw"))

    def test_wrong_password_reports_portal_message(self):
        s = _ApiSession({"login": LOGON_BAD})
        prov = AISProvider()
        ok = prov.login(s, portal.LoginContext(phone="0812345678", password="nope"),
                        portal_url="https://wifi.ais.co.th", method="password")
        self.assertFalse(ok)
        self.assertEqual(prov.last_failure, "Invalid User/Password")

    def test_credentials_only_go_to_ais_even_with_untrusted_portal(self):
        s = _ApiSession({"login": LOGON_OK})
        AISProvider().login(s, portal.LoginContext(phone="0812345678", password="pw"),
                            portal_url="http://evil.example/login", method="password")
        self.assertTrue(all(u.startswith("https://wifi.ais.co.th/") for u, _ in s.posts))

    def test_otp_registers_then_logs_in_with_the_code(self):
        s = _ApiSession({"register": REGISTERED, "login": LOGON_OK})
        events = []
        ctx = portal.LoginContext(
            phone="0812345678",
            otp_prepare=lambda: events.append("prepare"),
            otp_provider=lambda: events.append("otp") or "482193",
        )
        ok = AISProvider().login(s, ctx, portal_url="https://wifi.ais.co.th", method="otp")
        self.assertTrue(ok)
        # Baseline taken before the SMS request, then the code used as password.
        self.assertEqual(events, ["prepare", "otp"])
        paths = [u.rsplit("/", 1)[-1] for u, _ in s.posts]
        self.assertEqual(paths, ["register", "login"])
        self.assertEqual(s.posts[1][1]["txtPassword"], "482193")
        # The login uses the username returned by register, not the raw phone.
        self.assertEqual(s.posts[1][1]["txtUsername"], "0812345678@aisads")

    def test_otp_without_code_fails_without_logging_in(self):
        s = _ApiSession({"register": REGISTERED})
        ctx = portal.LoginContext(phone="0812345678", otp_provider=lambda: None)
        ok = AISProvider().login(s, ctx, portal_url="https://wifi.ais.co.th", method="otp")
        self.assertFalse(ok)
        self.assertEqual([u.rsplit("/", 1)[-1] for u, _ in s.posts], ["register"])


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
