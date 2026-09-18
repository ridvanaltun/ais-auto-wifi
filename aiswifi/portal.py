"""
Generic captive-portal login engine.

The AIS portal is built on ASP.NET; its forms contain hidden fields such as
`__VIEWSTATE` and `__EVENTVALIDATION`. This engine:
  1) downloads the login page,
  2) parses the forms and fields in it,
  3) classifies visible fields by keywords as phone / password / OTP /
     submit,
  4) fills in and submits the form while keeping hidden fields (viewstate
     etc.) EXACTLY as they are.

It relies on patterns rather than field names so that it keeps working when
names change; this also makes it reusable for other providers.

Form data is built the way a browser would send it: only checked
checkboxes/radios, the selected <select> option and a SINGLE submit button
(the one "clicked") are sent; disabled fields are skipped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import quote, quote_plus, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

logger = logging.getLogger("aiswifi.portal")

# Keyword sets for field classification (English + Thai + generic).
PHONE_KEYS = re.compile(
    r"(msisdn|mobile|phone|tel|number|user(name)?|เบอร์|หมายเลข|โทร)", re.IGNORECASE
)
PASSWORD_KEYS = re.compile(r"(pass|pwd|รหัสผ่าน)", re.IGNORECASE)
OTP_KEYS = re.compile(r"(otp|code|pin|verif|รหัส(?!ผ่าน)|ยืนยัน)", re.IGNORECASE)

# Submit button choice: "log in / confirm" buttons ...
LOGIN_BUTTON_KEYS = re.compile(
    r"(login|log\s?in|signin|sign[\s-]?in|connect|submit|ok|confirm|verif|"
    r"ตกลง|เข้าสู่ระบบ|เชื่อมต่อ|ยืนยัน)",
    re.IGNORECASE,
)
# ... and "request code / send SMS" buttons.
OTP_REQUEST_KEYS = re.compile(r"(request|resend|send|sms|ขอ|ส่ง)", re.IGNORECASE)

# Button types that are sent when clicked (only ONE of them is sent).
BUTTON_TYPES = ("submit", "image")
# Field types a browser never sends.
NEVER_SENT_TYPES = ("button", "reset", "file")
# Choice fields: credentials are NOT written into them; they keep their own values.
CHOICE_TYPES = ("checkbox", "radio", "select")

# Used to hide URL query strings in log/error text.
_URL_QUERY_RE = re.compile(r"\?[^\s'\"<>)]*")


@dataclass
class FormField:
    name: str
    type: str = "text"
    value: str = ""
    id: str = ""
    placeholder: str = ""
    label: str = ""          # <button> label (used when choosing the button)
    checked: bool = False    # for checkbox/radio
    disabled: bool = False   # browsers do not send disabled fields

    def haystack(self) -> str:
        """Combined text used for classification."""
        parts = [self.name, self.id, self.placeholder]
        if self.type in BUTTON_TYPES:
            # For buttons the visible text ("Login", "Request OTP") is the best hint.
            parts += [self.value, self.label]
        return " ".join(parts).lower()


@dataclass
class Form:
    action: str
    method: str = "post"
    fields: List[FormField] = field(default_factory=list)

    def hidden_payload(self) -> Dict[str, str]:
        """Name→value mapping of hidden fields (viewstate etc.)."""
        payload: Dict[str, str] = {}
        for f in self.fields:
            if f.type == "hidden" and f.name and not f.disabled:
                payload[f.name] = f.value
        return payload


@dataclass
class LoginContext:
    """Everything needed for one login attempt."""
    phone: Optional[str] = None
    password: Optional[str] = None
    # Called when an OTP is needed; returns the code (str) or None.
    otp_provider: Optional[Callable[[], Optional[str]]] = None
    # Called RIGHT BEFORE the form submission that triggers the SMS (e.g. to
    # take a reference point in the Messages database). Providers that
    # override `_login_with_otp` must also call it before step 1.
    otp_prepare: Optional[Callable[[], None]] = None
    # Extra values written into the form BY FIELD NAME (e.g. {"chkAccept": "on"}).
    extra_fields: Dict[str, str] = field(default_factory=dict)


def _select_value(sel) -> Optional[str]:
    """The value a browser sends for a <select> (selected or first option)."""
    options = sel.find_all("option")
    if not options:
        return None
    chosen = next((o for o in options if o.has_attr("selected")), options[0])
    if chosen.has_attr("value"):
        return chosen.get("value") or ""
    return chosen.get_text(strip=True)


def parse_forms(html: str) -> List[Form]:
    """Parse all forms in the HTML."""
    soup = BeautifulSoup(html or "", "html.parser")
    forms: List[Form] = []
    for f in soup.find_all("form"):
        action = (f.get("action") or "").strip()
        method = (f.get("method") or "post").lower()
        fields: List[FormField] = []
        for inp in f.find_all(["input", "select", "textarea", "button"]):
            name = inp.get("name") or ""
            if not name:
                continue
            label = ""
            if inp.name == "select":
                itype = "select"
                value = _select_value(inp)
                if value is None:
                    continue  # a <select> without options is not sent by browsers
            elif inp.name == "textarea":
                itype = "textarea"
                value = inp.get_text()
            elif inp.name == "button":
                # The default type of <button> is "submit".
                itype = (inp.get("type") or "submit").lower()
                value = inp.get("value") or ""
                label = inp.get_text(" ", strip=True)
            else:
                itype = (inp.get("type") or "text").lower()
                value = inp.get("value")
                if value is None:
                    # A checkbox/radio without a value is sent as "on" by browsers.
                    value = "on" if itype in ("checkbox", "radio") else ""
            fields.append(FormField(
                name=name,
                type=itype,
                value=value,
                id=inp.get("id") or "",
                placeholder=inp.get("placeholder") or "",
                label=label,
                checked=inp.has_attr("checked"),
                disabled=inp.has_attr("disabled"),
            ))
        forms.append(Form(action=action, method=method, fields=fields))
    return forms


def classify_field(f: FormField) -> str:
    """Label a field as 'phone' / 'password' / 'otp' / 'submit' / 'other'."""
    if f.type == "password":
        return "password"
    if f.type in ("hidden",):
        return "other"
    if f.type in BUTTON_TYPES or f.type == "button":
        return "submit"
    if f.type in CHOICE_TYPES or f.type in NEVER_SENT_TYPES:
        # E.g. a <select> named "ddlPhoneCode" must not receive the phone number.
        return "other"
    if f.type == "tel":
        return "phone"
    hay = f.haystack()
    # Order matters: OTP is checked first for cases like "รหัส" without "pass".
    if OTP_KEYS.search(hay):
        return "otp"
    if PASSWORD_KEYS.search(hay):
        return "password"
    if PHONE_KEYS.search(hay):
        return "phone"
    return "other"


def choose_login_form(forms: List[Form]) -> Optional[Form]:
    """
    Pick the most likely login form, i.e. one with phone/password/otp fields.
    If there is no such form, return the form with the most fields.
    """
    scored: List[tuple] = []
    for form in forms:
        score = 0
        for f in form.fields:
            kind = classify_field(f)
            if kind in ("phone", "password", "otp"):
                score += 2
            elif kind == "submit":
                score += 1
        scored.append((score, len(form.fields), form))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


def choose_submit(form: Form, intent: str = "login") -> Optional[FormField]:
    """
    Pick a SINGLE submit button, like a browser does.

    ASP.NET pages often have "Login" and "Request OTP" buttons in the same
    form; sending all of them makes the server run the wrong event.
    intent: "login" (log in/confirm) or "request_otp" (request an SMS code).
    On a tie the first button in the document wins.
    """
    buttons = [f for f in form.fields
               if f.type in BUTTON_TYPES and f.name and not f.disabled]
    if not buttons:
        return None

    def score(b: FormField) -> int:
        hay = b.haystack()
        is_login = bool(LOGIN_BUTTON_KEYS.search(hay))
        is_request = bool(OTP_REQUEST_KEYS.search(hay))
        if intent == "request_otp":
            return (2 if is_request else 1) - (1 if is_login else 0)
        return (2 if is_login else 1) - (1 if is_request else 0)

    return max(buttons, key=score)


def build_payload(form: Form, values: Dict[str, str], intent: str = "login",
                  extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Build the POST/GET data to send:
    hidden fields are kept, `values` are placed into the classified fields.
    `values` keys: phone / password / otp.
    `extra` holds raw values applied last, BY FIELD NAME.
    """
    payload = form.hidden_payload()
    for f in form.fields:
        if f.disabled or f.type == "hidden":
            continue
        if f.type in BUTTON_TYPES or f.type in NEVER_SENT_TYPES:
            continue  # the button is chosen separately below
        if f.type in ("checkbox", "radio"):
            # Like a browser: only checked ones are sent.
            if f.checked:
                payload[f.name] = f.value
            continue
        kind = classify_field(f)
        if kind in values and values[kind]:
            payload[f.name] = values[kind]
        elif f.name not in payload:
            payload[f.name] = f.value

    button = choose_submit(form, intent)
    if button is not None:
        if button.type == "image":
            # <input type="image"> is sent with click coordinates.
            payload[f"{button.name}.x"] = "0"
            payload[f"{button.name}.y"] = "0"
        else:
            payload[button.name] = button.value

    if extra:
        payload.update(extra)
    return payload


def form_action_url(form: Form, base_url: str) -> str:
    """The absolute URL the form is submitted to."""
    return urljoin(base_url, form.action) if form.action else base_url


def submit_form(session, form: Form, values: Dict[str, str], base_url: str,
                timeout: float = 12.0, intent: str = "login",
                extra: Optional[Dict[str, str]] = None):
    """Fill in and submit the form, returning the HTTP response."""
    payload = build_payload(form, values, intent=intent, extra=extra)
    action_url = form_action_url(form, base_url)
    headers = {"Referer": base_url}
    logger.debug("Submitting form: %s (%s) fields=%s",
                 redact(action_url), form.method, list(payload.keys()))
    if form.method == "get":
        # Browser behaviour: for a GET form the query of the action URL is
        # REPLACED by the form data (not appended to).
        action_url = urlunsplit(urlsplit(action_url)._replace(query="", fragment=""))
        return session.get(action_url, params=payload, headers=headers,
                           timeout=timeout, allow_redirects=True)
    return session.post(action_url, data=payload, headers=headers,
                        timeout=timeout, allow_redirects=True)


def form_needs_otp(form: Form) -> bool:
    """Does the form have an OTP field?"""
    return any(classify_field(f) == "otp" for f in form.fields)


def redact(text, *secrets: Optional[str]) -> str:
    """
    Remove URL query strings and known secret values from log/error text.

    requests exceptions include the request URL (with its query); for a form
    submitted via GET that would put the password/OTP into the log file.
    """
    out = _URL_QUERY_RE.sub("?<redacted>", str(text))
    for s in secrets:
        if s and len(s) >= 4:
            for form in {s, quote(s, safe=""), quote_plus(s)}:
                out = out.replace(form, "***")
    return out
