"""
Genel (fallback) sağlayıcı.

Bilinen hiçbir sağlayıcı eşleşmediğinde son çare olarak kullanılır. Portalın
yönlendirdiği adresteki formu genel motorla doldurmaya çalışır. Her portalda
çalışacağının garantisi yoktur, ama pek çok basit captive portal için iş görür.
"""

from __future__ import annotations

from typing import Optional

from .base import BaseProvider


class GenericProvider(BaseProvider):
    key = "generic"
    name = "Genel Captive Portal"

    def matches(self, ssid: Optional[str], portal_url: Optional[str],
                page_html: Optional[str]) -> bool:
        # Fallback olarak her zaman eşleşir; kayıt listesinde en sona konur.
        return True

    def resolve_login_url(self, portal_url: Optional[str]) -> Optional[str]:
        # Genel sağlayıcının sabit bir adresi yoktur; portalın verdiğini kullanır.
        return portal_url
