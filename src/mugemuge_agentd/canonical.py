"""正規化JSONとハッシュ（PROTOCOL.md 7.2節）。

署名・ハッシュの対象には浮動小数点を含めない。含まれていたら TypeError にする。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _reject_floats(obj: Any, path: str = "$") -> None:
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return
    if isinstance(obj, float):
        raise TypeError(f"float is not allowed in canonical JSON: {path}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"non-string key at {path}: {k!r}")
            _reject_floats(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_floats(v, f"{path}[{i}]")
        return
    raise TypeError(f"unsupported type at {path}: {type(obj).__name__}")


def canonical_json(obj: Any) -> bytes:
    _reject_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def args_sha256(op: str, args: dict) -> str:
    return sha256_hex(canonical_json({"op": op, "args": args}))
