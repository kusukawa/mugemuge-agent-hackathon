"""Web/API のログインまわり: パスワードの scrypt ハッシュと、HMAC 署名つきのセッション Cookie。

    python3 -m services.web.security hash        # パスワードを入力 → MUGEMUGE_USERS に入れるハッシュを出す
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SESSION_TTL_S = 12 * 3600


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, dk = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, _unb64(dk))


# 存在しない ID でも同じだけ時間をかける（ID の有無を応答時間で漏らさない）
_DUMMY = hash_password(secrets.token_hex(8))


def check_login(users: dict[str, str], uid: str, password: str) -> bool:
    stored = users.get(uid)
    ok = verify_password(password, stored or _DUMMY)
    return ok and stored is not None


class Sessions:
    def __init__(self, secret: bytes):
        if len(secret) < 32:
            raise ValueError("session secret must be at least 32 bytes")
        self.secret = secret

    def _sig(self, body: str) -> str:
        return _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())

    def issue(self, uid: str, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        body = _b64(json.dumps({"uid": uid, "exp": now + SESSION_TTL_S, "csrf": secrets.token_urlsafe(16)},
                               separators=(",", ":")).encode())
        return f"{body}.{self._sig(body)}"

    def read(self, cookie: str | None, now: int | None = None) -> dict | None:
        if not cookie or "." not in cookie:
            return None
        body, sig = cookie.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(body)):
            return None
        try:
            data = json.loads(_unb64(body))
        except ValueError:
            return None
        now = int(time.time()) if now is None else now
        return data if data.get("exp", 0) > now else None


if __name__ == "__main__":
    import getpass
    import sys

    if sys.argv[1:] == ["hash"]:
        pw = getpass.getpass("password: ")
        if pw != getpass.getpass("again: "):
            sys.exit("passwords do not match")
        print(hash_password(pw))
    else:
        sys.exit("usage: python3 -m services.web.security hash")
