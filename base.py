"""
Sağlayıcı (provider) temel sınıfı.

Her Wi-Fi sağlayıcısı bu sınıftan türetilir. Yeni bir ücretsiz Wi-Fi noktası
eklemek için tek yapmanız gereken:
  - `key` ve `name` belirlemek,
  - `matches(...)` içinde bu sağlayıcıya ait olup olmadığımızı döndürmek,
  - gerekiyorsa `login_url`/başarı kontrolünü özelleştirmek.

Varsayılan `login()` akışı, `portal.py` içindeki genel form motorunu kullanır
ve çoğu captive portal için yeniden yazmaya gerek kalmadan çalışır.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .. import network, portal

logger = logging.getLogger("aiswifi.provider")


class BaseProvider:
    key: str = "base"
    name: str = "Genel Sağlayıcı"

    # Alt sınıflar, giriş sayfası adresini biliyorsa buraya yazabilir.
    login_url: Optional[str] = None

    # ---- Tespit -----------------------------------------------------------

    def matches(self, ssid: Optional[str], portal_url: Optional[str],
                page_html: Optional[str]) -> bool:
        """
        Bu sağlayıcı, mevcut ağ/portal ile eşleşiyor mu?
        Alt sınıflar SSID adı, portal alan adı veya sayfa içeriğine bakabilir.
        Temel sınıf hiçbir şeyle eşleşmez (yalnızca fallback sağlayıcı için True döner).
        """
        return False

    # ---- Giriş ------------------------------------------------------------

    def resolve_login_url(self, portal_url: Optional[str]) -> Optional[str]:
        """Kullanılacak giriş adresini belirle."""
        return self.login_url or portal_url

    def login(self, session, ctx: "portal.LoginContext",
              portal_url: Optional[str] = None,
              method: str = "password",
              http_timeout: float = 12.0) -> bool:
        """
        Genel giriş akışı. Başarılıysa True döner.

        Adımlar:
          1) Giriş sayfasını indir, formu bul.
          2) method="password": telefon + şifre ile tek adımda gönder.
             method="otp":      telefon ile OTP iste, kodu al, ikinci adımda gönder.
          3) Başarı kontrolü: internet tekrar açıldı mı diye yeniden yokla
             (en güvenilir kanıt), olmadı ise sayfa içeriğine bak.
        """
        url = self.resolve_login_url(portal_url)
        if not url:
            logger.error("[%s] Giriş adresi belirlenemedi.", self.key)
            return False

        try:
            resp = session.get(url, timeout=http_timeout, allow_redirects=True)
        except Exception as exc:
            logger.error("[%s] Giriş sayfası indirilemedi: %s", self.key, exc)
            return False

        base_url = resp.url or url
        forms = portal.parse_forms(resp.text)
        form = portal.choose_login_form(forms)
        if form is None:
            logger.error("[%s] Sayfada giriş formu bulunamadı.", self.key)
            return False

        if method == "otp":
            ok = self._login_with_otp(session, form, ctx, base_url, http_timeout)
        else:
            ok = self._login_with_password(session, form, ctx, base_url, http_timeout)

        if not ok:
            return False

        # En sağlam kanıt: gerçekten internete çıkabiliyor muyuz?
        return self._verify_online(session, http_timeout)

    # ---- Yöntemler --------------------------------------------------------

    def _login_with_password(self, session, form, ctx, base_url, timeout) -> bool:
        if not ctx.phone or not ctx.password:
            logger.error("[%s] Şifre yöntemi için telefon+şifre gerekli.", self.key)
            return False
        values = {"phone": ctx.phone, "password": ctx.password}
        values.update(ctx.extra_fields)
        try:
            resp = portal.submit_form(session, form, values, base_url, timeout)
        except Exception as exc:
            logger.error("[%s] Form gönderilemedi: %s", self.key, exc)
            return False
        logger.info("[%s] Şifre ile giriş formu gönderildi (HTTP %s).",
                    self.key, getattr(resp, "status_code", "?"))
        return True

    def _login_with_otp(self, session, form, ctx, base_url, timeout) -> bool:
        if not ctx.phone:
            logger.error("[%s] OTP yöntemi için telefon numarası gerekli.", self.key)
            return False
        if ctx.otp_provider is None:
            logger.error("[%s] OTP sağlayıcı fonksiyonu tanımlı değil.", self.key)
            return False

        # 1. Adım: numarayı gönder (bu genellikle SMS OTP tetikler).
        step1_values = {"phone": ctx.phone}
        step1_values.update(ctx.extra_fields)
        try:
            resp = portal.submit_form(session, form, step1_values, base_url, timeout)
        except Exception as exc:
            logger.error("[%s] OTP isteği gönderilemedi: %s", self.key, exc)
            return False

        base_url = resp.url or base_url

        # OTP alanı ilk formda zaten varsa ikinci sayfayı beklemeye gerek yok.
        forms2 = portal.parse_forms(resp.text)
        otp_form = next((f for f in forms2 if portal.form_needs_otp(f)), None)
        if otp_form is None:
            otp_form = portal.choose_login_form(forms2) or form

        # 2. Adım: OTP kodunu al (Mesajlar'dan veya kullanıcıdan).
        logger.info("[%s] OTP bekleniyor...", self.key)
        code = ctx.otp_provider()
        if not code:
            logger.error("[%s] OTP kodu alınamadı.", self.key)
            return False

        step2_values = {"otp": code, "phone": ctx.phone}
        if ctx.password:
            step2_values["password"] = ctx.password
        step2_values.update(ctx.extra_fields)
        try:
            portal.submit_form(session, otp_form, step2_values, base_url, timeout)
        except Exception as exc:
            logger.error("[%s] OTP gönderilemedi: %s", self.key, exc)
            return False
        logger.info("[%s] OTP ile giriş formu gönderildi.", self.key)
        return True

    def _verify_online(self, session, timeout: float, attempts: int = 3) -> bool:
        """Girişten sonra internetin gerçekten açıldığını doğrula."""
        for i in range(attempts):
            result = network.probe_connectivity(session, timeout=min(timeout, 8.0))
            if result.state == network.ONLINE:
                logger.info("[%s] Giriş başarılı: internet erişimi doğrulandı.", self.key)
                return True
            time.sleep(2)
        logger.warning("[%s] Giriş sonrası internet doğrulanamadı.", self.key)
        return False
