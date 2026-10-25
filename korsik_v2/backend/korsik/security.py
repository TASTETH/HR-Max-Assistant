"""Passwords (scrypt), server-side sessions (httpOnly cookie + CSRF header)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from .config import get_settings

COOKIE = "korsik_session"


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Пароль должен быть не короче 10 символов")
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def new_session(conn, user_id: int) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    conn.execute("DELETE FROM sessions WHERE expires_at < now()")
    conn.execute("INSERT INTO sessions(token_hash, user_id, csrf, expires_at) VALUES (%s,%s,%s,%s)",
                 (sha(token), user_id, csrf,
                  datetime.now(timezone.utc) + timedelta(hours=get_settings().session_hours)))
    return token, csrf


def session_user(conn, token: str | None) -> dict | None:
    if not token:
        return None
    return conn.execute("""SELECT u.*, s.csrf FROM sessions s JOIN users u ON u.id=s.user_id
                           WHERE s.token_hash=%s AND s.expires_at > now() AND u.active""", (sha(token),)).fetchone()


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)  # 32 chars, fits MAX start payload (<=128)
