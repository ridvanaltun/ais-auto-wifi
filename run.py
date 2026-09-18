#!/usr/bin/env python3
"""
AIS Wi-Fi Auto-Login — başlatıcı.

Kullanım:
  python3 run.py             # menü çubuğu uygulamasını başlat (rumps gerekir)
  python3 run.py --diagnose  # ağ/OTP/SSID teşhisi (arayüz açmaz)
  python3 run.py --version   # sürümü yazdır

Not: --diagnose ve --version, rumps kurulu olmasa da çalışır.
"""

from __future__ import annotations

import sys


def _diagnose() -> int:
    """Arayüz açmadan ağ durumunu ve yardımcıları test et."""
    from aiswifi import config as config_mod
    from aiswifi import network, otp
    from aiswifi import providers as providers_mod

    config_mod.setup_logging(verbose=True)
    print("== AIS Wi-Fi Auto-Login — Teşhis ==\n")

    cfg = config_mod.load_config()
    print(f"Ayar dosyası : {config_mod.CONFIG_PATH}")
    print(f"Log dosyası  : {config_mod.LOG_PATH}")
    print(f"Yöntem       : {cfg.get('login_method')}")
    print(f"OTP kaynağı  : {cfg.get('otp_source')}\n")

    ssid = network.get_ssid()
    print(f"SSID         : {ssid or '(okunamadı — Konum izni gerekebilir)'}")

    iface = network.get_wifi_interface()
    print(f"Wi-Fi arayüz : {iface}")

    print("\nBağlantı yoklanıyor…")
    result = network.probe_connectivity(timeout=8.0)
    print(f"Durum        : {result.state}")
    if result.portal_url:
        print(f"Portal adresi: {result.portal_url}")

    registry = providers_mod.build_registry(cfg.get("ais_login_url"))
    provider = providers_mod.detect_provider(
        registry, ssid, result.portal_url, result.body,
        preferred_key=cfg.get("preferred_provider"),
    )
    print(f"Sağlayıcı    : {provider.name if provider else '(bulunamadı)'}")

    if provider:
        phone, password = config_mod.get_credentials(provider.key)
        print(f"Telefon kayıt: {'var' if phone else 'YOK'}")
        print(f"Şifre kayıt  : {'var' if password else 'YOK'}")

    print("\nSon 5 dk içinde SMS OTP aranıyor…")
    code = otp.read_latest_otp(within_seconds=300)
    if code:
        print(f"Bulunan OTP  : {code}")
    else:
        print("OTP bulunamadı (Mesajlar erişimi/Full Disk Access ya da forwarding kapalı olabilir).")

    print("\nTeşhis tamamlandı.")
    return 0


def _version() -> int:
    from aiswifi import __app_name__, __version__
    print(f"{__app_name__} v{__version__}")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if "--version" in argv or "-v" in argv:
        return _version()
    if "--diagnose" in argv or "-d" in argv:
        return _diagnose()

    # Normal mod: menü çubuğu uygulaması
    try:
        from aiswifi.app import run
    except ImportError as exc:
        print("Menü çubuğu uygulaması için 'rumps' gerekli.", file=sys.stderr)
        print(f"Ayrıntı: {exc}", file=sys.stderr)
        print("Kurulum: pip install rumps", file=sys.stderr)
        print("Sadece test için: python3 run.py --diagnose", file=sys.stderr)
        return 1
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
