"""
SMS OTP kodunu macOS Mesajlar (Messages) veritabanından otomatik okuma.

Nasıl çalışır:
- Mesajlar uygulaması SMS'leri  ~/Library/Messages/chat.db  (SQLite) içinde
  saklar. iPhone'unuzda "Metin Mesajı Yönlendirme" (Text Message Forwarding)
  açıksa, gelen SMS'ler Mac'e de düşer ve buradan okunabilir.
- Bu veritabanını okumak için uygulamaya (veya Terminal'e) "Tam Disk Erişimi"
  (Full Disk Access) izni verilmelidir. İzin yoksa fonksiyonlar sessizce
  None döndürür; uygulama yine de OTP'yi elle sormaya geçebilir.

Bu tamamen "en iyi çaba" (best-effort) bir kolaylıktır. Çalışmazsa
`login_method = "password"` yöntemi hiç OTP gerektirmez ve daha güvenilirdir.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("aiswifi.otp")

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# Apple tarih biçimi: 2001-01-01'den bu yana nanosaniye.
APPLE_EPOCH_OFFSET = 978307200  # 2001-01-01 00:00:00 UTC -> Unix saniye

# OTP'nin yanında sık geçen ipucu kelimeler (İngilizce/Tayca/genel).
OTP_HINT_WORDS = re.compile(
    r"(otp|code|pin|verify|verification|password|รหัส|ยืนยัน|wifi|ais|"
    r"kod|doğrulama|şifre)",
    re.IGNORECASE,
)

# 4–8 haneli aday kod. Uzun kart/numara dizilerini elemek için sınır konur.
CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """
    chat.db'yi salt-okunur aç. Kilitliyse geçici bir kopya üzerinden oku.
    """
    uri = f"file:{db_path}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.execute("SELECT 1 FROM message LIMIT 1")
        return conn
    except sqlite3.Error as exc:
        logger.debug("chat.db doğrudan açılamadı (%s), kopya deneniyor", exc)

    tmp_dir = Path(tempfile.mkdtemp(prefix="aiswifi_msg_"))
    tmp_db = tmp_dir / "chat.db"
    # WAL/SHM dosyalarını da kopyala ki tutarlı okuyalım.
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            try:
                shutil.copy2(src, str(tmp_db) + suffix)
            except OSError:
                pass
    return sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True, timeout=2.0)


def _decode_attributed_body(blob: bytes) -> str:
    """
    Yeni macOS sürümlerinde `text` boş olup içerik `attributedBody` (ikili
    NSKeyedArchiver) alanında olabilir. Tam çözümlemek karmaşık; burada
    yazdırılabilir karakterleri süzerek kaba bir metin çıkarırız (OTP için
    yeterli).
    """
    try:
        text = blob.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # Yazdırılabilir aralığı bırak, kontrol karakterlerini boşluğa çevir.
    return re.sub(r"[^\x20-\x7e\u0e00-\u0e7f]+", " ", text)


def _fetch_recent_rows(conn: sqlite3.Connection, since_unix: float,
                       limit: int = 30) -> List[Tuple[int, float, str]]:
    """
    (ROWID, unix_ts, text) satırlarını en yeniden eskiye döndür.
    `since_unix`ten daha yeni ve bize gelen (is_from_me=0) mesajlar.
    """
    since_apple_ns = int((since_unix - APPLE_EPOCH_OFFSET) * 1_000_000_000)
    rows: List[Tuple[int, float, str]] = []
    try:
        cur = conn.execute(
            """
            SELECT ROWID, date, text, attributedBody
            FROM message
            WHERE is_from_me = 0 AND date > ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (since_apple_ns, limit),
        )
    except sqlite3.Error as exc:
        logger.debug("message tablosu sorgulanamadı: %s", exc)
        return rows

    for rowid, date_ns, text, abody in cur.fetchall():
        body = text or ""
        if not body and abody:
            try:
                body = _decode_attributed_body(abody)
            except Exception:
                body = ""
        unix_ts = (date_ns / 1_000_000_000) + APPLE_EPOCH_OFFSET
        rows.append((int(rowid), float(unix_ts), body))
    return rows


def _extract_code(text: str, length_hint: Optional[int]) -> Optional[str]:
    """Bir mesaj metninden en olası OTP kodunu çıkar."""
    if not text:
        return None
    candidates = CODE_RE.findall(text)
    if not candidates:
        return None
    if length_hint:
        for c in candidates:
            if len(c) == length_hint:
                return c
    # İpucu yoksa: kelime ipuçları varsa ilk adayı, yoksa en uzununu seç.
    if OTP_HINT_WORDS.search(text):
        return candidates[0]
    return max(candidates, key=len)


def read_latest_otp(within_seconds: int = 180,
                    length_hint: Optional[int] = None) -> Optional[str]:
    """
    Son `within_seconds` saniye içinde gelen mesajlardan en yeni OTP'yi getir.
    Bulunamazsa (veya izin yoksa) None.
    """
    if not CHAT_DB.exists():
        logger.debug("chat.db bulunamadı: %s", CHAT_DB)
        return None
    since = time.time() - within_seconds
    try:
        conn = _open_readonly(CHAT_DB)
    except sqlite3.Error as exc:
        logger.info("Mesajlar veritabanı açılamadı (Tam Disk Erişimi gerekli olabilir): %s", exc)
        return None
    try:
        for _rowid, _ts, body in _fetch_recent_rows(conn, since):
            code = _extract_code(body, length_hint)
            if code:
                return code
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return None


def current_baseline() -> int:
    """
    Şu ana kadarki en büyük ROWID'yi döndür. `wait_for_new_otp` bunu referans
    alır; böylece giriş başlamadan ÖNCE gelmiş eski kodları yanlışlıkla
    kullanmayız. Okunamazsa 0.
    """
    if not CHAT_DB.exists():
        return 0
    try:
        conn = _open_readonly(CHAT_DB)
        try:
            row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def wait_for_new_otp(baseline_rowid: int, timeout: int = 90, poll: float = 3.0,
                     length_hint: Optional[int] = None) -> Optional[str]:
    """
    `baseline_rowid`ten SONRA gelen yeni bir mesajda OTP belirene kadar bekle.
    Süre dolarsa None döner.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if CHAT_DB.exists():
            try:
                conn = _open_readonly(CHAT_DB)
                try:
                    cur = conn.execute(
                        """
                        SELECT ROWID, text, attributedBody
                        FROM message
                        WHERE is_from_me = 0 AND ROWID > ?
                        ORDER BY ROWID DESC
                        LIMIT 20
                        """,
                        (baseline_rowid,),
                    )
                    for _rowid, text, abody in cur.fetchall():
                        body = text or (_decode_attributed_body(abody) if abody else "")
                        code = _extract_code(body, length_hint)
                        if code:
                            return code
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
        time.sleep(poll)
    return None
