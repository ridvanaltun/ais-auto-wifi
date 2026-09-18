"""
Generic (fallback) provider.

Used as a last resort when no known provider matches. It tries to fill in
the form at the portal's redirect URL with the generic engine. It is not
guaranteed to work on every portal, but it does the job for many simple
captive portals.
"""

from __future__ import annotations

from typing import Optional

from .base import BaseProvider


class GenericProvider(BaseProvider):
    key = "generic"
    name = "Generic Captive Portal"

    def matches(self, ssid: Optional[str], portal_url: Optional[str],
                page_html: Optional[str]) -> bool:
        # As the fallback it always matches; it is placed last in the registry.
        return True

    def resolve_login_url(self, portal_url: Optional[str]) -> Optional[str]:
        # The generic provider has no fixed URL; it uses the one the portal gives.
        return portal_url
