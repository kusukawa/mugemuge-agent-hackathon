"""メッセージの検証と組み立て（PROTOCOL.md 3〜4章）。"""
from __future__ import annotations

import json
import secrets
from functools import lru_cache

import jsonschema

from .gate import Verdict
from .manifest import load_schema

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str = "") -> str:
    return prefix + "".join(secrets.choice(_ULID_ALPHABET) for _ in range(26))


def new_run_id() -> str:
    """デーモンの起動ごとの id（8文字）。job_id・log_id に含めて、再起動をまたいで一意にする。

    /proc の boot_id は Pi を再起動したときしか変わらず、デーモンだけの再起動では同じなので使わない。
    """
    return "".join(secrets.choice(_ULID_ALPHABET) for _ in range(8)).lower()


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(load_schema("protocol.schema.json"))


class MalformedMessage(ValueError):
    pass


def _reject_number(s: str):
    raise MalformedMessage(f"non-integer number is not allowed: {s}")


def parse_message(text: str) -> dict:
    """受信フレームを読む。小数表記（2.0 や 1e3）と NaN/Infinity はここで拒否する。

    JSON Schema では 2.0 も integer とみなされるため、スキーマ検証だけでは浮動小数点を防げない。
    浮動小数点が args に入ると正規化JSON（承認の args_sha256）が一意に決まらなくなる。
    """
    msg = json.loads(text, parse_float=_reject_number, parse_constant=_reject_number)
    if not isinstance(msg, dict):
        raise MalformedMessage("message must be a JSON object")
    errs = message_errors(msg)
    if errs:
        raise MalformedMessage("; ".join(errs))
    return msg


def message_errors(msg: dict) -> list[str]:
    """スキーマ違反の一覧。空ならOK。"""
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
        for e in sorted(_validator().iter_errors(msg), key=lambda e: list(e.absolute_path))
    ]


BLOB_CHUNK = 40_000      # 生のバイト数。base64 で約 53KB（1フレーム 64KiB に収める）


def new_blob_id() -> str:
    return "blob_" + new_id()


def make_blob_messages(blob_id: str, data: bytes, content_type: str, *, ts: int, meta: dict | None = None) -> list[dict]:
    """写真などを 64KiB のフレームに収まるよう分割する（PROTOCOL.md 4.9節）。最後のフレームに sha256 と全体の長さ。"""
    import base64
    import hashlib

    chunks = [data[i:i + BLOB_CHUNK] for i in range(0, len(data), BLOB_CHUNK)] or [b""]
    out = []
    for seq, c in enumerate(chunks):
        m = {"v": 1, "type": "blob", "id": new_id("bl-"), "ts": ts, "blob_id": blob_id, "seq": seq,
             "last": seq == len(chunks) - 1, "content_type": content_type, "data": base64.b64encode(c).decode()}
        if m["last"]:
            m["sha256"] = hashlib.sha256(data).hexdigest()
            m["total_bytes"] = len(data)
            if meta:
                m["meta"] = meta
        out.append(m)
    return out


def make_event(event: str, detail: dict, *, ts: int) -> dict:
    return {"v": 1, "type": "event", "id": new_id("e-"), "ts": ts, "event": event, "detail": detail}


def make_result(
    exec_msg: dict,
    verdict: Verdict,
    manifest_hash: str,
    *,
    ts: int,
    data: dict | None = None,
    io_error: dict | None = None,
    duration_ms: int | None = None,
    msg_id: str | None = None,
) -> dict:
    """gate の判定（と、実行した場合はその結果）から result メッセージを作る。

    io_error は {"code": "io_error" | "fingerprint_mismatch" | ..., "message": ..., "detail": {...}}。
    """
    msg: dict = {"v": 1, "type": "result", "id": msg_id or new_id(), "ts": ts, "re": exec_msg["id"]}
    error = verdict.error() or io_error
    if error is None:
        msg["ok"] = True
        msg["data"] = data if data is not None else {}
    else:
        msg["ok"] = False
        msg["error"] = error
    msg["policy"] = verdict.policy(manifest_hash)
    if duration_ms is not None:
        msg["duration_ms"] = duration_ms
    if "traceparent" in exec_msg:
        msg["traceparent"] = exec_msg["traceparent"]
    return msg
