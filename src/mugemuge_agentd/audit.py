"""監査ログ（PROTOCOL.md 9章）。

exec 1件ごとに JSON を1行追記する。各行の prev_sha256 は直前の行（改行を除く）の sha256 で、
行の削除や書き換えがあれば鎖が切れる。audit_max_mb を超えたら <path>.1 へ回し（古いものは .2, .3 …）、
新しいファイルの1行目の prev_sha256 には旧ファイル最終行のハッシュを書く。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

GENESIS = "0" * 64
KEEP_ROTATED = 3


def _line_hash(line: bytes) -> str:
    return hashlib.sha256(line.rstrip(b"\n")).hexdigest()


def _last_line(path: Path) -> bytes | None:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            back = min(size, 65536)
            f.seek(size - back)
            tail = f.read().rstrip(b"\n")
    except FileNotFoundError:
        return None
    return tail.rsplit(b"\n", 1)[-1] if tail else None


class AuditLog:
    def __init__(self, path: str | Path, max_mb: int = 20):
        self.path = Path(path)
        self.max_bytes = max_mb * 1024 * 1024
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq, self.prev = 0, GENESIS
        last = _last_line(self.path) or _last_line(self.path.with_name(self.path.name + ".1"))
        if last:
            self.prev = _line_hash(last)
            try:
                self.seq = int(json.loads(last)["seq"]) + 1
            except (ValueError, KeyError, TypeError):
                log.warning("audit: cannot read seq from last line of %s; continuing from 0", self.path)

    def append(self, record: dict) -> dict:
        with self._lock:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                self._rotate()
            entry = {"seq": self.seq, **record, "prev_sha256": self.prev}
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            with self.path.open("ab") as f:
                f.write(line + b"\n")
            self.prev = _line_hash(line)
            self.seq += 1
            return entry

    def _rotate(self) -> None:
        for i in range(KEEP_ROTATED, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if i == KEEP_ROTATED:
                src.unlink(missing_ok=True)
            elif src.exists():
                src.rename(self.path.with_name(f"{self.path.name}.{i + 1}"))
        self.path.rename(self.path.with_name(self.path.name + ".1"))


def verify_chain(paths: list[Path]) -> int:
    """古い順に並べたファイル群の鎖を検査し、行数を返す。切れていれば ValueError。"""
    prev, n = None, 0
    for p in paths:
        for raw in p.read_bytes().splitlines():
            entry = json.loads(raw)
            if prev is not None and entry["prev_sha256"] != prev:
                raise ValueError(f"chain broken at {p.name} seq={entry.get('seq')}")
            prev = _line_hash(raw)
            n += 1
    return n


def exec_record(*, ts: int, session_id: str | None, exec_msg: dict, args_sha: str | None, result: dict) -> dict:
    """exec と result から監査ログの1行（seq と prev_sha256 以外）を作る。"""
    pol = result.get("policy", {})
    err = result.get("error")
    grant = exec_msg.get("approval") or {}
    return {
        "ts": ts,
        "session_id": session_id,
        "exec_id": exec_msg.get("id"),
        "run_id": exec_msg.get("run_id"),
        "step_id": exec_msg.get("step_id"),
        "traceparent": exec_msg.get("traceparent"),
        "op": exec_msg.get("op"),
        "args_sha256": args_sha,
        "decision": pol.get("decision"),
        "rule": pol.get("rule"),
        "ok": result.get("ok"),
        "error": err["code"] if err else None,
        "grant_id": grant.get("grant_id") if pol.get("decision") == "approved" else None,
    }
