"""
AIS SUPER WiFi sağlayıcısı.

Portal: wifi.ais.co.th / ext-activities.ais.co.th (ASP.NET tabanlı).
İki giriş yöntemi vardır:
  - Numara + Şifre  (önerilen: OTP gerektirmez, tam otomatik)
  - Numara + SMS OTP

Giriş akışının kendisi genel motordan (BaseProvider) gelir; burada yalnızca
tanıma (matches) ve varsayılan giriş adresi özelleştirilir.
"""

from __future__ import annotations

from typing import Optional

from .base import BaseProvider

# Yapılandırmadan gelmezse kullanılacak varsayılan giriş adresi.
DEFAULT_AIS_LOGIN_URL = "https://ext-activities.ais.co.th/apps/wifigen/login.aspx"


class AISProvider(BaseProvider):
    key = "ais"
    name = "AIS SUPER WiFi"

    def __init__(self, login_url: Optional[str] = None):
        self.login_url = login_url or DEFAULT_AIS_LOGIN_URL

    def matches(self, ssid: Optional[str], portal_url: Optional[str],
                page_html: Optional[str]) -> bool:
        # SSID adında AIS geçiyorsa
        if ssid and "ais" in ssid.lower():
            return True
        # Portal, AIS alan adlarına yönlendirdiyse
        if portal_url:
            u = portal_url.lower()
            if "ais.co.th" in u or "ext-activities.ais" in u or "wifi.ais" in u:
                return True
        # Sayfa içeriğinde AIS izleri varsa
        if page_html and ("ais.co.th" in page_html.lower() or "wifi by ais" in page_html.lower()):
            return True
        return False
