"""
Removed: reading the SMS OTP from the macOS Messages database.

This never worked for the app's purpose. On a captive portal the Mac has no
internet, so the SMS forwarded from the iPhone (over Apple's push servers)
does not arrive in Messages until you are already logged in — exactly when
the code is no longer needed. The OTP is therefore always requested from the
user in a dialog instead (see aiswifi/app.py `_ask_otp`).

This module is intentionally empty and can be deleted.
"""
