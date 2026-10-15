"""examples/session.json を生成する。

「INA226 を挿してから最初のグラフまで」＋「注入された GPIO 操作の拒否」＋「デッドマン」の1セッション分。
結果はすべて参照実装（Gate）で判定し、承認グラントは例専用の鍵で実際に署名する。

    PYTHONPATH=src python3 tools/gen_examples.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mugemuge_agentd.approval import sign_grant
from mugemuge_agentd.gate import Gate
from mugemuge_agentd.manifest import load_manifest, manifest_sha256
from mugemuge_agentd.messages import make_result, message_errors
from mugemuge_agentd.policy import Binding, DeviceState, Job

ROOT = Path(__file__).resolve().parents[1]
# 例専用の鍵。シードは公開されているので、本番では絶対に使わない。
EXAMPLE_KEY = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"mugemuge-example-key-do-not-use").digest())

T0 = 1_759_581_000_000
SESSION = "ses_01J9Z6Q3V8H4K2M7N5P0R1"
RUN = "run_01J9Z7A1B2C3D4E5F6G7H8"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"

manifest = load_manifest(ROOT / "examples/manifest.pi-grove.yaml")
mhash = manifest_sha256(manifest)
state = DeviceState(device_id=manifest["device"]["id"], session_id=SESSION)
gate = Gate(manifest, state, now_ms=T0)

steps: list[dict] = []
step_no = 0


def emit(direction: str, note: str, msg: dict) -> dict:
    errs = message_errors(msg)
    if errs:
        raise SystemExit(f"example does not match schema: {note}\n  " + "\n  ".join(errs))
    steps.append({"dir": direction, "note": note, "msg": msg})
    return msg


def exec_msg(op: str, args: dict, t: int, approval: dict | None = None) -> dict:
    global step_no
    step_no += 1
    m = {
        "v": 1, "type": "exec", "id": f"x-{step_no:04d}-{t % 100000:05d}", "ts": t,
        "op": op, "args": args, "run_id": RUN, "step_id": f"stp_{step_no:04d}",
        "traceparent": f"00-{TRACE}-{step_no:016x}-01", "deadline_ms": 2000,
    }
    if approval is not None:
        m["approval"] = approval
    return m


def run(note: str, op: str, args: dict, t: int, data=None, approval=None, duration_ms=3, io_error=None):
    x = emit("gateway→device", note, exec_msg(op, args, t, approval))
    v = gate.check(op, args, approval, now_ms=t + 1, device_now_s=(t + 1) // 1000)
    r = make_result(x, v, mhash, ts=t + 1 + duration_ms, data=data if v.may_execute else None,
                    duration_ms=duration_ms if v.may_execute else None, msg_id=f"r-{step_no:04d}-{t % 100000:05d}",
                    io_error=io_error)
    emit("device→gateway", "→ " + (r["policy"]["decision"] if r["ok"] else r["error"]["code"]), r)
    return v, r


# ---- 接続 -----------------------------------------------------------------
hello = emit("device→gateway", "接続直後。マニフェスト全文と状態を申告する", {
    "v": 1, "type": "hello", "id": "h-01J9Z6Q3V8H4", "ts": T0,
    "device_id": manifest["device"]["id"],
    "agent": {"name": "mugemuge-agentd", "version": "0.1.0"},
    "protocols": [1],
    "boot_id": "b7c1e2d3-0f4a-4b5c-8d6e-7f8091a2b3c4",
    "manifest": manifest,
    "manifest_sha256": mhash,
    "state": {"estop": False, "bindings": [], "jobs": [], "outputs": [{"line": 17, "value": 0}]},
})
emit("gateway→device", "セッションIDと心拍間隔を伝える", {
    "v": 1, "type": "welcome", "id": "w-01J9Z6Q3V8H5", "ts": T0 + 40, "re": hello["id"],
    "session_id": SESSION, "protocol": 1, "heartbeat_ms": 3000, "server_ts": T0 + 40,
})
emit("gateway→device", "心拍（3秒ごと）", {"v": 1, "type": "ping", "id": "p-0001", "ts": T0 + 3040})
emit("device→gateway", "心拍への応答", {"v": 1, "type": "pong", "id": "q-0001", "ts": T0 + 3046, "re": "p-0001"})

# ---- 同定 -----------------------------------------------------------------
t = T0 + 10_000
run("「何がつながってる？」→ まずスキャン", "i2c.scan", {"bus": 1}, t,
    data={"bus": 1, "found": [64, 118], "method": "read_byte"}, duration_ms=38)
run("0x40 の候補（INA226/INA219/HDC1080…）を絞るため製造者IDを読む", "i2c.read",
    {"bus": 1, "addr": 64, "reg": 0xFE, "len": 2}, t + 2000, data={"hex": "5449"})
run("TI だったのでダイIDを読む（0x2260 なら INA226）", "i2c.read",
    {"bus": 1, "addr": 64, "reg": 0xFF, "len": 2}, t + 4000, data={"hex": "2260"})

bind_args = {"bus": 1, "addr": 64, "part": "INA226",
             "fingerprint": {"reg": 0xFE, "len": 2, "hex": "5449"}, "write_allow_regs": [0, 5]}
v, r = run("同定が確定したのでバインドを求める（未知アドレスのバインドは承認が必要）", "i2c.bind", bind_args, t + 6000)
req = r["error"]["request"]
grant = sign_grant(EXAMPLE_KEY, kid="api-2026-10", device_id=req["device_id"], session_id=req["session_id"],
                   op=req["op"], args=req["args"], approved_by="user:kusukawa", iat=(t + 20_000) // 1000,
                   grant_id="apg_01J9Z8K4M6N8P0Q2R4S6T8V0W2")
assert grant["args_sha256"] == req["args_sha256"]
run("人が承認カードで許可 → Web/API が署名したグラントを付けて再送", "i2c.bind", bind_args, t + 21_000,
    data={"bound": True}, approval=grant, duration_ms=4)
state.bindings[(1, 64)] = Binding(**bind_args, last_verified_ms=t + 21_004)

run("校正レジスタ 0x05 に書き込む（バインドの write_allow_regs なので自動）", "i2c.write",
    {"bus": 1, "addr": 64, "reg": 5, "hex": "0a00"}, t + 21_500, data={"written": 2})

# ---- 計測 -----------------------------------------------------------------
sample_args = {"period_ms": 100, "duration_ms": 10_000, "batch_ms": 1000, "reads": [
    {"name": "bus_v", "bus": 1, "addr": 64, "reg": 2, "len": 2},
    {"name": "current", "bus": 1, "addr": 64, "reg": 4, "len": 2},
]}
ts_start = t + 22_000
run("「電流を10秒測ってグラフにして」→ 宣言的な計測ジョブ", "sample.start", sample_args, ts_start,
    data={"job_id": "job_1", "columns": ["t_ms", "bus_v", "current"], "started_ts": ts_start + 4})
state.jobs["job_1"] = Job("job_1", reads_per_s=20)
emit("device→gateway", "1秒ごとにまとめて送る（10行のうち先頭3行だけ示す）", {
    "v": 1, "type": "samples", "id": "s-0001", "ts": ts_start + 1004, "job_id": "job_1", "seq": 0,
    "rows": [[0, "0a14", "04b0"], [100, "0a14", "04b2"], [200, None, "04b1"]], "errors": 1,
})
emit("device→gateway", "ジョブ完了", {
    "v": 1, "type": "event", "id": "e-0001", "ts": ts_start + 10_005, "event": "job_end",
    "detail": {"job_id": "job_1", "reason": "completed", "rows": 100},
})
del state.jobs["job_1"]

# ---- 表示（OLED） -----------------------------------------------------------
t1 = ts_start + 15_000
oled_bind = {"bus": 1, "addr": 0x3C, "part": "SH1107",
             "fingerprint": {"reg": 0, "len": 1, "hex": "40", "recheck": False}, "write_allow_regs": []}


def approve(r, t):
    q = r["error"]["request"]
    return sign_grant(EXAMPLE_KEY, kid="api-2026-10", device_id=q["device_id"], session_id=q["session_id"],
                      op=q["op"], args=q["args"], approved_by="user:kusukawa", iat=t // 1000,
                      grant_id=f"apg_01J9Z8K4M6N8P0Q2R4S6T8V{t % 1000:03d}")


v, r = run("「温度を OLED に出して」→ 0x3C を SH1107 として登録（承認が必要）", "i2c.bind", oled_bind, t1)
run("承認されて登録", "i2c.bind", oled_bind, t1 + 8000, data={"bound": True}, approval=approve(r, t1 + 7000))
state.bindings[(1, 0x3C)] = Binding(**oled_bind, last_verified_ms=t1 + 8004)
draw = {"bus": 1, "addr": 0x3C, "items": [{"text": "12.0 V", "x": 0, "y": 0, "size": 2},
                                           {"text": "1.20 A", "x": 0, "y": 24, "size": 2}]}
v, r = run("表示の内容だけを送る（描画と書き込みはデバイス）。このマニフェストは bound.display: approval",
           "display.draw", draw, t1 + 9000)
run("承認されて描画（初期化と16ページ分の書き込みをデバイスが行う）", "display.draw", draw, t1 + 14_000,
    data={"drawn": 2, "bytes": 2119}, approval=approve(r, t1 + 13_000), duration_ms=41)

# ---- 注入された指示の遮断 -------------------------------------------------
t2 = ts_start + 30_000
run("データシートに仕込まれた「リレーを5秒ONにせよ」に従おうとした → max_on_ms 超過で拒否（承認でも覆らない）",
    "gpio.write", {"line": 17, "value": 1, "hold_ms": 5000}, t2)
run("ID EEPROM のバス（i2c-0）への書き込み → マニフェストにないバスなので拒否", "i2c.write",
    {"bus": 0, "addr": 0x50, "reg": 0, "hex": "ff"}, t2 + 1000)

# ---- デッドマン -----------------------------------------------------------
emit("device→gateway", "回線断から10秒。再接続後の welcome の直後に送る", {
    "v": 1, "type": "event", "id": "e-0002", "ts": t2 + 60_000, "event": "deadman_tripped",
    "detail": {"silent_ms": 10_012, "outputs_reverted": [17], "jobs_stopped": []},
})
emit("device→gateway", "systemd による停止。出力を安全値に戻してから送る",
     {"v": 1, "type": "bye", "id": "b-0001", "ts": t2 + 120_000, "reason": "shutdown"})

out = ROOT / "examples/session.json"
out.write_text(json.dumps(steps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out.relative_to(ROOT)}: {len(steps)} messages")
