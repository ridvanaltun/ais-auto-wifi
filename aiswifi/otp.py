"""
Automatically read the SMS OTP code from the macOS Messages database.

How it works:
- The Messages app stores SMS in  ~/Library/Messages/chat.db  (SQLite). If
  "Text Message Forwarding" is enabled on your iPhone, incoming SMS also
  arrive on the Mac and can be read from there.
- Reading this database requires "Full Disk Access" for the app (or
  Terminal). Without it the functions quietly return None; the app can still
  fall back to asking for the OTP.

This is a purely best-effort convenience. If it does not work, the
`login_method = "password"` method needs no OTP at all and is more reliable.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger("aiswifi.otp")

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# Apple date format: nanoseconds since 2001-01-01.
APPLE_EPOCH_OFFSET = 978307200  # 2001-01-01 00:00:00 UTC -> Unix seconds

# Hint words that often appear next to an OTP (English/Thai/Turkish/generic).
OTP_HINT_WORDS = re.compile(
    r"(otp|code|pin|verify|verification|password|รหัส|ยืนยัน|wifi|ais|"
    r"kod|doğrulama|şifre|parola)",
    re.IGNORECASE,
)

# 4–8 digit candidate code. Longer runs (phone/card numbers) and fragments
# joined by separators (2026-09-18, 081-234-5678, 18.09.2026) are rejected.
CODE_RE = re.compile(r"(?<!\d)(?<!\d[/:.,\-])(\d{4,8})(?!\d)(?![/:.,\-]\d)")

# A code right after a hint word: "OTP: 1234", "รหัส OTP คือ 482193".
ANCHORED_CODE_RE = re.compile(
    r"(?:otp|code|pin|password|รหัส|kod|şifre|parola)[^\d\n]{0,25}?"
    r"(?<!\d)(?<!\d[/:.,\-])(\d{4,8})(?!\d)(?![/:.,\-]\d)",
    re.IGNORECASE,
)

# Expected errors when accessing the database (no permission, locks, copy failure...).
_DB_ERRORS = (sqlite3.Error, OSError)


def _db_exists() -> bool:
    """Does chat.db exist? (Returns False instead of raising on permission/TCC errors.)"""
    return os.path.exists(CHAT_DB)


@contextlib.contextmanager
def _open_readonly(db_path: Path) -> Iterator[sqlite3.Connection]:
    """
    Open chat.db read-only; close the connection when done.

    `immutable=1` is NOT used: Messages runs in WAL mode, and an immutable
    connection does not see -wal content that has not been checkpointed yet,
    i.e. the NEWLY arrived SMS. If the database cannot be opened directly, a
    temporary copy is read instead; the copy directory is always deleted
    (even on errors).
    """
    conn: Optional[sqlite3.Connection] = None
    tmp_dir: Optional[str] = None
    try:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            conn.execute("SELECT 1 FROM message LIMIT 1")
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
                conn = None
            logger.debug("Could not open chat.db directly (%s), trying a copy", exc)
            tmp_dir = tempfile.mkdtemp(prefix="aiswifi_msg_")
            tmp_db = os.path.join(tmp_dir, "chat.db")
            shutil.copy2(str(db_path), tmp_db)  # no permission → OSError to the caller
            # The -wal file is copied too; -shm is DELIBERATELY not copied: instead
            # of a possibly inconsistent wal-index from copy time, SQLite rebuilds it from the WAL.
            wal = f"{db_path}-wal"
            if os.path.exists(wal):
                try:
                    shutil.copy2(wal, tmp_db + "-wal")
                except OSError:
                    pass
            conn = sqlite3.connect(tmp_db, timeout=2.0)  # our own copy
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _decode_attributed_body(blob: bytes) -> str:
    """
    On newer macOS versions `text` may be empty and the content stored in
    `attributedBody` (a binary NSArchiver "typedstream"). The message text is
    the UTF-8 string that follows the "NSString" class name, a "+" marker and
    a length prefix. If the format is not recognised, fall back to a crude
    method that keeps printable characters.
    """
    if not blob:
        return ""
    try:
        idx = blob.find(b"NSString")
        if idx != -1:
            rest = blob[idx + len(b"NSString"):]
            plus = rest.find(b"+", 0, 16)
            if plus != -1:
                data = rest[plus + 1:]
                if data[0] == 0x81:      # 2-byte length (little-endian)
                    length, start = int.from_bytes(data[1:3], "little"), 3
                elif data[0] == 0x82:    # 4-byte length
                    length, start = int.from_bytes(data[1:5], "little"), 5
                elif data[0] < 0x80:     # single byte
                    length, start = data[0], 1
                else:
                    length, start = -1, 0
                chunk = data[start:start + length] if length >= 0 else b""
                if length >= 0 and len(chunk) == length:
                    return chunk.decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        text = blob.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # Keep the printable range, turn control characters into spaces.
    return re.sub(r"[^\x20-\x7e฀-๿]+", " ", text)


def _fetch_recent_rows(conn: sqlite3.Connection, since_unix: float,
                       limit: int = 30) -> List[Tuple[int, float, str]]:
    """
    Return (ROWID, unix_ts, text) rows from newest to oldest:
    incoming (is_from_me=0) messages newer than `since_unix`.
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
        logger.debug("Could not query the message table: %s", exc)
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


def _extract_code(text: str, length_hint: Optional[int] = None,
                  require_hint: bool = False) -> Optional[str]:
    """
    Extract the most likely OTP code from a message text.

    With require_hint=True, messages without an OTP hint word (e.g. a friend's
    "at 1930") are rejected; this prevents unrelated SMS that arrive while
    waiting for the login from being mistaken for the code.
    """
    if not text:
        return None
    candidates = CODE_RE.findall(text)
    if not candidates:
        return None
    has_hint = bool(OTP_HINT_WORDS.search(text))
    if require_hint and not has_hint:
        return None
    if length_hint:
        for c in candidates:
            if len(c) == length_hint:
                return c
    # First, a code right next to a hint word, like "OTP: 1234".
    m = ANCHORED_CODE_RE.search(text)
    if m:
        return m.group(1)
    # With a hint, the first candidate; otherwise the longest one.
    if has_hint:
        return candidates[0]
    return max(candidates, key=len)


def read_latest_otp(within_seconds: int = 180,
                    length_hint: Optional[int] = None) -> Optional[str]:
    """
    Return the newest OTP from messages received in the last `within_seconds`.
    None if nothing is found (or there is no permission).
    """
    if not _db_exists():
        logger.debug("chat.db not found: %s", CHAT_DB)
        return None
    since = time.time() - within_seconds
    try:
        with _open_readonly(CHAT_DB) as conn:
            rows = _fetch_recent_rows(conn, since)
    except _DB_ERRORS as exc:
        logger.info("Could not open the Messages database (Full Disk Access may be required): %s", exc)
        return None
    for _rowid, _ts, body in rows:
        code = _extract_code(body, length_hint, require_hint=True)
        if code:
            return code
    return None


def current_baseline() -> Optional[int]:
    """
    Return the largest ROWID so far. `wait_for_new_otp` uses it as a
    reference so that old codes received BEFORE the login started are not
    used by mistake. Must be called BEFORE the request that triggers the SMS.

    Returns None if the database cannot be read (NOT 0: a baseline of 0 would
    make every old OTP in the history look "new").
    """
    if not _db_exists():
        return None
    try:
        with _open_readonly(CHAT_DB) as conn:
            row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except _DB_ERRORS as exc:
        logger.debug("Could not take the Messages reference point: %s", exc)
        return None


def can_read_messages() -> bool:
    """Can the Messages database be read (is Full Disk Access granted)?"""
    return current_baseline() is not None


def wait_for_new_otp(baseline_rowid: int, timeout: int = 90, poll: float = 3.0,
                     length_hint: Optional[int] = None,
                     stop_event=None) -> Optional[str]:
    """
    Wait until an OTP appears in a new message received AFTER `baseline_rowid`.
    If there are several new SMS, the newest one wins. Returns None when the
    time runs out (or `stop_event` is set).
    """
    deadline = time.monotonic() + timeout
    while True:
        if _db_exists():
            try:
                with _open_readonly(CHAT_DB) as conn:
                    rows = conn.execute(
                        """
                        SELECT ROWID, text, attributedBody
                        FROM message
                        WHERE is_from_me = 0 AND ROWID > ?
                        ORDER BY ROWID DESC
                        LIMIT 20
                        """,
                        (baseline_rowid,),
                    ).fetchall()
                for _rowid, text, abody in rows:
                    body = text or (_decode_attributed_body(abody) if abody else "")
                    code = _extract_code(body, length_hint, require_hint=True)
                    if code:
                        return code
            except _DB_ERRORS as exc:
                logger.debug("Could not read messages: %s", exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        pause = min(poll, remaining)
        if stop_event is not None:
            if stop_event.wait(pause):
                return None
        else:
            time.sleep(pause)
