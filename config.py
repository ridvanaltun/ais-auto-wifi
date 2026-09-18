"""
Yapılandırma (config) ve kimlik bilgisi yönetimi.

- Ayarlar düz bir JSON dosyasında tutulur:  ~/.config/aiswifi/config.json
- Gizli bilgiler (telefon numarası + şifre) macOS Anahtar Zinciri'nde
  (Keychain) saklanır; JSON'a ASLA yazılmaz. `keyring` yoksa, en kötü
  ihtimalle çalışmaya devam eder ama kimlik bilgisini hatırlayamaz.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("aiswifi.config")

# --- Yollar -----------------------------------------------------------------

CONFIG_DIR = Path(os.path.expanduser("~/.config/aiswifi"))
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "aiswifi.log"

KEYRING_SERVICE = "aiswifi"

# --- Varsayılan ayarlar -----------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    # Otomatik giriş açık mı?
    "auto_login": True,
    # Bağlantı kaç saniyede bir kontrol edilsin?
    "poll_interval": 15,
    # Tercih edilen giriş yöntemi: "password" (önerilen) veya "otp"
    "login_method": "password",
    # OTP kaynağı: "messages" (Mesajlar uygulamasından otomatik oku),
    #             "ask" (bir pencere ile bana sor) veya "none"
    "otp_source": "messages",
    # Tercih edilen sağlayıcı anahtarı; None ise otomatik tespit edilir.
    "preferred_provider": None,
    # Bir giriş denemesi başarısız olursa kaç kez tekrar denensin?
    "max_retries": 3,
    # Başarısızlıkta bekleme (saniye) — üssel artışın taban ve tavan değeri.
    "backoff_base": 5,
    "backoff_max": 120,
    # HTTP istekleri için zaman aşımı (saniye).
    "http_timeout": 12,
    # OTP SMS'i en fazla kaç saniye beklensin?
    "otp_wait_timeout": 90,
    # AIS portal giriş adresi (gerekirse değiştirilebilir).
    "ais_login_url": "https://ext-activities.ais.co.th/apps/wifigen/login.aspx",
    # Bilgilendirme bildirimleri gösterilsin mi?
    "notifications": True,
}


def ensure_config_dir() -> None:
    """Yapılandırma klasörünün var olduğundan emin ol."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Dict[str, Any]:
    """Ayarları oku; eksik anahtarları varsayılanlarla tamamla."""
    ensure_config_dir()
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("config.json okunamadı, varsayılanlar kullanılıyor: %s", exc)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    """Ayarları diske yaz (yalnızca bilinen anahtarlar)."""
    ensure_config_dir()
    clean = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False, indent=2)
        tmp.replace(CONFIG_PATH)
    except OSError as exc:
        logger.error("config.json yazılamadı: %s", exc)


# --- Kimlik bilgileri (Keychain) --------------------------------------------

def _account_name(provider_key: str, field: str) -> str:
    return f"{provider_key}:{field}"


def set_credentials(provider_key: str, phone: str, password: str) -> bool:
    """Telefon + şifreyi Anahtar Zinciri'ne kaydet. Başarılıysa True döner."""
    try:
        import keyring  # type: ignore
    except Exception as exc:  # keyring kurulu değil
        logger.error("keyring bulunamadı, kimlik bilgisi kaydedilemiyor: %s", exc)
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, _account_name(provider_key, "phone"), phone or "")
        keyring.set_password(KEYRING_SERVICE, _account_name(provider_key, "password"), password or "")
        return True
    except Exception as exc:
        logger.error("Anahtar Zinciri'ne yazılamadı: %s", exc)
        return False


def get_credentials(provider_key: str) -> Tuple[Optional[str], Optional[str]]:
    """Kayıtlı (telefon, şifre) çiftini getir. Yoksa (None, None)."""
    try:
        import keyring  # type: ignore
    except Exception:
        return (None, None)
    try:
        phone = keyring.get_password(KEYRING_SERVICE, _account_name(provider_key, "phone"))
        password = keyring.get_password(KEYRING_SERVICE, _account_name(provider_key, "password"))
        return (phone or None, password or None)
    except Exception as exc:
        logger.error("Anahtar Zinciri okunamadı: %s", exc)
        return (None, None)


def clear_credentials(provider_key: str) -> None:
    """Kayıtlı kimlik bilgilerini sil."""
    try:
        import keyring  # type: ignore
    except Exception:
        return
    for field in ("phone", "password"):
        try:
            keyring.delete_password(KEYRING_SERVICE, _account_name(provider_key, field))
        except Exception:
            pass


# --- Loglama ----------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    """Hem dosyaya hem konsola log yaz."""
    ensure_config_dir()
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("aiswifi")
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
