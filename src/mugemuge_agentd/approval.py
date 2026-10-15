"""承認グラントの署名と検証（PROTOCOL.md 7章）。

sign_grant は Web/API サービス側、verify_grant はデバイス側で使う。
Agent サービスは署名鍵を持たないので sign_grant を呼べない。
"""
from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import args_sha256, canonical_json

CLOCK_SKEW_S = 30
GRANT_FIELDS = ("grant_id", "kid", "device_id", "session_id", "op", "args_sha256", "approved_by", "iat", "exp")


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def public_key_from_b64(s: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(b64url_decode(s))


def public_key_to_b64(key: Ed25519PublicKey) -> str:
    return b64url_encode(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def new_grant_id() -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "apg_" + "".join(secrets.choice(alphabet) for _ in range(26))


def signing_payload(grant: dict) -> bytes:
    return canonical_json({k: grant[k] for k in GRANT_FIELDS})


def sign_grant(
    private_key: Ed25519PrivateKey,
    *,
    kid: str,
    device_id: str,
    session_id: str,
    op: str,
    args: dict,
    approved_by: str,
    iat: int,
    ttl_s: int = 300,
    grant_id: str | None = None,
) -> dict:
    grant = {
        "grant_id": grant_id or new_grant_id(),
        "kid": kid,
        "device_id": device_id,
        "session_id": session_id,
        "op": op,
        "args_sha256": args_sha256(op, args),
        "approved_by": approved_by,
        "iat": iat,
        "exp": iat + ttl_s,
    }
    grant["sig"] = b64url_encode(private_key.sign(signing_payload(grant)))
    return grant


class ApprovalInvalid(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class GrantLedger:
    """使用済みグラントID を exp まで覚えておく（再利用の防止）。"""

    used: dict[str, int] = field(default_factory=dict)

    def is_used(self, grant_id: str) -> bool:
        return grant_id in self.used

    def mark(self, grant_id: str, exp: int) -> None:
        self.used[grant_id] = exp

    def prune(self, now_s: int) -> None:
        for gid in [g for g, exp in self.used.items() if exp + CLOCK_SKEW_S < now_s]:
            del self.used[gid]


def verify_grant(
    grant: dict,
    *,
    public_keys: dict[str, Ed25519PublicKey],
    device_id: str,
    session_id: str,
    op: str,
    args: dict,
    now_s: int,
    max_ttl_s: int,
    ledger: GrantLedger,
) -> None:
    """検証に失敗したら ApprovalInvalid(reason)。成功しても ledger には記録しない（呼び出し側が実行直前に mark する）。"""
    missing = [k for k in (*GRANT_FIELDS, "sig") if k not in grant]
    if missing:
        raise ApprovalInvalid("malformed")
    key = public_keys.get(grant["kid"])
    if key is None:
        raise ApprovalInvalid("unknown_kid")
    try:
        key.verify(b64url_decode(grant["sig"]), signing_payload(grant))
    except (InvalidSignature, ValueError):
        raise ApprovalInvalid("bad_signature") from None
    if grant["device_id"] != device_id:
        raise ApprovalInvalid("wrong_device")
    if grant["session_id"] != session_id:
        raise ApprovalInvalid("wrong_session")
    if grant["op"] != op or grant["args_sha256"] != args_sha256(op, args):
        raise ApprovalInvalid("args_mismatch")
    if grant["exp"] - grant["iat"] > max_ttl_s:
        raise ApprovalInvalid("ttl_too_long")
    if now_s + CLOCK_SKEW_S < grant["iat"]:
        raise ApprovalInvalid("not_yet_valid")
    if now_s - CLOCK_SKEW_S > grant["exp"]:
        raise ApprovalInvalid("expired")
    if ledger.is_used(grant["grant_id"]):
        raise ApprovalInvalid("replayed")
