"""
Tiny localization layer for the user-facing UI (English + Thai).

Only strings the user reads in the app are translated: menu items, dialogs,
notifications, the status label and the monitor's status messages. Code,
comments, logs and the --diagnose output stay in English. English is the
default and the fallback for any missing key/translation.

Usage:  t("menu.connect_now", lang)  ->  "Connect Now" / "เชื่อมต่อเดี๋ยวนี้"
Templates may take keyword arguments applied with str.format, e.g.
t("menu.about", lang, version="1.0.0").
"""

from __future__ import annotations

import logging

logger = logging.getLogger("aiswifi.i18n")

DEFAULT_LANG = "en"
# Language code -> the label shown for it in the menu (always in its own script).
LANGUAGES = {"en": "English", "th": "ไทย"}

# key -> {lang: template}. English is mandatory and used as the fallback.
STRINGS = {
    # --- Menu items ----------------------------------------------------------
    "menu.connect_now": {"en": "Connect Now", "th": "เชื่อมต่อเดี๋ยวนี้"},
    "menu.auto_connect": {"en": "Auto Connect", "th": "เชื่อมต่ออัตโนมัติ"},
    "menu.open_at_login": {"en": "Open at Login", "th": "เปิดเมื่อเข้าสู่ระบบ"},
    "menu.enter_credentials": {"en": "Enter Credentials…", "th": "กรอกข้อมูลเข้าสู่ระบบ…"},
    "menu.login_method": {"en": "Login Method", "th": "วิธีเข้าสู่ระบบ"},
    "menu.method_password": {"en": "Password (recommended)", "th": "รหัสผ่าน (แนะนำ)"},
    "menu.method_otp": {"en": "SMS OTP", "th": "OTP ทาง SMS"},
    "menu.otp_source": {"en": "SMS OTP Code", "th": "รหัส OTP ทาง SMS"},
    "menu.otp_messages": {"en": "Read code from Messages", "th": "อ่านรหัสจากแอป Messages"},
    "menu.otp_ask": {"en": "Ask me in a window", "th": "ถามฉันในหน้าต่าง"},
    "menu.language": {"en": "Language", "th": "ภาษา"},
    "menu.open_logs": {"en": "Open Logs", "th": "เปิดบันทึก"},
    "menu.about": {"en": "About (v{version})", "th": "เกี่ยวกับ (v{version})"},
    "menu.quit": {"en": "Quit", "th": "ออก"},

    # --- Labels in the status area -------------------------------------------
    "label.status": {"en": "Status", "th": "สถานะ"},
    "label.network": {"en": "Network", "th": "เครือข่าย"},
    "label.time_left": {"en": "Time left", "th": "เวลาที่เหลือ"},
    "label.ready": {"en": "Ready", "th": "พร้อม"},
    "time.unlimited": {"en": "Unlimited", "th": "ไม่จำกัด"},

    # --- Status words (by status code) ---------------------------------------
    "status.checking": {"en": "Checking…", "th": "กำลังตรวจสอบ…"},
    "status.online": {"en": "Connected", "th": "เชื่อมต่อแล้ว"},
    "status.offline": {"en": "No network", "th": "ไม่มีเครือข่าย"},
    "status.captive": {"en": "Connection lost", "th": "การเชื่อมต่อหลุด"},
    "status.logging_in": {"en": "Logging in…", "th": "กำลังเข้าสู่ระบบ…"},
    "status.error": {"en": "Error", "th": "ข้อผิดพลาด"},
    "status.idle": {"en": "Auto login off", "th": "ปิดการเข้าสู่ระบบอัตโนมัติ"},

    # --- Permissions submenu -------------------------------------------------
    "perm.menu": {"en": "Permissions", "th": "การอนุญาต"},
    "perm.fda_granted": {"en": "Full Disk Access: granted",
                         "th": "Full Disk Access: อนุญาตแล้ว"},
    "perm.fda_denied": {"en": "Full Disk Access: not granted — for SMS OTP auto-read",
                        "th": "Full Disk Access: ยังไม่อนุญาต — สำหรับอ่าน OTP อัตโนมัติ"},

    # --- Monitor status messages (the detail line) ---------------------------
    "msg.checking": {"en": "Checking…", "th": "กำลังตรวจสอบ…"},
    "msg.auto_off": {"en": "Auto login is off", "th": "ปิดการเข้าสู่ระบบอัตโนมัติ"},
    "msg.connected": {"en": "Connected", "th": "เชื่อมต่อแล้ว"},
    "msg.no_network": {"en": "No network (Wi-Fi may be off)",
                       "th": "ไม่มีเครือข่าย (Wi‑Fi อาจปิดอยู่)"},
    "msg.captive_auto_off": {
        "en": "Not connected — auto login is off (use Connect Now)",
        "th": "ยังไม่เชื่อมต่อ — ปิดการเข้าสู่ระบบอัตโนมัติ (ใช้ ‘เชื่อมต่อเดี๋ยวนี้’)"},
    "msg.connection_lost": {"en": "Connection lost, logging in…",
                            "th": "การเชื่อมต่อหลุด กำลังเข้าสู่ระบบ…"},
    "msg.unexpected": {"en": "Unexpected error (Open Logs)",
                       "th": "เกิดข้อผิดพลาดไม่คาดคิด (เปิดบันทึก)"},
    "msg.already_connected": {"en": "Already connected", "th": "เชื่อมต่ออยู่แล้ว"},
    "msg.no_network_short": {"en": "No network", "th": "ไม่มีเครือข่าย"},
    "msg.logging_in": {"en": "Logging in…", "th": "กำลังเข้าสู่ระบบ…"},
    "msg.no_provider": {"en": "No suitable provider found",
                        "th": "ไม่พบผู้ให้บริการที่รองรับ"},
    "msg.no_credentials": {"en": "No credentials — use ‘Enter Credentials…’",
                           "th": "ไม่มีข้อมูลเข้าสู่ระบบ — ใช้ ‘กรอกข้อมูลเข้าสู่ระบบ…’"},
    "msg.no_password": {"en": "No password saved — use ‘Enter Credentials…’",
                        "th": "ยังไม่ได้บันทึกรหัสผ่าน — ใช้ ‘กรอกข้อมูลเข้าสู่ระบบ…’"},
    "msg.provider_logging_in": {"en": "{provider}: logging in…",
                                "th": "{provider}: กำลังเข้าสู่ระบบ…"},
    "msg.login_success": {"en": "Login successful 🎉", "th": "เข้าสู่ระบบสำเร็จ 🎉"},
    "msg.login_failed_retry": {"en": "{reason} (will retry)",
                               "th": "{reason} (จะลองใหม่)"},
    "msg.waiting_otp_dialog": {"en": "Waiting for the OTP code (enter it in the dialog)…",
                               "th": "กำลังรอรหัส OTP (กรอกในหน้าต่าง)…"},
    "msg.waiting_sms": {"en": "Waiting for the SMS OTP…", "th": "กำลังรอรหัส OTP ทาง SMS…"},
    "msg.login_request_sent": {"en": "Login request sent…", "th": "ส่งคำขอเข้าสู่ระบบแล้ว…"},

    # --- Buttons -------------------------------------------------------------
    "btn.next": {"en": "Next", "th": "ถัดไป"},
    "btn.cancel": {"en": "Cancel", "th": "ยกเลิก"},
    "btn.save": {"en": "Save", "th": "บันทึก"},
    "btn.submit": {"en": "Submit", "th": "ส่ง"},
    "btn.open_settings": {"en": "Open Settings", "th": "เปิดการตั้งค่า"},
    "btn.later": {"en": "Later", "th": "ภายหลัง"},

    # --- Credentials dialogs -------------------------------------------------
    "dlg.phone.title": {"en": "{provider} — Phone Number",
                        "th": "{provider} — หมายเลขโทรศัพท์"},
    "dlg.phone.msg": {"en": "Enter the phone number registered with AIS (e.g. 08xxxxxxxx):",
                      "th": "กรอกหมายเลขโทรศัพท์ที่ลงทะเบียนกับ AIS (เช่น 08xxxxxxxx):"},
    "dlg.missing.title": {"en": "Missing information", "th": "ข้อมูลไม่ครบ"},
    "dlg.missing.msg": {"en": "The phone number cannot be empty.",
                        "th": "หมายเลขโทรศัพท์ต้องไม่ว่าง"},
    "dlg.password.title": {"en": "{provider} — Password", "th": "{provider} — รหัสผ่าน"},
    "dlg.password.msg": {
        "en": ("Enter your AIS SUPER WiFi password.\n"
               "(Leave it empty to keep the saved password; a password is not "
               "needed if you only use SMS OTP.)"),
        "th": ("กรอกรหัสผ่าน AIS SUPER WiFi ของคุณ\n"
               "(เว้นว่างไว้เพื่อเก็บรหัสผ่านเดิม; ไม่จำเป็นต้องมีรหัสผ่านหากใช้ OTP ทาง SMS เท่านั้น)")},
    "dlg.creds_error.title": {"en": "Error", "th": "ข้อผิดพลาด"},
    "dlg.creds_error.msg": {
        "en": "Could not save the credentials. Is 'keyring' installed?\nTerminal: pip install keyring",
        "th": "บันทึกข้อมูลเข้าสู่ระบบไม่ได้ ติดตั้ง 'keyring' แล้วหรือยัง?\nTerminal: pip install keyring"},

    # --- OTP dialog ----------------------------------------------------------
    "dlg.otp.title": {"en": "SMS OTP", "th": "OTP ทาง SMS"},
    "dlg.otp.msg": {"en": "Enter the verification code sent to your phone:",
                    "th": "กรอกรหัสยืนยันที่ส่งไปยังโทรศัพท์ของคุณ:"},

    # --- Full Disk Access dialog ---------------------------------------------
    "dlg.fda.title": {"en": "Full Disk Access needed", "th": "ต้องการสิทธิ์ Full Disk Access"},
    "dlg.fda.who_app": {
        "en": "add “{app}” (from the Applications folder), then quit and reopen this app.",
        "th": "เพิ่ม “{app}” (จากโฟลเดอร์ Applications) แล้วปิดและเปิดแอปนี้ใหม่"},
    "dlg.fda.who_terminal": {
        "en": ("add the app you started this from (e.g. Terminal), then quit and restart it. "
               "Tip: install it as a Mac app with make_app.py so the permission belongs to "
               "“{app}” itself."),
        "th": ("เพิ่มแอปที่คุณใช้เปิดสิ่งนี้ (เช่น Terminal) แล้วปิดและเปิดใหม่ "
               "เคล็ดลับ: ติดตั้งเป็นแอป Mac ด้วย make_app.py เพื่อให้สิทธิ์เป็นของ “{app}” เอง")},
    "dlg.fda.msg": {
        "en": ("To read the SMS code automatically, the app needs Full Disk Access to the "
               "Messages database. macOS does not let apps ask for this permission, so it "
               "has to be granted manually:\n\n"
               "System Settings → Privacy & Security → Full Disk Access → {who}\n\n"
               "Until then, the app will ask you for the code in a dialog."),
        "th": ("เพื่ออ่านรหัส SMS โดยอัตโนมัติ แอปต้องการสิทธิ์ Full Disk Access ในการเข้าถึงฐานข้อมูล "
               "Messages macOS ไม่อนุญาตให้แอปขอสิทธิ์นี้ จึงต้องให้ด้วยตนเอง:\n\n"
               "System Settings → Privacy & Security → Full Disk Access → {who}\n\n"
               "ในระหว่างนี้ แอปจะถามรหัสจากคุณในหน้าต่าง")},

    # --- Open at Login dialogs ----------------------------------------------
    "dlg.login_item.title": {"en": "Open at Login", "th": "เปิดเมื่อเข้าสู่ระบบ"},
    "dlg.login_item.unavailable": {
        "en": ("Open at Login is available when the app is installed as a Mac app "
               "(macOS 13 or later).\n\nIn Terminal, in the project folder, run:\n"
               "python3 make_app.py\n\nthen open “{app}” from Applications."),
        "th": ("เปิดเมื่อเข้าสู่ระบบใช้ได้เมื่อติดตั้งเป็นแอป Mac (macOS 13 ขึ้นไป)\n\n"
               "ใน Terminal ที่โฟลเดอร์โปรเจกต์ ให้รัน:\npython3 make_app.py\n\n"
               "จากนั้นเปิด “{app}” จากโฟลเดอร์ Applications")},
    "dlg.login_item.approval": {
        "en": ("macOS needs your approval: turn on “{app}” in "
               "System Settings → General → Login Items."),
        "th": ("macOS ต้องการการอนุมัติ: เปิด “{app}” ใน "
               "System Settings → General → Login Items")},
    "dlg.login_item.error": {"en": "Could not change the login item:\n{error}",
                             "th": "เปลี่ยนรายการเข้าสู่ระบบไม่ได้:\n{error}"},

    # --- Misc dialogs / notifications ---------------------------------------
    "dlg.logs_error.title": {"en": "Could not open logs", "th": "เปิดบันทึกไม่ได้"},
    "dlg.about.title": {"en": "{app} v{version}", "th": "{app} v{version}"},
    "dlg.about.msg": {
        "en": ("Automatically logs in to AIS SUPER WiFi and similar captive portals.\n\n"
               "• Automatic re-login when the connection drops\n"
               "• Password or SMS OTP method\n"
               "• Extensible with new providers\n\n"
               "Your credentials are stored in the macOS Keychain."),
        "th": ("เข้าสู่ระบบ AIS SUPER WiFi และ captive portal ที่คล้ายกันโดยอัตโนมัติ\n\n"
               "• เข้าสู่ระบบใหม่อัตโนมัติเมื่อการเชื่อมต่อหลุด\n"
               "• วิธีรหัสผ่าน หรือ OTP ทาง SMS\n"
               "• เพิ่มผู้ให้บริการใหม่ได้\n\n"
               "ข้อมูลเข้าสู่ระบบของคุณถูกเก็บไว้ใน macOS Keychain")},
    "notif.connected.title": {"en": "Connected", "th": "เชื่อมต่อแล้ว"},
    "notif.connected.body": {"en": "Internet access is back.", "th": "อินเทอร์เน็ตกลับมาแล้ว"},
    "notif.lost.title": {"en": "Connection lost", "th": "การเชื่อมต่อหลุด"},
    "notif.lost.body": {"en": "Trying to log in automatically…",
                        "th": "กำลังลองเข้าสู่ระบบอัตโนมัติ…"},
    "notif.failed.title": {"en": "Login failed", "th": "เข้าสู่ระบบไม่สำเร็จ"},
    "notif.failed.body": {"en": "Will retry.", "th": "จะลองใหม่"},
    "notif.saved.title": {"en": "Saved", "th": "บันทึกแล้ว"},
    "notif.saved.body": {"en": "{provider} credentials were saved to the Keychain.",
                         "th": "บันทึกข้อมูล {provider} ไว้ใน Keychain แล้ว"},
    "notif.otp.title": {"en": "SMS OTP required", "th": "ต้องการรหัส OTP"},
    "notif.otp.body": {"en": "Enter the code sent to your phone in the dialog.",
                       "th": "กรอกรหัสที่ส่งไปยังโทรศัพท์ของคุณในหน้าต่าง"},
}


def normalize(lang) -> str:
    """Return a supported language code, defaulting to English."""
    return lang if lang in LANGUAGES else DEFAULT_LANG


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """Translate `key` into `lang` (English fallback), applying str.format kwargs."""
    entry = STRINGS.get(key)
    if entry is None:
        logger.debug("Missing i18n key: %s", key)
        return key
    template = entry.get(normalize(lang)) or entry.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return template
    return template
