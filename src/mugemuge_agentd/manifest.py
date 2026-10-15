"""マニフェストの読み込み・検証・既定値の補完。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .canonical import canonical_json, sha256_hex

SCHEMA_DIR = Path(os.environ.get("MUGEMUGE_SCHEMA_DIR", Path(__file__).resolve().parents[2] / "schemas"))

DEFAULTS: dict[str, dict[str, Any]] = {
    "approval": {"max_ttl_s": 300},
    "safety": {"deadman_ms": 10000, "estop_clear": "approval"},
    "limits": {
        "exec_per_s": 20,
        "i2c_read_per_s": 200,
        "i2c_write_per_s": 20,
        "gpio_write_per_s": 5,
        "display_per_s": 2,
        "max_read_len": 32,
        "max_write_len": 16,
        "queue_depth": 8,
    },
    "sample": {
        "min_period_ms": 10,
        "max_duration_ms": 600000,
        "max_reads_per_tick": 8,
        "max_concurrent_jobs": 2,
    },
    "logging": {"audit_path": "/var/lib/mugemuge/audit.jsonl", "audit_max_mb": 20},
    "log": {
        "min_interval_ms": 1000,
        "max_duration_ms": 604_800_000,      # 7日
        "min_photo_interval_ms": 60_000,
        "max_concurrent_logs": 2,
        "spool_mb": 512,                     # spool の上限（写真は 80% で止め、100% で古いバッチから捨てる）
        "photo_max_px": 640,                 # 記録の写真の長辺の上限（camera.capture の上限とは別）
    },
}
CAMERA_DEFAULTS = {"max_width": 1280, "max_height": 960, "min_interval_ms": 2000, "max_per_hour": 120}
BUS_SCAN_DEFAULTS = {"enabled": True, "first": 0x08, "last": 0x77, "exclude": []}
BUS_BOUND_DEFAULTS = {"other_write": "approval", "max_write_allow_regs": 8, "fingerprint_recheck_ms": 1000,
                      "allow_no_recheck": False, "display": "approval"}
ADDR_DEFAULTS = {"protected": True, "write_allow_regs": []}
LINE_DEFAULTS = {"read": "allow", "bias": "none"}


class ManifestError(ValueError):
    pass


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def validate(raw: dict) -> None:
    schema = load_schema("manifest.schema.json")
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors]
        raise ManifestError("manifest is invalid:\n  " + "\n  ".join(lines))
    _semantic_checks(raw)


def _semantic_checks(raw: dict) -> None:
    """スキーマで書きにくい整合性の検査。"""
    buses = [b["bus"] for b in raw["i2c"]["buses"]]
    if len(buses) != len(set(buses)):
        raise ManifestError("i2c.buses: duplicate bus number")
    for b in raw["i2c"]["buses"]:
        addrs = [a["addr"] for a in b.get("addresses", [])]
        if len(addrs) != len(set(addrs)):
            raise ManifestError(f"i2c bus {b['bus']}: duplicate address rule")
        scan = {**BUS_SCAN_DEFAULTS, **b.get("scan", {})}
        if scan["first"] > scan["last"]:
            raise ManifestError(f"i2c bus {b['bus']}: scan.first > scan.last")
    lines = [ln["line"] for ln in raw["gpio"]["lines"]]
    if len(lines) != len(set(lines)):
        raise ManifestError("gpio.lines: duplicate line")
    cams = [c["id"] for c in raw.get("camera", {}).get("devices", [])]
    if len(cams) != len(set(cams)):
        raise ManifestError("camera.devices: duplicate id")
    for c in raw.get("camera", {}).get("devices", []):
        if c["kind"] == "usb" and "device" not in c:
            raise ManifestError(f"camera {c['id']}: kind usb needs device (/dev/videoN)")
    kids = [k["kid"] for k in raw["approval"]["public_keys"]]
    if len(kids) != len(set(kids)):
        raise ManifestError("approval.public_keys: duplicate kid")


def with_defaults(raw: dict) -> dict:
    m = copy.deepcopy(raw)
    for section, defaults in DEFAULTS.items():
        m[section] = {**defaults, **m.get(section, {})}
    m["device"].setdefault("hat", "none")
    for b in m["i2c"]["buses"]:
        b["scan"] = {**BUS_SCAN_DEFAULTS, **b.get("scan", {})}
        b["bound"] = {**BUS_BOUND_DEFAULTS, **b.get("bound", {})}
        b["addresses"] = [{**ADDR_DEFAULTS, **a} for a in b.get("addresses", [])]
    m["gpio"]["lines"] = [{**LINE_DEFAULTS, **ln} for ln in m["gpio"]["lines"]]
    m["camera"] = {"devices": [{**CAMERA_DEFAULTS, **c} for c in m.get("camera", {}).get("devices", [])]}
    return m


def parse_manifest(raw: dict) -> dict:
    """検証して既定値を補った「実効マニフェスト」を返す。hello で送るのもこれ。"""
    validate(raw)
    return with_defaults(raw)


def load_manifest(path: str | Path) -> dict:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a mapping")
    return parse_manifest(raw)


def manifest_sha256(effective: dict) -> str:
    return sha256_hex(canonical_json(effective))
