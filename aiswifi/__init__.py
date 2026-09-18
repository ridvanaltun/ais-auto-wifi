"""
AIS Wi-Fi Auto-Login — a macOS menu bar (tray) application.

Automatically logs back in when the connection drops on free captive-portal
Wi-Fi networks such as AIS SUPER WiFi. New providers can easily be added
under `aiswifi/providers/`.
"""

__version__ = "1.0.1"
__app_name__ = "AIS Wi-Fi Auto-Login"
# Bundle identifier of the Mac app built by make_app.py.
__bundle_id__ = "com.aiswifi.autologin"

__all__ = ["__version__", "__app_name__", "__bundle_id__"]
