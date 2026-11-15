"""Admin password hashing and signed session cookies."""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import time
from pathlib import Path


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        kind, salt_hex, dig_hex = encoded.split("$", 2)
    except ValueError:
        return False
    if kind != "scrypt" or not salt_hex or not dig_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expect = bytes.fromhex(dig_hex)
        got = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    except ValueError:
        return False
    return hmac.compare_digest(got, expect)


def sign_cookie(secret: bytes, value: str, *, now: int | None = None) -> str:
    ts = str(int(now if now is not None else time.time()))
    body = f"{value}.{ts}"
    mac = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def cookie_value_ok(value: str) -> bool:
    return value == "ok"


def verify_cookie(secret: bytes, token: str, *, max_age_s: int = 86400 * 14, now: int | None = None) -> str | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    value, ts, mac = parts
    body = f"{value}.{ts}"
    expect = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, mac):
        return None
    try:
        issued = int(ts)
    except ValueError:
        return None
    current = int(now if now is not None else time.time())
    if current < issued or current - issued > max_age_s:
        return None
    return value if cookie_value_ok(value) else None


def allow_password_bypass(data_dir: Path, *, flag: str | None = None, tmp_root: Path | None = None) -> bool:
    """ROGMEM_NO_PASSWORD is a test hatch and only applies under the system temp dir."""
    if flag is None:
        flag = os.environ.get("ROGMEM_NO_PASSWORD")
    if flag != "1":
        return False
    root = Path(tempfile.gettempdir() if tmp_root is None else tmp_root).resolve()
    try:
        Path(data_dir).resolve().relative_to(root)
    except ValueError:
        return False
    return True
