"""
Provider registry and automatic detection.

To add a new provider:
  1) Create a new file in this folder (e.g. `truemove.py`),
  2) Write a class that derives from `BaseProvider`,
  3) Add it to `build_registry()` below.
"""

from __future__ import annotations

from typing import List, Optional

from .base import BaseProvider
from .ais import AISProvider
from .generic import GenericProvider


def build_registry(ais_login_url: Optional[str] = None) -> List[BaseProvider]:
    """
    Build the provider list in detection order.
    IMPORTANT: GenericProvider must always be LAST (it matches everything).
    """
    return [
        AISProvider(login_url=ais_login_url),
        # New providers can be added here, e.g.:
        # TrueMoveProvider(),
        GenericProvider(),
    ]


def detect_provider(registry: List[BaseProvider],
                    ssid: Optional[str],
                    portal_url: Optional[str],
                    page_html: Optional[str],
                    preferred_key: Optional[str] = None) -> Optional[BaseProvider]:
    """
    Return the first provider that matches the current network.
    If `preferred_key` is given and it matches, it takes precedence.
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
