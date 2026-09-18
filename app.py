"""
Menü çubuğu (tray) arayüzü — rumps ile.

- Simge, bağlantı durumunu gösterir (bağlı / kopuk / giriş yapılıyor).
- Menüden: şimdi bağlan, otomatik giriş aç/kapa, kimlik bilgisi gir,
  yöntem seç, log dosyasını aç, çıkış.
- Ağ işleri arka planda (Monitor thread) döner; arayüz yalnızca durumu
  gösterir. Durum, ana döngüdeki bir Timer ile güvenli biçimde okunur.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Optional

import rumps

from . import __app_name__, __version__
from . import config as config_mod
from . import monitor as monitor_mod
from .monitor import (
    ST_CAPTIVE, ST_ERROR, ST_IDLE, ST_LOGGING_IN, ST_OFFLINE, ST_ONLINE,
)
from . import providers as providers_mod

logger = logging.getLogger("aiswifi.app")

# Duruma göre menü çubuğu başlığı (kısa simge). Metin yerine emoji kullanılır.
STATUS_ICON = {
    ST_ONLINE: "🛜",
    ST_OFFLINE: "📴",
    ST_CAPTIVE: "🔒",
    ST_LOGGING_IN: "🔄",
    ST_ERROR: "⚠️",
    ST_IDLE: "⏸️",
}

STATUS_TEXT = {
    ST_ONLINE: "Bağlı",
    ST_OFFLINE: "Ağ yok",
    ST_CAPTIVE: "Bağlantı koptu",
    ST_LOGGING_IN: "Giriş yapılıyor…",
    ST_ERROR: "Hata",
    ST_IDLE: "Otomatik kapalı",
}


class AISWifiApp(rumps.App):
    def __init__(self):
        super().__init__(name=__app_name__, title="🛜", quit_button=None)
        self.cfg = config_mod.load_config()

        # İzleyiciyi kur
        self.monitor = monitor_mod.Monitor(self.cfg)
        self.monitor.set_ask_otp_callback(self._ask_otp)

        # --- Menü ----------------------------------------------------------
        self.status_item = rumps.MenuItem("Durum: —")
        self.detail_item = rumps.MenuItem("")

        self.login_now_item = rumps.MenuItem("Şimdi Bağlan", callback=self._on_login_now)
        self.auto_item = rumps.MenuItem("Otomatik Bağlan", callback=self._on_toggle_auto)
        self.auto_item.state = 1 if self.cfg.get("auto_login") else 0

        self.creds_item = rumps.MenuItem("Kimlik Bilgilerini Gir…", callback=self._on_set_credentials)

        # Yöntem alt menüsü
        self.method_pw = rumps.MenuItem("Şifre (önerilen)", callback=self._on_method_password)
        self.method_otp = rumps.MenuItem("SMS OTP", callback=self._on_method_otp)
        self._sync_method_checks()

        self.log_item = rumps.MenuItem("Kayıtları Aç", callback=self._on_open_log)
        self.about_item = rumps.MenuItem(f"Hakkında (v{__version__})", callback=self._on_about)
        self.quit_item = rumps.MenuItem("Çıkış", callback=self._on_quit)

        self.menu = [
            self.status_item,
            self.detail_item,
            None,  # ayraç
            self.login_now_item,
            self.auto_item,
            None,
            self.creds_item,
            {"Giriş Yöntemi": [self.method_pw, self.method_otp]},
            None,
            self.log_item,
            self.about_item,
            None,
            self.quit_item,
        ]

        # Arka plan izleyiciyi başlat
        self.monitor.start()

        # Arayüzü periyodik olarak (ana döngüde) tazele — thread-güvenli.
        self._ui_timer = rumps.Timer(self._refresh_ui, 1.0)
        self._ui_timer.start()
        self._last_status: Optional[str] = None

    # ---- Arayüz tazeleme --------------------------------------------------

    def _refresh_ui(self, _timer) -> None:
        snap = self.monitor.state.snapshot()
        status = snap["status"]
        self.title = STATUS_ICON.get(status, "🛜")

        st_text = STATUS_TEXT.get(status, status)
        ssid = snap.get("ssid") or "—"
        self.status_item.title = f"Durum: {st_text}"

        detail = snap.get("message") or ""
        prov = snap.get("provider")
        parts = []
        if ssid and ssid != "—":
            parts.append(f"Ağ: {ssid}")
        if prov:
            parts.append(prov)
        if detail:
            parts.append(detail)
        self.detail_item.title = "  •  ".join(parts) if parts else "Hazır"

        # Durum değişiminde bildirim göster
        if status != self._last_status:
            self._notify_transition(self._last_status, status, detail)
            self._last_status = status

    def _notify_transition(self, old: Optional[str], new: str, detail: str) -> None:
        if not self.cfg.get("notifications", True):
            return
        if old is None:
            return  # ilk açılışta bildirim gönderme
        try:
            if new == ST_ONLINE and old in (ST_CAPTIVE, ST_LOGGING_IN, ST_ERROR):
                rumps.notification(__app_name__, "Bağlandı", "İnternet erişimi geri geldi.")
            elif new == ST_CAPTIVE:
                rumps.notification(__app_name__, "Bağlantı koptu", "Otomatik giriş deneniyor…")
            elif new == ST_ERROR:
                rumps.notification(__app_name__, "Giriş başarısız", detail or "Tekrar denenecek.")
        except Exception:
            pass  # bildirim izinleri yoksa sessiz geç

    # ---- Menü eylemleri ---------------------------------------------------

    def _on_login_now(self, _sender) -> None:
        self.monitor.trigger_login()
        self.detail_item.title = "Giriş isteği gönderildi…"

    def _on_toggle_auto(self, sender) -> None:
        new_val = not bool(sender.state)
        sender.state = 1 if new_val else 0
        self.cfg["auto_login"] = new_val
        config_mod.save_config(self.cfg)
        self.monitor.set_auto(new_val)

    def _on_method_password(self, _sender) -> None:
        self.cfg["login_method"] = "password"
        config_mod.save_config(self.cfg)
        self._sync_method_checks()

    def _on_method_otp(self, _sender) -> None:
        self.cfg["login_method"] = "otp"
        config_mod.save_config(self.cfg)
        self._sync_method_checks()

    def _sync_method_checks(self) -> None:
        is_pw = self.cfg.get("login_method", "password") == "password"
        self.method_pw.state = 1 if is_pw else 0
        self.method_otp.state = 0 if is_pw else 1

    def _on_set_credentials(self, _sender) -> None:
        # Hangi sağlayıcı için? Tercih edilen varsa onu, yoksa AIS'i kullan.
        registry = providers_mod.build_registry(self.cfg.get("ais_login_url"))
        pref = self.cfg.get("preferred_provider") or "ais"
        provider = providers_mod.get_provider_by_key(registry, pref) or registry[0]

        phone_win = rumps.Window(
            title=f"{provider.name} — Telefon Numarası",
            message="AIS'e kayıtlı telefon numaranızı girin (ör. 08xxxxxxxx):",
            default_text=(config_mod.get_credentials(provider.key)[0] or ""),
            ok="İleri", cancel="Vazgeç", dimensions=(300, 24),
        )
        resp = phone_win.run()
        if not resp.clicked:
            return
        phone = resp.text.strip()

        pass_win = rumps.Window(
            title=f"{provider.name} — Şifre",
            message="AIS SUPER WiFi şifrenizi girin.\n"
                    "(Sadece SMS OTP kullanacaksanız boş bırakabilirsiniz.)",
            default_text="",
            ok="Kaydet", cancel="Vazgeç", dimensions=(300, 24),
        )
        resp2 = pass_win.run()
        if not resp2.clicked:
            return
        password = resp2.text.strip()

        ok = config_mod.set_credentials(provider.key, phone, password)
        if ok:
            rumps.notification(__app_name__, "Kaydedildi",
                               f"{provider.name} kimlik bilgileri Anahtar Zinciri'ne kaydedildi.")
            self.monitor.trigger_login()
        else:
            rumps.alert("Hata",
                        "Kimlik bilgileri kaydedilemedi. 'keyring' kurulu mu?\n"
                        "Terminal: pip install keyring")

    def _ask_otp(self) -> Optional[str]:
        """OTP kaynağı 'ask' ise kullanıcıya soran pencere (ana döngüde çalışır)."""
        result: dict = {}
        done = threading.Event()

        def show():
            win = rumps.Window(
                title="SMS OTP",
                message="Telefonunuza gelen doğrulama kodunu girin:",
                ok="Gönder", cancel="Vazgeç", dimensions=(160, 24),
            )
            r = win.run()
            result["code"] = r.text.strip() if r.clicked else None
            done.set()

        # Pencere ana iş parçacığında açılmalı.
        try:
            from Foundation import NSThread  # type: ignore
            if NSThread.isMainThread():
                show()
            else:
                rumps.Timer(lambda _t: (show(), _t.stop()), 0.1).start()
                done.wait(timeout=self.cfg.get("otp_wait_timeout", 90))
        except Exception:
            show()
            done.wait(timeout=self.cfg.get("otp_wait_timeout", 90))
        return result.get("code")

    def _on_open_log(self, _sender) -> None:
        try:
            subprocess.run(["open", str(config_mod.LOG_PATH)], check=False)
        except Exception as exc:
            rumps.alert("Kayıtlar açılamadı", str(exc))

    def _on_about(self, _sender) -> None:
        rumps.alert(
            f"{__app_name__} v{__version__}",
            "AIS SUPER WiFi ve benzeri captive portal'lara otomatik giriş yapar.\n\n"
            "• Bağlantı koptuğunda otomatik yeniden giriş\n"
            "• Şifre veya SMS OTP yöntemi\n"
            "• Yeni sağlayıcılar için genişletilebilir\n\n"
            "Kimlik bilgileriniz macOS Anahtar Zinciri'nde saklanır.",
        )

    def _on_quit(self, _sender) -> None:
        try:
            self.monitor.stop()
        finally:
            rumps.quit_application()


def run() -> None:
    config_mod.setup_logging(verbose=False)
    logger.info("%s v%s başlatılıyor", __app_name__, __version__)
    AISWifiApp().run()
