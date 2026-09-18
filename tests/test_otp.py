"""otp.py — code extraction, attributedBody decoding and chat.db (WAL) reading tests."""

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aiswifi import otp
from aiswifi.otp import _extract_code

APPLE_EPOCH_OFFSET = 978307200


class ExtractCodeTests(unittest.TestCase):
    def test_four_and_six_digit_codes(self):
        self.assertEqual(_extract_code("Your OTP is 4821"), "4821")
        self.assertEqual(_extract_code("รหัส OTP ของคุณคือ 482193 (Ref: ABCD)"), "482193")
        # Turkish hint words are supported too.
        self.assertEqual(_extract_code("AIS WiFi doğrulama kodu:7351"), "7351")

    def test_long_numbers_are_ignored(self):
        # A 10-digit phone number is not a candidate; neither is a hyphenated phone fragment.
        self.assertEqual(_extract_code("Call 0812345678, code 5531"), "5531")
        self.assertEqual(_extract_code("Support: 081-234-5678. OTP 7412"), "7412")
        self.assertIsNone(_extract_code("Payment received with card 4111111111111111"))

    def test_dates_are_not_codes(self):
        self.assertEqual(_extract_code("Your OTP for 2026-09-18 is 482193"), "482193")
        self.assertEqual(_extract_code("Password dated 18.09.2026: 9031"), "9031")

    def test_hint_word_anchoring(self):
        # A code next to a hint word wins over the first number in the text.
        self.assertEqual(_extract_code("Ref 9999. Your code: 123456"), "123456")

    def test_hint_requirement(self):
        text = "See you tomorrow at 1930, room 12345"
        self.assertEqual(_extract_code(text), "12345")             # no hint → longest
        self.assertIsNone(_extract_code(text, require_hint=True))  # rejected while waiting

    def test_length_hint_and_empty(self):
        self.assertEqual(_extract_code("codes 1234 and 567890", length_hint=6), "567890")
        self.assertIsNone(_extract_code("hi, how are you"))
        self.assertIsNone(_extract_code(""))


def _typedstream(msg: str) -> bytes:
    """A synthetic attributedBody resembling an NSArchiver typedstream."""
    data = msg.encode("utf-8")
    if len(data) < 0x80:
        length = bytes([len(data)])
    else:
        length = b"\x81" + len(data).to_bytes(2, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
            b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
            + length + data + b"\x86\x84\x02iI\x01\x05\x92\x84\x84\x84\x0cNSDictionary\x00")


class AttributedBodyTests(unittest.TestCase):
    def test_short_and_long_messages(self):
        short = "AIS WiFi OTP: 482193"
        self.assertEqual(otp._decode_attributed_body(_typedstream(short)), short)
        long_msg = "รหัสผ่าน AIS SUPER WiFi ของท่านคือ 836271 " * 4
        self.assertEqual(otp._decode_attributed_body(_typedstream(long_msg)), long_msg)

    def test_unknown_format_falls_back(self):
        self.assertIn("code 1234", otp._decode_attributed_body(b"\x00\x01code 1234\x02"))
        self.assertEqual(otp._decode_attributed_body(b""), "")


class ChatDbTests(unittest.TestCase):
    """A chat.db in WAL mode that has not been checkpointed, like the real Messages."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Path(self.dir) / "chat.db"
        self.w = sqlite3.connect(str(self.db), check_same_thread=False)
        self.w.execute("PRAGMA journal_mode=WAL")
        self.w.execute("PRAGMA wal_autocheckpoint=0")
        self.w.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, "
                       "text TEXT, attributedBody BLOB, is_from_me INTEGER)")
        self.w.commit()
        self.add("Old OTP: 111111")
        self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        patcher = mock.patch.object(otp, "CHAT_DB", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.w.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, text=None, body=None, from_me=0):
        ns = int((time.time() - APPLE_EPOCH_OFFSET) * 1e9)
        self.w.execute("INSERT INTO message (date, text, attributedBody, is_from_me) VALUES (?,?,?,?)",
                       (ns, text, body, from_me))
        self.w.commit()  # written to -wal only (no checkpoint)

    def test_new_sms_in_wal_is_visible(self):
        baseline = otp.current_baseline()
        self.assertEqual(baseline, 1)
        self.add("AIS WiFi OTP: 482193")
        self.assertEqual(otp.wait_for_new_otp(baseline, timeout=2, poll=0.1), "482193")

    def test_old_codes_and_own_messages_are_ignored(self):
        baseline = otp.current_baseline()
        self.add("OTP 999999", from_me=1)
        self.assertIsNone(otp.wait_for_new_otp(baseline, timeout=0.3, poll=0.1))

    def test_newest_of_multiple_sms_and_unrelated_sms(self):
        baseline = otp.current_baseline()
        self.add("AIS WiFi OTP: 222222")
        self.add("Let's meet at 1930 tonight")       # no hint → ignored
        self.add(body=_typedstream("AIS WiFi OTP: 333333"))
        self.assertEqual(otp.wait_for_new_otp(baseline, timeout=1, poll=0.1), "333333")

    def test_sms_arriving_during_wait(self):
        baseline = otp.current_baseline()
        threading.Timer(0.3, self.add, args=("Your AIS code is 5123",)).start()
        self.assertEqual(otp.wait_for_new_otp(baseline, timeout=3, poll=0.1), "5123")

    def test_stop_event_ends_wait(self):
        ev = threading.Event()
        ev.set()
        t0 = time.monotonic()
        self.assertIsNone(otp.wait_for_new_otp(otp.current_baseline(), timeout=30,
                                               poll=5, stop_event=ev))
        self.assertLess(time.monotonic() - t0, 2)

    def test_copy_fallback_cleans_temp_dir(self):
        real_connect, real_mkdtemp = sqlite3.connect, tempfile.mkdtemp
        created = []

        def failing_direct(target, *a, **kw):
            if kw.get("uri"):
                raise sqlite3.OperationalError("authorization denied")
            return real_connect(target, *a, **kw)

        def tracking_mkdtemp(*a, **kw):
            d = real_mkdtemp(*a, **kw)
            created.append(d)
            return d

        self.add("AIS WiFi OTP: 777777")
        with mock.patch.object(otp.sqlite3, "connect", side_effect=failing_direct), \
                mock.patch.object(otp.tempfile, "mkdtemp", side_effect=tracking_mkdtemp):
            self.assertEqual(otp.current_baseline(), 2)    # the copy includes WAL content too
            with mock.patch.object(otp.shutil, "copy2", side_effect=PermissionError("TCC")):
                self.assertIsNone(otp.current_baseline())  # no permission → None (not 0)
        self.assertEqual(len(created), 2)
        for d in created:
            self.assertFalse(os.path.exists(d), "temporary copy directory was not deleted")

    def test_missing_db(self):
        with mock.patch.object(otp, "CHAT_DB", Path(self.dir) / "missing.db"):
            self.assertIsNone(otp.current_baseline())
            self.assertFalse(otp.can_read_messages())
            self.assertIsNone(otp.read_latest_otp())


if __name__ == "__main__":
    unittest.main()
