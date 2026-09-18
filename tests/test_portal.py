"""portal.py — field classification, form data building and submission tests."""

import unittest

from aiswifi import portal
from aiswifi.portal import FormField, classify_field

ASPNET_PAGE = """
<html><body>
<form method="post" action="./login.aspx?sid=42" id="form1">
  <input type="hidden" name="__VIEWSTATE" value="dDwtMTA4NzY=" />
  <input type="hidden" name="__EVENTVALIDATION" value="/wEWAgK+" />
  <input type="text" name="txtMobile" placeholder="เบอร์โทรศัพท์" />
  <input type="password" name="txtPassword" />
  <input type="text" name="txtOTP" />
  <select name="ddlPhoneCode">
    <option value="+95">MM</option>
    <option value="+66" selected>TH</option>
  </select>
  <input type="checkbox" name="chkRemember" value="1" />
  <input type="checkbox" name="chkAccept" checked />
  <input type="checkbox" name="chkShowPassword" value="yes" />
  <input type="radio" name="lang" value="th" checked />
  <input type="radio" name="lang" value="en" />
  <input type="text" name="txtPromo" value="x" disabled />
  <input type="button" name="btnHelp" value="Help" />
  <input type="reset" name="btnReset" value="Clear" />
  <input type="submit" name="btnRequestOTP" value="Request OTP" />
  <input type="submit" name="btnLogin" value="Login" />
</form>
<form action="/search" method="get"><input type="text" name="q" /></form>
</body></html>
"""


class ClassifyFieldTests(unittest.TestCase):
    def test_types(self):
        self.assertEqual(classify_field(FormField("x", type="password")), "password")
        self.assertEqual(classify_field(FormField("x", type="tel")), "phone")
        self.assertEqual(classify_field(FormField("__VIEWSTATE", type="hidden")), "other")
        self.assertEqual(classify_field(FormField("btn", type="submit")), "submit")
        self.assertEqual(classify_field(FormField("btn", type="image")), "submit")

    def test_keywords(self):
        self.assertEqual(classify_field(FormField("msisdn")), "phone")
        self.assertEqual(classify_field(FormField("f1", placeholder="เบอร์โทรศัพท์")), "phone")
        self.assertEqual(classify_field(FormField("txtOTP")), "otp")
        self.assertEqual(classify_field(FormField("passcode")), "otp")   # OTP is checked first
        self.assertEqual(classify_field(FormField("pwd")), "password")
        self.assertEqual(classify_field(FormField("f", placeholder="รหัสผ่าน")), "password")
        self.assertEqual(classify_field(FormField("f", placeholder="รหัส OTP")), "otp")
        self.assertEqual(classify_field(FormField("comment")), "other")

    def test_choice_fields_never_get_credentials(self):
        # Even with "password"/"phone"/"code" in the name, choice fields get no values.
        self.assertEqual(classify_field(FormField("chkShowPassword", type="checkbox")), "other")
        self.assertEqual(classify_field(FormField("ddlPhoneCode", type="select")), "other")
        self.assertEqual(classify_field(FormField("rdoPhone", type="radio")), "other")


class BuildPayloadTests(unittest.TestCase):
    def setUp(self):
        forms = portal.parse_forms(ASPNET_PAGE)
        self.form = portal.choose_login_form(forms)

    def test_choose_login_form(self):
        self.assertIn("txtMobile", [f.name for f in self.form.fields])

    def test_login_payload_like_a_browser(self):
        p = portal.build_payload(self.form, {"phone": "0812345678", "password": "S3cret!"})
        # Hidden ASP.NET fields are kept EXACTLY.
        self.assertEqual(p["__VIEWSTATE"], "dDwtMTA4NzY=")
        self.assertEqual(p["__EVENTVALIDATION"], "/wEWAgK+")
        self.assertEqual(p["txtMobile"], "0812345678")
        self.assertEqual(p["txtPassword"], "S3cret!")
        self.assertEqual(p["txtOTP"], "")
        # Checkbox/radio: only checked ones; a checkbox without a value is "on".
        self.assertNotIn("chkRemember", p)
        self.assertNotIn("chkShowPassword", p)
        self.assertEqual(p["chkAccept"], "on")
        self.assertEqual(p["lang"], "th")
        # <select>: the selected option; the phone number is NOT written into it.
        self.assertEqual(p["ddlPhoneCode"], "+66")
        # Disabled, type=button and reset fields are not sent.
        for name in ("txtPromo", "btnHelp", "btnReset"):
            self.assertNotIn(name, p)
        # A single button: "Login" for the login intent.
        self.assertEqual(p["btnLogin"], "Login")
        self.assertNotIn("btnRequestOTP", p)

    def test_request_otp_intent_picks_request_button(self):
        p = portal.build_payload(self.form, {"phone": "0812345678"}, intent="request_otp")
        self.assertEqual(p["btnRequestOTP"], "Request OTP")
        self.assertNotIn("btnLogin", p)
        self.assertEqual(p["txtPassword"], "")

    def test_extra_fields_override_by_name(self):
        p = portal.build_payload(self.form, {"phone": "1"}, extra={"chkRemember": "1"})
        self.assertEqual(p["chkRemember"], "1")

    def test_button_element_and_image_input(self):
        # Choice by <button> label: the text gives the hint even if name/value don't.
        html = """<form><input name="user">
                  <button name="b1" value="1">Request OTP</button>
                  <button name="b2" value="2">Login</button></form>"""
        form = portal.parse_forms(html)[0]
        self.assertEqual(portal.build_payload(form, {"phone": "0800000000"})["b2"], "2")
        p_req = portal.build_payload(form, {"phone": "0800000000"}, intent="request_otp")
        self.assertEqual((p_req.get("b1"), p_req.get("b2")), ("1", None))
        img_only = portal.parse_forms('<form><input name="user"><input type="image" name="imgLogin"></form>')[0]
        p2 = portal.build_payload(img_only, {"phone": "0800000000"})
        self.assertEqual((p2["imgLogin.x"], p2["imgLogin.y"]), ("0", "0"))


class _FakeResp:
    def __init__(self, url):
        self.url = url
        self.status_code = 200
        self.text = ""


class _RecordingSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return _FakeResp(url)

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return _FakeResp(url)


class SubmitFormTests(unittest.TestCase):
    def test_post_resolves_relative_action_and_sends_referer(self):
        form = portal.choose_login_form(portal.parse_forms(ASPNET_PAGE))
        s = _RecordingSession()
        portal.submit_form(s, form, {"phone": "0812345678", "password": "pw12"},
                           "https://portal.example/wifi/start.aspx?x=1")
        method, url, kw = s.calls[0]
        self.assertEqual(method, "post")
        self.assertEqual(url, "https://portal.example/wifi/login.aspx?sid=42")
        self.assertEqual(kw["headers"]["Referer"], "https://portal.example/wifi/start.aspx?x=1")

    def test_get_form_replaces_action_query(self):
        form = portal.parse_forms('<form method="get" action="/auth?old=1#top"><input name="user"></form>')[0]
        s = _RecordingSession()
        portal.submit_form(s, form, {"phone": "0812345678"}, "http://10.0.0.1/")
        method, url, kw = s.calls[0]
        self.assertEqual((method, url), ("get", "http://10.0.0.1/auth"))
        self.assertEqual(kw["params"], {"user": "0812345678"})


class RedactTests(unittest.TestCase):
    def test_redacts_query_and_secrets(self):
        msg = ("HTTPConnectionPool(host='10.0.0.1', port=80): Max retries exceeded with "
               "url: /login?user=0812345678&pass=p%40ss%20word (Caused by X)")
        out = portal.redact(msg, "0812345678", "p@ss word")
        self.assertNotIn("0812345678", out)
        self.assertNotIn("p%40ss", out)
        self.assertIn("/login?<redacted>", out)
        self.assertEqual(portal.redact("password=Secret123 error", "Secret123"), "password=*** error")


if __name__ == "__main__":
    unittest.main()
