"""
Ağ yardımcıları: internet/captive-portal tespiti, SSID okuma, arayüz bulma.

Tasarım notu:
- "Captive portal" tespiti için Apple'ın kullandığı yönteme benzer bir yol
  izlenir: bilinen bir "yoklama" (probe) adresine gidilir. Gerçek internet
  varsa sabit bir "Success" yanıtı döner. Bir portal araya giriyorsa yanıt
  farklı olur (yönlendirme veya portal sayfası) → giriş yapmak gerekir.
- SSID okumak macOS Sonoma+ üzerinde konum izni isteyebildiği için kritik
  DEĞİLDİR; sağlayıcı, portalın yönlendirdiği adresten de tespit edilebilir.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger("aiswifi.network")

# macOS'un captive tespitinde kullandığı adres. Gerçek internet varken
# gövdesi tam olarak "Success" içeren küçük bir HTML döndürür.
APPLE_PROBE_URL = "http://captive.apple.com/hotspot-detect.html"
APPLE_PROBE_EXPECT = "Success"

# Yedek yoklama: içerik döndürmemesi (204) beklenen adresler.
FALLBACK_PROBES = [
    "http://www.gstatic.com/generate_204",
    "http://connectivitycheck.gstatic.com/generate_204",
]

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Durumlar
ONLINE = "online"     # gerçek internet var
CAPTIVE = "captive"   # ağ var ama portal engelliyor → giriş gerekli
OFFLINE = "offline"   # hiç ağ/erişim yok (ör. Wi-Fi kapalı)


@dataclass
class ProbeResult:
    state: str                      # ONLINE / CAPTIVE / OFFLINE
    portal_url: Optional[str] = None  # captive ise ulaşılan portal adresi
    body: Optional[str] = None        # captive ise portal sayfası HTML'i


def new_session() -> requests.Session:
    """Tarayıcı benzeri, yönlendirmeleri izleyen bir HTTP oturumu."""
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"})
    s.trust_env = False  # sistem proxy ayarlarını yok say (portal arkasında sorun çıkarabilir)
    return s


def probe_connectivity(session: Optional[requests.Session] = None,
                       timeout: float = 8.0) -> ProbeResult:
    """
    İnternet durumunu döndür.

    Mantık:
      1) Apple probe adresine git.
         - Gövde "Success" ise → ONLINE.
         - Farklı içerik / yönlendirme geldiyse → CAPTIVE (portal HTML'i ile).
      2) Apple probe'a hiç ulaşılamazsa yedek 204 adresleri denenir.
         - 204 gelirse → ONLINE.
      3) Hiçbiri olmadıysa → OFFLINE.
    """
    sess = session or new_session()

    # 1) Apple probe
    try:
        resp = sess.get(APPLE_PROBE_URL, timeout=timeout, allow_redirects=True)
        text = (resp.text or "").strip()
        if resp.status_code == 200 and APPLE_PROBE_EXPECT in text and len(text) < 200:
            return ProbeResult(ONLINE)
        # 200 ama beklenen içerik yok ya da yönlendirildik → portal araya girdi
        portal_url = resp.url if resp.url and "captive.apple.com" not in resp.url else None
        return ProbeResult(CAPTIVE, portal_url=portal_url, body=resp.text)
    except requests.RequestException as exc:
        logger.debug("Apple probe başarısız: %s", exc)

    # 2) Yedek 204 yoklamaları
    for url in FALLBACK_PROBES:
        try:
            r = sess.get(url, timeout=timeout, allow_redirects=False)
            if r.status_code == 204:
                return ProbeResult(ONLINE)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                return ProbeResult(CAPTIVE, portal_url=loc)
            if r.status_code == 200 and r.text:
                return ProbeResult(CAPTIVE, portal_url=r.url, body=r.text)
        except requests.RequestException:
            continue

    # 3) Hiçbir şeye ulaşılamadı
    return ProbeResult(OFFLINE)


# --- SSID / arayüz tespiti (macOS) ------------------------------------------

def get_wifi_interface() -> str:
    """
    Wi-Fi donanım arayüzünün adını bul (genelde 'en0').
    `networksetup -listallhardwareports` çıktısını ayrıştırır.
    """
    try:
        out = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        # "Hardware Port: Wi-Fi\nDevice: en0\n..."
        blocks = out.split("Hardware Port:")
        for b in blocks:
            if "Wi-Fi" in b or "AirPort" in b:
                m = re.search(r"Device:\s*(\w+)", b)
                if m:
                    return m.group(1)
    except Exception as exc:
        logger.debug("Wi-Fi arayüzü bulunamadı: %s", exc)
    return "en0"


def get_ssid() -> Optional[str]:
    """
    Bağlı olunan Wi-Fi ağının adını (SSID) döndür. Bulunamazsa None.

    Sırasıyla denenir:
      1) CoreWLAN (PyObjC) — en güvenilir, ama Sonoma+ için Konum izni ister.
      2) `networksetup -getairportnetwork <iface>`
      3) Eski `airport -I` aracı (yeni macOS'ta olmayabilir).
    """
    # 1) CoreWLAN
    try:
        from CoreWLAN import CWWiFiClient  # type: ignore
        iface = CWWiFiClient.sharedWiFiClient().interface()
        if iface is not None:
            ssid = iface.ssid()
            if ssid:
                return str(ssid)
    except Exception as exc:
        logger.debug("CoreWLAN ile SSID okunamadı: %s", exc)

    # 2) networksetup
    try:
        iface = get_wifi_interface()
        out = subprocess.run(
            ["networksetup", "-getairportnetwork", iface],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        # "Current Wi-Fi Network: AIS SUPER WIFI"
        if ":" in out and "not associated" not in out.lower():
            return out.split(":", 1)[1].strip() or None
    except Exception as exc:
        logger.debug("networksetup ile SSID okunamadı: %s", exc)

    # 3) Eski airport aracı
    airport = ("/System/Library/PrivateFrameworks/Apple80211.framework/"
               "Versions/Current/Resources/airport")
    try:
        out = subprocess.run([airport, "-I"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"^\s*SSID:\s*(.+)$", out, re.MULTILINE)
        if m:
            return m.group(1).strip() or None
    except Exception:
        pass

    return None
