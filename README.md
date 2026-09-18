# AIS Wi-Fi Auto-Login 🛜

A small macOS menu bar (tray) app that **automatically logs you back in**
when the connection drops on free captive-portal Wi-Fi networks such as
**AIS SUPER WiFi**.

You're sitting in a café, the connection drops every 30 minutes, and each time
you have to enter your phone number + an SMS OTP. This app keeps watching the
connection in the background; as soon as it notices a drop it finds the
portal, logs in with your credentials and verifies that you're online again.
The menu bar icon shows the current state at a glance.

> **Note:** This app automates logging in to a Wi-Fi network you are already
> legally allowed to use, with your own number and credentials. It does not
> bypass any security measure, crack passwords or use anyone else's account.

---

## What do the icons mean?

| Icon | Status |
|------|--------|
| ⏳ | Checking (at launch / when auto login is turned back on) |
| 🛜 | Online — all good |
| 📴 | No connection (Wi-Fi may be off) |
| 🔒 | Captive portal detected (login required) |
| 🔄 | Logging in… |
| ⚠️ | Error (the second menu line shows the reason) |
| ⏸️ | Auto login is off |

---

## Installation

### 1. Requirements

- macOS
- Python 3.9 or newer (check with `python3 --version`)

### 2. Install the dependencies

Open a Terminal in this folder and run:

```bash
pip3 install -r requirements.txt
```

### 3. Run the diagnostics first (test without the UI)

```bash
python3 run.py --diagnose
```

This command shows the SSID, the Wi-Fi interface, the connection state, the
detected provider, whether credentials are saved, and the SMS OTP from the
last 5 minutes. If something is wrong, this is where you'll see it.

### 4. Start the app

```bash
python3 run.py
```

The 🛜 icon appears in the menu bar. On first launch, click the icon in the
top-right corner.

---

## First use

1. From **Enter Credentials…**, enter your phone number (and your password if
   you chose the password method). They are saved to the **macOS Keychain**
   and never kept as plain text in any file.
2. Pick one option from the **Login Method** submenu:
   - **Password (recommended):** fully automatic, single-step login on the AIS
     portal with number + password. No waiting for an SMS. *This is the most
     trouble-free method.* (You can set a Wi-Fi password from the AIS
     app/portal.)
   - **SMS OTP:** the app enters your number, the portal sends an SMS, and the
     app reads the code from Messages and enters it automatically (the
     permissions below are required). If Messages cannot be read, or the code
     does not arrive in time, it asks you in a small dialog. Since every
     attempt means a new SMS, the OTP method waits at least 60 seconds
     between failed attempts.
3. As long as **Auto Connect** is on, the app handles disconnects on its own.
   You can also trigger it manually at any time with **Connect Now**.

---

## Reading the SMS OTP automatically (optional but recommended)

Only needed if you use the **SMS OTP** method. The app reads the incoming code
from the database of the **Messages** app on your Mac. For that:

### A) iPhone → Mac SMS forwarding
On the iPhone: **Settings → Messages → Text Message Forwarding** → turn on your
Mac. This way the SMS code from AIS also lands in Messages on your Mac.

### B) Full Disk Access
To read the Messages database, grant permission to Terminal (or to the .app if
you packaged the app):
**System Settings → Privacy & Security → Full Disk Access** → add and check
Terminal. Then quit and reopen Terminal.

> **If you start it with a LaunchAgent (at login):** Terminal's permission
> does NOT apply; you need to grant it to the Python interpreter written in the
> plist itself (find the real path with
> `python3 -c "import sys; print(sys.executable)"` and add that file to the
> Full Disk Access list with ⌘⇧G).

The **Messages : readable / unreadable** line in the output of
`python3 run.py --diagnose` shows whether the permission works.

If you'd rather not grant these permissions, that's fine: with the OTP method
selected, if the app cannot read the code it asks you for it **manually** in a
small dialog. Or skip all of this and use the **Password** method.

---

## SSID reading and Location permission

macOS requires **Location Services** permission to read the name (SSID) of the
Wi-Fi network you're connected to. Without it the app still works; it just
detects "which network am I on" from the portal URL/HTML. To see the SSID:
**System Settings → Privacy & Security → Location Services**.

Note: on macOS 14.4 and later, a Python script that is not packaged as an .app
cannot request Location permission, and `networksetup`/`airport` hide the
network name too; so the SSID will most likely show as "(unreadable)". This is
expected and does not affect logging in.

---

## Starting automatically at login

To have the app start by itself every time you log in, use the bundled
`com.aiswifi.autologin.plist` file:

1. Open the file in a text editor and fix the two PATHS in it for your machine
   (`which python3` for the Python path, `pwd` in this folder for the folder path).
2. Copy and load it:
   ```bash
   cp com.aiswifi.autologin.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.aiswifi.autologin.plist
   ```
3. To remove it:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.aiswifi.autologin.plist
   ```

---

## Adding other Wi-Fi hotspots later

The architecture is designed for this. Each network lives as a "provider"
under `aiswifi/providers/`. To add a new network:

1. Use `aiswifi/providers/ais.py` as an example, create a new file
   (e.g. `truewifi.py`) and derive from `BaseProvider`.
2. In `matches()`, describe how to recognise that network (SSID name, portal
   domain or page HTML).
3. Add it to the `build_registry()` list in `aiswifi/providers/__init__.py`
   (`GenericProvider` must always stay last — it is the last resort that uses
   the generic HTML-form login engine for unknown portals).

For most simple captive portals you may not need to write anything at all:
`GenericProvider` finds the form automatically and tries by guessing the
number/password/OTP fields.

---

## Portals with their own certificate (optional)

Some hotel/corporate portals serve their login page with a self-signed
certificate. In that case the app **does not log in, for your safety**, and
writes the portal's certificate fingerprint (SHA-256) to the logs. If you trust
the portal and have verified the fingerprint through another channel, add it
to `~/.config/aiswifi/config.json` and restart the app:

```json
"portal_cert_pins": {"portal.example-hotel.com": "<64-character SHA-256 fingerprint>"}
```

This only applies to portal requests to that host: exactly that certificate is
accepted instead of the chain. The connectivity probe and all other addresses
always use full verification. (The AIS portal does not need this.)

---

## Tests

Unit tests with no extra dependencies (standard `unittest`):

```bash
python3 -m unittest discover -s tests -v
```

---

## File and log locations

- Settings: `~/.config/aiswifi/config.json`
- Log: `~/.config/aiswifi/aiswifi.log` (**Open Logs** in the menu; rotates at
  1 MB, the last 3 files are kept). Passwords and OTPs are never logged.
- Credentials: **macOS Keychain** (`aiswifi` service) — not in a file.

---

## Troubleshooting

- **"requires 'rumps'" error:** `pip3 install rumps` (or `pip3 install -r
  requirements.txt`).
- **The SSID is always empty:** Location permission may be off; that's fine,
  logging in still works.
- **The OTP is not read automatically:** Are Text Message Forwarding and Full
  Disk Access on? If not, the app will ask you for the code. Or switch to the
  Password method.
- **Login failed / error icon:** check the output of `python3 run.py --diagnose`
  and the log file via **Open Logs**.

---

## Command summary

```bash
python3 run.py             # start the menu bar app
python3 run.py --diagnose  # network/OTP/SSID diagnostics without the UI
python3 run.py --version   # version
```
