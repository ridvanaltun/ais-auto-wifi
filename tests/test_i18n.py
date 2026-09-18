"""i18n — translation lookup, fallback and catalog integrity (no rumps needed)."""

import unittest

from aiswifi import i18n


class I18nTests(unittest.TestCase):
    def test_english_default_and_thai(self):
        self.assertEqual(i18n.t("menu.connect_now"), "Connect Now")
        self.assertEqual(i18n.t("menu.connect_now", "en"), "Connect Now")
        self.assertEqual(i18n.t("menu.connect_now", "th"), "เชื่อมต่อเดี๋ยวนี้")

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(i18n.t("menu.quit", "de"), "Quit")
        self.assertEqual(i18n.normalize("de"), "en")
        self.assertEqual(i18n.normalize("th"), "th")

    def test_missing_key_returns_key(self):
        self.assertEqual(i18n.t("does.not.exist", "th"), "does.not.exist")

    def test_format_kwargs(self):
        self.assertEqual(i18n.t("menu.about", "en", version="1.0.0"), "About (v1.0.0)")
        self.assertIn("1.0.0", i18n.t("menu.about", "th", version="1.0.0"))
        # A template needing kwargs but given none returns the raw template, not a crash.
        self.assertIn("{version}", i18n.t("menu.about", "en"))

    def test_every_entry_has_english_and_thai(self):
        for key, entry in i18n.STRINGS.items():
            self.assertIn("en", entry, f"{key} missing English")
            self.assertTrue(entry["en"], f"{key} empty English")
            self.assertIn("th", entry, f"{key} missing Thai")
            self.assertTrue(entry["th"], f"{key} empty Thai")

    def test_placeholders_match_between_languages(self):
        import re
        ph = re.compile(r"\{(\w+)\}")
        for key, entry in i18n.STRINGS.items():
            en_ph = set(ph.findall(entry["en"]))
            th_ph = set(ph.findall(entry["th"]))
            self.assertEqual(en_ph, th_ph, f"{key}: placeholder mismatch en={en_ph} th={th_ph}")


if __name__ == "__main__":
    unittest.main()
