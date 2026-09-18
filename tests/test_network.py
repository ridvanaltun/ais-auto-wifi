"""network.py — connectivity probe (mocked requests), SSID parsing, certificate pinning."""

import http.server
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import requests

from aiswifi import network
from aiswifi.network import CAPTIVE, OFFLINE, ONLINE

APPLE = network.APPLE_PROBE_URL
G1, G2 = network.FALLBACK_PROBES
SUCCESS = "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"


class _Resp:
    def __init__(self, status=200, text="", url="", headers=None):
        self.status_code, self.text, self.url = status, text, url
        self.headers = headers or {}


class _Session:
    """A fake requests session with a URL → response-or-exception map."""

    def __init__(self, routes):
        self.routes = routes
        self.closed = False

    def get(self, url, **kw):
        r = self.routes.get(url, requests.ConnectionError("unreachable"))
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed = True


class ProbeTests(unittest.TestCase):
    def probe(self, routes):
        return network.probe_connectivity(_Session(routes), timeout=1)

    def test_online(self):
        self.assertEqual(self.probe({APPLE: _Resp(200, SUCCESS, APPLE)}).state, ONLINE)

    def test_captive_redirect_keeps_portal_url_with_apple_in_query(self):
        portal_url = "https://wifi.portal.example/login?mac=aa&url=http://captive.apple.com/hotspot-detect.html"
        r = self.probe({APPLE: _Resp(200, "<form></form>", portal_url)})
        self.assertEqual((r.state, r.portal_url), (CAPTIVE, portal_url))
        self.assertEqual(r.body, "<form></form>")

    def test_captive_meta_refresh(self):
        html = '<meta content="0; URL=http://10.0.0.1/login.aspx?nasid=7" http-equiv="Refresh">'
        r = self.probe({APPLE: _Resp(200, html, APPLE)})
        self.assertEqual((r.state, r.portal_url), (CAPTIVE, "http://10.0.0.1/login.aspx?nasid=7"))
        # A relative target resolves to the probe host; that is never a portal URL.
        rel = '<meta http-equiv="refresh" content="0;url=/login.aspx">'
        self.assertIsNone(self.probe({APPLE: _Resp(200, rel, APPLE)}).portal_url)

    def test_captive_js_redirect_and_transparent(self):
        js = '<script>window.location.href = "https://portal.example/p?id=1";</script>'
        r = self.probe({APPLE: _Resp(200, js, APPLE)})
        self.assertEqual((r.state, r.portal_url), (CAPTIVE, "https://portal.example/p?id=1"))
        r2 = self.probe({APPLE: _Resp(200, "<form>login</form>", APPLE)})
        self.assertEqual((r2.state, r2.portal_url), (CAPTIVE, None))

    def test_offline(self):
        self.assertEqual(self.probe({}).state, OFFLINE)

    def test_fallback_204_online(self):
        self.assertEqual(self.probe({G1: _Resp(204, "", G1)}).state, ONLINE)

    def test_fallback_relative_location_is_absolute(self):
        r = self.probe({G1: _Resp(302, "", G1, {"Location": "/portal/login"})})
        self.assertEqual((r.state, r.portal_url), (CAPTIVE, "http://www.gstatic.com/portal/login"))

    def test_fallback_511_and_probe_host_not_portal(self):
        r = self.probe({G1: _Resp(511, "Network Authentication Required", G1)})
        self.assertEqual((r.state, r.portal_url), (CAPTIVE, None))

    def test_own_session_is_closed(self):
        fake = _Session({APPLE: _Resp(200, SUCCESS, APPLE)})
        with mock.patch.object(network, "new_session", return_value=fake):
            self.assertEqual(network.probe_connectivity(timeout=1).state, ONLINE)
        self.assertTrue(fake.closed)


class IpFamilyTests(unittest.TestCase):
    def tearDown(self):
        network.set_ip_family(True)  # restore the app default

    def test_force_ipv4_restricts_family(self):
        import socket
        import urllib3.util.connection as u3c
        network.set_ip_family(True)
        network.new_session().close()
        self.assertEqual(u3c.allowed_gai_family(), socket.AF_INET)

    def test_can_be_disabled(self):
        network.set_ip_family(False)
        network.new_session().close()
        import urllib3.util.connection as u3c
        self.assertIs(u3c.allowed_gai_family, network._ORIG_GAI_FAMILY)


class SsidTests(unittest.TestCase):
    def _ssid_with(self, networksetup_out):
        def fake_run(args, **kw):
            out = networksetup_out if "-getairportnetwork" in args else ""
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
        with mock.patch.dict(sys.modules, {"CoreWLAN": None}), \
                mock.patch.object(network.subprocess, "run", side_effect=fake_run):
            return network.get_ssid()

    def test_networksetup_parsing(self):
        self.assertEqual(self._ssid_with("Current Wi-Fi Network: .@ AIS SUPER WiFi\n"),
                         ".@ AIS SUPER WiFi")
        self.assertIsNone(self._ssid_with("** Error: Error obtaining wireless information."))
        self.assertIsNone(self._ssid_with("You are not associated with an AirPort network."))

    def test_no_tools_available(self):
        with mock.patch.dict(sys.modules, {"CoreWLAN": None}), \
                mock.patch.object(network.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(network.get_ssid())
            self.assertEqual(network.get_wifi_interface(), "en0")


@unittest.skipUnless(shutil.which("openssl"), "openssl not available")
class CertPinTests(unittest.TestCase):
    """Pinning against a local HTTPS "portal" with a self-signed certificate."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        key, crt = os.path.join(cls.dir, "k.pem"), os.path.join(cls.dir, "c.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=portal.test", "-keyout", key, "-out", crt],
                       check=True, capture_output=True)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"portal")

            def log_message(self, *a):
                pass

        cls.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        cls.httpd.socket = ctx.wrap_socket(cls.httpd.socket, server_side=True)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"https://127.0.0.1:{cls.httpd.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_default_verification_rejects_self_signed(self):
        with network.new_session() as s, self.assertRaises(requests.exceptions.SSLError):
            s.get(self.url, timeout=5)

    def test_matching_pin_allows_only_that_host(self):
        fp = network.cert_fingerprint(self.url)
        self.assertEqual(len(fp), 64)
        with network.new_session() as s:
            network.apply_cert_pins(s, {"127.0.0.1": fp})
            self.assertEqual(s.get(self.url, timeout=5).text, "portal")
            # The same server under another host name: no pinning applies → rejected.
            with self.assertRaises(requests.exceptions.SSLError):
                s.get(self.url.replace("127.0.0.1", "localhost"), timeout=5)

    def test_wrong_pin_is_rejected(self):
        with network.new_session() as s:
            network.apply_cert_pins(s, {"127.0.0.1": "00" * 32})
            with self.assertRaises(requests.exceptions.SSLError):
                s.get(self.url, timeout=5)

    def test_probe_hosts_and_invalid_entries_are_ignored(self):
        with network.new_session() as s:
            before = dict(s.adapters)
            network.apply_cert_pins(s, {"captive.apple.com": "ab" * 32, "x.test": "short"})
            self.assertEqual(dict(s.adapters), before)


if __name__ == "__main__":
    unittest.main()
