"""
Sağlayıcı kayıt defteri ve otomatik tespit.

Yeni bir sağlayıcı eklemek için:
  1) Bu klasöre yeni bir dosya oluşturun (ör. `truemove.py`),
  2) `BaseProvider`tan türeyen bir sınıf yazın,
  3) Aşağıdaki `build_registry()` içine ekleyin.
"""

from __future__ import annotations

from typing import List, Optional

from .base import BaseProvider
from .ais import AISProvider
from .generic import GenericProvider


def build_registry(ais_login_url: Optional[str] = None) -> List[BaseProvider]:
    """
    Tanıma sırasına göre sağlayıcı listesini oluştur.
    ÖNEMLİ: GenericProvider her zaman en SONDA olmalı (o her şeyle eşleşir).
    """
    return [
        AISProvider(login_url=ais_login_url),
        # Buraya yeni sağlayıcılar eklenebilir, ör:
        # TrueMoveProvider(),
        GenericProvider(),
    ]


def detect_provider(registry: List[BaseProvider],
                    ssid: Optional[str],
                    portal_url: Optional[str],
                    page_html: Optional[str],
                    preferred_key: Optional[str] = None) -> Optional[BaseProvider]:
    """
    Mevcut ağa uyan ilk sağlayıcıyı döndür.
    `preferred_key` verilmişse ve eşleşiyorsa ona öncelik verilir.
    """
    if preferred_key:
        for p in registry:
            if p.key == preferred_key and p.matches(ssid, portal_url, page_html):
                return p
    for p in registry:
        if p.matches(ssid, portal_url, page_html):
            return p
    return None


def get_provider_by_key(registry: List[BaseProvider], key: str) -> Optional[BaseProvider]:
    for p in registry:
        if p.key == key:
            return p
    return None
