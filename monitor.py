"""
Arka plan izleyici (monitor).

Ayrı bir iş parçacığında (thread) sürekli olarak:
  - bağlantıyı yoklar,
  - captive portal tespit ederse ilgili sağlayıcı ile otomatik giriş yapar,
  - durumu, iş parçacığı-güvenli bir `State` nesnesinde tutar.

Kullanıcı arayüzü (menü çubuğu) bu `State`i okur; ağ işleri arayüzü
kilitlemez. Böylece ağ (thread) ile arayüz (ana döngü) net biçimde ayrılır.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from . import network, otp, portal
from . import providers as providers_mod
from . import config as config_mod

logger = logging.getLogger("aiswifi.monitor")

# Kullanıcıya gösterilecek durum kodları
ST_IDLE = "idle"                # otomatik giriş kapalı
ST_ONLINE = "online"           # internet var
ST_OFFLINE = "offline"         # ağ yok (Wi-Fi kapalı vb.)
ST_CAPTIVE = "captive"         # portal tespit edildi, giriş gerekli
ST_LOGGING_IN = "logging_in"   # giriş yapılıyor
ST_ERROR = "error"             # son deneme başarısız


@dataclass
class State:
    """Arayüzün okuyacağı, kilitle korunan paylaşımlı durum."""
    status: str = ST_IDLE
    ssid: Optional[str] = None
    provider: Optional[str] = None
    message: str = ""
    last_login_ts: float = 0.0
    last_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "ssid": self.ssid,
                "provider": self.provider,
                "message": self.message,
                "last_login_ts": self.last_login_ts,
                "last_error": self.last_error,
            }


class Monitor:
    def __init__(self, cfg: Dict[str, Any],
                 on_change: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.cfg = cfg
        self.state = State()
        self.on_change = on_change  # durum değişince arayüzü uyarmak için (opsiyonel)

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._login_now = threading.Event()  # anlık giriş isteği
        self._registry = providers_mod.build_registry(cfg.get("ais_login_url"))

        self.state.status = ST_ONLINE if cfg.get("auto_login") else ST_IDLE

    # ---- Yaşam döngüsü ----------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aiswifi-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._login_now.set()
        if self._thread:
            self._thread.join(timeout=3)

    def trigger_login(self) -> None:
        """Kullanıcı 'Şimdi Bağlan' dediğinde çağrılır."""
        self._login_now.set()

    def set_auto(self, enabled: bool) -> None:
        self.cfg["auto_login"] = enabled
        if not enabled:
            self._set_state(status=ST_IDLE, message="Otomatik giriş kapalı")
        else:
            self._set_state(status=ST_ONLINE, message="")
            self._login_now.set()

    # ---- Ana döngü --------------------------------------------------------

    def _run(self) -> None:
        session = network.new_session()
        backoff = 0.0
        while not self._stop.is_set():
            interval = float(self.cfg.get("poll_interval", 15))

            if not self.cfg.get("auto_login"):
                # Otomatik kapalı: yalnızca 'Şimdi Bağlan' isteğini bekle.
                if self._login_now.wait(timeout=1.0):
                    self._login_now.clear()
                    self._attempt_cycle(session, forced=True)
                continue

            # SSID'i güncelle (bilgi amaçlı; tespit için şart değil).
            ssid = network.get_ssid()

            result = network.probe_connectivity(session, timeout=8.0)
            if result.state == network.ONLINE:
                backoff = 0.0
                self._set_state(status=ST_ONLINE, ssid=ssid,
                                message="Bağlı", last_error="")
            elif result.state == network.OFFLINE:
                backoff = 0.0
                self._set_state(status=ST_OFFLINE, ssid=ssid,
                                message="Ağ yok (Wi-Fi kapalı olabilir)")
            else:  # CAPTIVE
                self._set_state(status=ST_CAPTIVE, ssid=ssid,
                                message="Bağlantı koptu, giriş yapılıyor…")
                ok = self._do_login(session, result, ssid)
                if ok:
                    backoff = 0.0
                else:
                    # Üssel geri çekilme (başarısız denemede bekleyerek yeniden dene)
                    base = float(self.cfg.get("backoff_base", 5))
                    cap = float(self.cfg.get("backoff_max", 120))
                    backoff = min(cap, base if backoff == 0 else backoff * 2)

            # Beklerken 'Şimdi Bağlan' gelirse hemen uyanıp dene.
            wait_for = max(interval, backoff)
            if self._login_now.wait(timeout=wait_for):
                self._login_now.clear()
                self._attempt_cycle(session, forced=True)

    def _attempt_cycle(self, session, forced: bool) -> None:
        """Tek seferlik yokla-ve-gerekiyorsa-giriş-yap döngüsü."""
        ssid = network.get_ssid()
        result = network.probe_connectivity(session, timeout=8.0)
        if result.state == network.ONLINE:
            self._set_state(status=ST_ONLINE, ssid=ssid, message="Zaten bağlı")
            return
        if result.state == network.OFFLINE and not forced:
            self._set_state(status=ST_OFFLINE, ssid=ssid, message="Ağ yok")
            return
        self._set_state(status=ST_LOGGING_IN, ssid=ssid, message="Giriş yapılıyor…")
        self._do_login(session, result, ssid)

    # ---- Giriş ------------------------------------------------------------

    def _do_login(self, session, probe_result, ssid: Optional[str]) -> bool:
        portal_url = probe_result.portal_url
        page_html = probe_result.body

        provider = providers_mod.detect_provider(
            self._registry, ssid, portal_url, page_html,
            preferred_key=self.cfg.get("preferred_provider"),
        )
        if provider is None:
            self._set_state(status=ST_ERROR,
                            message="Uygun sağlayıcı bulunamadı",
                            last_error="no_provider")
            return False

        phone, password = config_mod.get_credentials(provider.key)
        method = self.cfg.get("login_method", "password")

        if not phone:
            self._set_state(status=ST_ERROR,
                            provider=provider.name,
                            message="Kimlik bilgisi yok — Ayarlar'dan girin",
                            last_error="no_credentials")
            return False
        if method == "password" and not password:
            self._set_state(status=ST_ERROR,
                            provider=provider.name,
                            message="Şifre kayıtlı değil — Ayarlar'dan girin",
                            last_error="no_password")
            return False

        ctx = portal.LoginContext(
            phone=phone,
            password=password,
            otp_provider=self._make_otp_provider() if method == "otp" else None,
        )

        self._set_state(status=ST_LOGGING_IN, provider=provider.name,
                        message=f"{provider.name}: giriş yapılıyor…")

        retries = int(self.cfg.get("max_retries", 3))
        timeout = float(self.cfg.get("http_timeout", 12))
        for attempt in range(1, retries + 1):
            logger.info("Giriş denemesi %d/%d (%s, yöntem=%s)",
                        attempt, retries, provider.key, method)
            try:
                ok = provider.login(session, ctx, portal_url=portal_url,
                                    method=method, http_timeout=timeout)
            except Exception as exc:
                logger.exception("Giriş sırasında hata: %s", exc)
                ok = False
            if ok:
                self._set_state(status=ST_ONLINE, provider=provider.name,
                                message="Giriş başarılı 🎉",
                                last_login_ts=time.time(), last_error="")
                return True
            time.sleep(2)

        self._set_state(status=ST_ERROR, provider=provider.name,
                        message="Giriş başarısız (tekrar denenecek)",
                        last_error="login_failed")
        return False

    def _make_otp_provider(self) -> Callable[[], Optional[str]]:
        """
        Yapılandırmaya göre OTP kaynağını seçen bir fonksiyon üretir:
          - "messages": SMS'i Mesajlar veritabanından otomatik oku,
          - "ask":      arayüzden sor (app tarafından enjekte edilir),
          - "none":     OTP yok.
        """
        source = self.cfg.get("otp_source", "messages")
        timeout = int(self.cfg.get("otp_wait_timeout", 90))

        if source == "messages":
            def provider_fn() -> Optional[str]:
                baseline = otp.current_baseline()
                logger.info("SMS OTP bekleniyor (en fazla %ss)…", timeout)
                return otp.wait_for_new_otp(baseline, timeout=timeout)
            return provider_fn

        if source == "ask" and callable(getattr(self, "_ask_otp_cb", None)):
            return self._ask_otp_cb  # type: ignore[attr-defined]

        def none_fn() -> Optional[str]:
            return None
        return none_fn

    def set_ask_otp_callback(self, cb: Callable[[], Optional[str]]) -> None:
        """Arayüz, OTP'yi kullanıcıya sormak için bir geri-çağırım verebilir."""
        self._ask_otp_cb = cb  # type: ignore[attr-defined]

    # ---- Yardımcı ---------------------------------------------------------

    def _set_state(self, **kwargs: Any) -> None:
        self.state.update(**kwargs)
        if self.on_change:
            try:
                self.on_change(self.state.snapshot())
            except Exception:
                pass
