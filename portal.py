"""
Genel captive-portal giriş motoru.

AIS portalı ASP.NET tabanlıdır; formlarında `__VIEWSTATE`,
`__EVENTVALIDATION` gibi gizli alanlar bulunur. Bu motor:
  1) Giriş sayfasını indirir,
  2) İçindeki formları ve alanları ayrıştırır,
  3) Görünür alanları anahtar kelimelere göre telefon / şifre / OTP / gönder
     olarak sınıflandırır,
  4) Gizli alanları (viewstate vb.) OLDUĞU GİBİ koruyarak formu doldurup
     gönderir.

Alan adları değişse bile çalışması için isimlere değil, kalıplara dayanır;
bu sayede başka sağlayıcılar için de yeniden kullanılabilir.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger("aiswifi.portal")

# Alan sınıflandırma için anahtar kelime kümeleri (İngilizce + Tayca + genel).
PHONE_KEYS = re.compile(
    r"(msisdn|mobile|phone|tel|number|user(name)?|เบอร์|หมายเลข|โทร)", re.IGNORECASE
)
PASSWORD_KEYS = re.compile(r"(pass|pwd|รหัสผ่าน)", re.IGNORECASE)
OTP_KEYS = re.compile(r"(otp|code|pin|verif|รหัส(?!ผ่าน)|ยืนยัน)", re.IGNORECASE)
SUBMIT_KEYS = re.compile(
    r"(login|submit|signin|sign-in|connect|ok|ตกลง|เข้าสู่ระบบ|เชื่อมต่อ|ยืนยัน|"
    r"request|send|ขอ)",
    re.IGNORECASE,
)

# Giriş başarısını gösteren kaba işaretler (yedek kontrol için).
SUCCESS_MARKERS = re.compile(
    r"(success|welcome|connected|online|เชื่อมต่อสำเร็จ|สำเร็จ|logout|ออกจากระบบ)",
    re.IGNORECASE,
)


@dataclass
class FormField:
    name: str
    type: str = "text"
    value: str = ""
    id: str = ""
    placeholder: str = ""

    def haystack(self) -> str:
        """Sınıflandırmada kullanılacak birleşik metin."""
        return " ".join([self.name, self.id, self.placeholder]).lower()


@dataclass
class Form:
    action: str
    method: str = "post"
    fields: List[FormField] = field(default_factory=list)

    def hidden_payload(self) -> Dict[str, str]:
        """Gizli/otomatik alanların ad→değer eşlemesi (viewstate vb.)."""
        payload: Dict[str, str] = {}
        for f in self.fields:
            if f.type in ("hidden", "submit", "button", "image"):
                if f.name:
                    payload[f.name] = f.value
        return payload


@dataclass
class LoginContext:
    """Bir giriş denemesi için gereken bilgiler."""
    phone: Optional[str] = None
    password: Optional[str] = None
    # OTP gerektiğinde çağrılan fonksiyon; kodu (str) veya None döndürür.
    otp_provider: Optional[Callable[[], Optional[str]]] = None
    extra_fields: Dict[str, str] = field(default_factory=dict)


def parse_forms(html: str) -> List[Form]:
    """HTML içindeki tüm formları ayrıştır."""
    soup = BeautifulSoup(html or "", "html.parser")
    forms: List[Form] = []
    for f in soup.find_all("form"):
        action = f.get("action") or ""
        method = (f.get("method") or "post").lower()
        fields: List[FormField] = []
        for inp in f.find_all(["input", "select", "textarea"]):
            name = inp.get("name") or ""
            if not name:
                continue
            itype = (inp.get("type") or ("select" if inp.name == "select" else "text")).lower()
            value = inp.get("value") or ""
            fields.append(FormField(
                name=name,
                type=itype,
                value=value,
                id=inp.get("id") or "",
                placeholder=inp.get("placeholder") or "",
            ))
        forms.append(Form(action=action, method=method, fields=fields))
    return forms


def classify_field(f: FormField) -> str:
    """Bir alanı 'phone' / 'password' / 'otp' / 'submit' / 'other' olarak etiketle."""
    if f.type == "password":
        return "password"
    if f.type in ("hidden",):
        return "other"
    if f.type in ("submit", "button", "image"):
        return "submit"
    if f.type == "tel":
        return "phone"
    hay = f.haystack()
    # Sıra önemli: OTP, "pass" içermeyen "รหัส" gibi durumlar için önce bakılır.
    if OTP_KEYS.search(hay):
        return "otp"
    if PASSWORD_KEYS.search(hay):
        return "password"
    if PHONE_KEYS.search(hay):
        return "phone"
    return "other"


def choose_login_form(forms: List[Form]) -> Optional[Form]:
    """
    İçinde telefon/şifre/otp alanı olan en olası giriş formunu seç.
    Böyle bir form yoksa, alanı en çok olan formu döndür.
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
    best = scored[0]
    return best[2] if best[0] > 0 else scored[0][2]


def build_payload(form: Form, values: Dict[str, str]) -> Dict[str, str]:
    """
    Gönderilecek POST/GET verisini oluştur:
    gizli alanlar korunur, sınıflandırılan alanlara `values` yerleştirilir.
    `values` anahtarları: phone / password / otp.
    """
    payload = form.hidden_payload()
    for f in form.fields:
        if f.type in ("hidden", "submit", "button", "image"):
            continue
        kind = classify_field(f)
        if kind in values and values[kind]:
            payload[f.name] = values[kind]
        elif f.name not in payload:
            payload[f.name] = f.value
    return payload


def submit_form(session, form: Form, values: Dict[str, str], base_url: str,
                timeout: float = 12.0):
    """Formu doldurup gönder ve HTTP yanıtını döndür."""
    payload = build_payload(form, values)
    action_url = urljoin(base_url, form.action) if form.action else base_url
    logger.debug("Form gönderiliyor: %s (%s) alanlar=%s",
                 action_url, form.method, list(payload.keys()))
    if form.method == "get":
        return session.get(action_url, params=payload, timeout=timeout, allow_redirects=True)
    return session.post(action_url, data=payload, timeout=timeout, allow_redirects=True)


def form_needs_otp(form: Form) -> bool:
    """Formda OTP alanı var mı?"""
    return any(classify_field(f) == "otp" for f in form.fields)


def looks_successful(html: str) -> bool:
    """Yanıt içeriği giriş başarısına işaret ediyor mu (kaba kontrol)?"""
    return bool(SUCCESS_MARKERS.search(html or ""))
