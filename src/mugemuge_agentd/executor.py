"""exec の実行部（PROTOCOL.md 4.4節、6.1節の手順6〜7）。

    result = await executor.handle(exec_msg, received_mono_ms)

判定はすべて gate.check() に任せ、may_execute のときだけ I/O を行う。ここに判定ロジックを書かない。
ここで扱うのは、判定の前後にある実行上の仕組みだけ:

- exec.id による二重実行の防止（直近64件・60秒。同じ id には前回の結果を duplicate: true で返す）
- バスごとの直列実行（レーン）と、queue_depth を超えたときの busy、deadline_ms を過ぎたときの timeout
- fingerprint_check がある書き込みの前の指紋の読み直しと、不一致時のバインド解除（binding_dropped）
- i2c.bind の実行時の指紋の読み出し
- 監査ログへの記録
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import socket
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from .audit import AuditLog, exec_record
from .canonical import args_sha256
from .clock import Clock
from .gate import Gate, Verdict
from .hw.gpio import GPIO, GPIOError
from .hw.i2c import I2CBus, I2CError
from .hw.camera import CameraError
from .hw import display
from .messages import make_blob_messages, make_event, make_result, new_blob_id, new_id
from .policy import BadRequest, Binding, DeviceState, Level
from .safety import Safety
from .sampler import Sampler

log = logging.getLogger(__name__)

DEDUP_MAX = 64
DEDUP_TTL_MS = 60_000
DEFAULT_DEADLINE_MS = 2000

# 待ち行列を通さずすぐ実行する op（I/O がないか、安全側で詰まらせてはいけないもの）
IMMEDIATE_OPS = {"sys.info", "sys.estop", "sys.estop_clear", "i2c.unbind", "sample.start", "sample.stop",
                 "log.start", "log.stop"}


class OpFailed(Exception):
    """ポリシーは通ったが実行で失敗した。result の error になる。"""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.error = {"code": code, "message": message[:512]}
        if detail:
            self.error["detail"] = detail


@dataclass
class Lane:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: int = 0


def _fp_reg_width(fp: dict) -> int:
    """指紋のレジスタの幅。バインドに書いてあればそれ、なければ reg が 0xFF を超えるかで決める（B7）。"""
    return fp.get("reg_width") or (16 if fp["reg"] > 0xFF else 8)


class Executor:
    def __init__(
        self,
        manifest: dict,
        state: DeviceState,
        gate: Gate,
        i2c: I2CBus,
        gpio: GPIO,
        safety: Safety,
        sampler: Sampler,
        audit: AuditLog | None,
        clock: Clock,
        emit: Callable[[dict], None],
        agent_version: str,
        cameras: dict | None = None,
        logger=None,
    ):
        self.manifest = manifest
        self.state = state
        self.gate = gate
        self.i2c = i2c
        self.gpio = gpio
        self.safety = safety
        self.sampler = sampler
        self.audit = audit
        self.clock = clock
        self.emit = emit
        self.agent_version = agent_version
        self.cameras = cameras or {}
        self.logger = logger
        if logger is not None:
            logger.lock_for = self.bus_lock
        self.queue_depth = manifest["limits"]["queue_depth"]
        self.buses = {b["bus"]: b for b in manifest["i2c"]["buses"]}
        self.lanes: dict[tuple, Lane] = {}
        self._screens: dict[tuple[int, int], display.Framebuffer] = {}   # clear: false で重ねて描くための前回の画面
        self._recent: OrderedDict[str, tuple[int, asyncio.Future]] = OrderedDict()
        self._started_mono = time.monotonic()
        sampler.lock_for = self.bus_lock

    # ---- レーン ------------------------------------------------------------------

    def _lane(self, key: tuple) -> Lane:
        lane = self.lanes.get(key)
        if lane is None:
            lane = self.lanes[key] = Lane()
        return lane

    def bus_lock(self, bus: int) -> asyncio.Lock:
        """I2C バスの直列化。計測ジョブの読み出しもこのロックを取る。"""
        return self._lane(("i2c", bus)).lock

    def _lane_key(self, op: str, args: dict) -> tuple | None:
        if op in IMMEDIATE_OPS:
            return None
        if op.startswith("i2c.") or op == "display.draw":
            return ("i2c", args["bus"])
        if op == "camera.capture":
            return ("camera", args["camera"])
        if op == "gpio.write":
            ln = self.safety.lines.get(args["line"])
            if ln is not None and args["value"] == ln["safe_value"]:
                return None      # 安全値への書き込みは待たせない
        return ("gpio",)

    # ---- 入口 --------------------------------------------------------------------

    async def handle(self, exec_msg: dict, received_mono_ms: int | None = None) -> dict:
        received = self.clock.mono_ms() if received_mono_ms is None else received_mono_ms
        xid = exec_msg["id"]
        self._prune_recent()
        hit = self._recent.get(xid)
        if hit is not None:
            prev = await asyncio.shield(hit[1])
            log.info("duplicate exec %s (%s); returning previous result", xid, exec_msg["op"])
            return {**prev, "id": new_id("r-"), "ts": self.clock.now_ms(), "duplicate": True}

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._recent[xid] = (self.clock.mono_ms() + DEDUP_TTL_MS, fut)
        while len(self._recent) > DEDUP_MAX:
            self._recent.popitem(last=False)
        try:
            result = await self._run(exec_msg, received)
        except Exception as e:   # 想定外の例外でも result は必ず返す
            log.exception("internal error while handling %s", xid)
            result = self._reject(exec_msg, "internal", f"internal error: {type(e).__name__}")
        fut.set_result(result)
        return result

    def _prune_recent(self) -> None:
        now = self.clock.mono_ms()
        for xid in [k for k, (exp, _) in self._recent.items() if exp < now]:
            del self._recent[xid]

    def reject_malformed(self, exec_like: dict, code: str, message: str) -> dict:
        """スキーマに合わない exec への応答（client から呼ぶ）。"""
        return self._reject(exec_like, code, message)

    def _reject(self, exec_msg: dict, code: str, message: str, decision: str = "deny", rule: str | None = None,
                retry_after_ms: int | None = None) -> dict:
        v = Verdict(decision, rule or str(exec_msg.get("op") or "exec"), code, message[:512],
                    retry_after_ms=retry_after_ms)
        result = make_result(exec_msg, v, self.gate.manifest_hash, ts=self.clock.now_ms(), msg_id=new_id("r-"))
        self._audit(exec_msg, result)
        return result

    async def _run(self, exec_msg: dict, received: int) -> dict:
        op, args = exec_msg["op"], exec_msg["args"]
        key = self._lane_key(op, args)
        if key is None:
            return await self._check_and_execute(exec_msg)

        # 拒否されるものは待たせない（判定は gate に任せ、ここでは待ち行列に入れるかだけを決める）
        try:
            static = self.gate.policy.evaluate(op, args, self.state, self.clock.mono_ms())
        except BadRequest:
            static = None
        if static is None or static.level is Level.DENY:
            return await self._check_and_execute(exec_msg)

        pending_decision = "allow" if static.level is Level.ALLOW else "approval_required"
        lane = self._lane(key)
        if lane.pending >= self.queue_depth:
            return self._reject(exec_msg, "busy", f"queue for {key[0]} is full ({self.queue_depth})",
                                decision=pending_decision, rule=static.rule)
        lane.pending += 1
        try:
            async with lane.lock:
                waited = self.clock.mono_ms() - received
                if waited > exec_msg.get("deadline_ms", DEFAULT_DEADLINE_MS):
                    return self._reject(exec_msg, "timeout", f"could not start within deadline ({waited} ms)",
                                        decision=pending_decision, rule=static.rule)
                return await self._check_and_execute(exec_msg)
        finally:
            lane.pending -= 1

    async def _check_and_execute(self, exec_msg: dict) -> dict:
        op, args, approval = exec_msg["op"], exec_msg["args"], exec_msg.get("approval")
        verdict = self.gate.check(op, args, approval, now_ms=self.clock.mono_ms(),
                                  device_now_s=self.clock.corrected_s())
        data, io_error, duration = None, None, None
        if verdict.may_execute:
            t0 = time.monotonic()
            try:
                if verdict.fingerprint_check is not None:
                    await self._recheck_fingerprint(*verdict.fingerprint_check)
                data = await self._execute(op, args, exec_msg, verdict)
            except OpFailed as e:
                io_error = e.error
            except (I2CError, GPIOError, CameraError) as e:
                io_error = OpFailed("io_error", str(e), {"errno": _errno_name(e)}).error
            duration = int((time.monotonic() - t0) * 1000)
        result = make_result(exec_msg, verdict, self.gate.manifest_hash, ts=self.clock.now_ms(), data=data,
                             io_error=io_error, duration_ms=duration, msg_id=new_id("r-"))
        self._audit(exec_msg, result)
        if not result["ok"]:
            log.info("exec %s %s → %s (%s)", exec_msg["id"], op, result["error"]["code"], verdict.rule)
        return result

    def _audit(self, exec_msg: dict, result: dict) -> None:
        if self.audit is None:
            return
        try:
            sha = args_sha256(exec_msg["op"], exec_msg["args"]) if isinstance(exec_msg.get("args"), dict) else None
        except TypeError:
            sha = None
        try:
            self.audit.append(exec_record(ts=result["ts"], session_id=self.state.session_id, exec_msg=exec_msg,
                                          args_sha=sha, result=result))
        except OSError:
            log.exception("failed to write audit log")

    # ---- 実行前の安全確認（6.1節の手順6） ------------------------------------------

    async def _recheck_fingerprint(self, bus: int, addr: int) -> None:
        b = self.state.bindings.get((bus, addr))
        if b is None:
            return
        fp = b.fingerprint
        try:
            got = (await asyncio.to_thread(self.i2c.read, bus, addr, fp["reg"], fp["len"],
                                           _fp_reg_width(fp))).hex()
        except I2CError:
            got = None   # 応答しない＝部品が外された。不一致として扱う
        if got == fp["hex"].lower():
            b.last_verified_ms = self.clock.mono_ms()
            return
        del self.state.bindings[(bus, addr)]
        stopped = self._stop_jobs_using(bus, addr)
        log.warning("fingerprint mismatch at bus%d 0x%02x (%s): expected %s, got %s; binding dropped, jobs stopped: %s",
                    bus, addr, b.part, fp["hex"], got, stopped)
        self.emit(make_event("binding_dropped", {"bus": bus, "addr": addr, "part": b.part,
                                                 "expected": fp["hex"], "got": got, "jobs_stopped": stopped},
                             ts=self.clock.now_ms()))
        raise OpFailed("fingerprint_mismatch", f"fingerprint of bus{bus} 0x{addr:02x} changed; binding dropped",
                       {"expected": fp["hex"], "got": got})

    # ---- op ごとの実行 -----------------------------------------------------------

    async def _execute(self, op: str, args: dict, exec_msg: dict, verdict: Verdict) -> dict:
        handler = getattr(self, "_x_" + op.replace(".", "_"))
        return await handler(args, exec_msg, verdict)

    async def _x_sys_info(self, args, exec_msg, verdict):
        try:
            with open("/proc/uptime") as f:
                uptime = int(float(f.read().split()[0]))
        except OSError:
            uptime = int(time.monotonic() - self._started_mono)
        return {
            "hostname": socket.gethostname(),
            "uptime_s": uptime,
            "agent_version": self.agent_version,
            "buses": sorted(self.buses),
            "gpio_lines": sorted(ln["line"] for ln in self.manifest["gpio"]["lines"]),
            "estop": self.state.estop,
        }

    async def _x_sys_estop(self, args, exec_msg, verdict):
        return await self.safety.engage_estop(exec_msg.get("run_id"))

    async def _x_sys_estop_clear(self, args, exec_msg, verdict):
        approved_by = (exec_msg.get("approval") or {}).get("approved_by") if verdict.decision == "approved" else None
        return self.safety.clear_estop(approved_by)

    async def _x_i2c_scan(self, args, exec_msg, verdict):
        bus = args["bus"]
        scan = self.buses[bus]["scan"]
        found = await asyncio.to_thread(self.i2c.scan, bus, scan["first"], scan["last"], scan["exclude"])
        return {"bus": bus, "found": found, "method": "read_byte"}

    async def _x_i2c_read(self, args, exec_msg, verdict):
        raw = await asyncio.to_thread(self.i2c.read, args["bus"], args["addr"], args.get("reg"), args["len"],
                                      args.get("reg_width", 8))
        return {"hex": raw.hex()}

    async def _x_i2c_write(self, args, exec_msg, verdict):
        n = await asyncio.to_thread(self.i2c.write, args["bus"], args["addr"], args.get("reg"),
                                    bytes.fromhex(args["hex"]), args.get("reg_width", 8))
        return {"written": n}

    async def _x_i2c_bind(self, args, exec_msg, verdict):
        bus, addr, fp = args["bus"], args["addr"], args["fingerprint"]
        try:
            got = (await asyncio.to_thread(self.i2c.read, bus, addr, fp["reg"], fp["len"],
                                           _fp_reg_width(fp))).hex()
        except I2CError as e:
            raise OpFailed("io_error", f"cannot read fingerprint: {e}", {"errno": _errno_name(e)}) from None
        if got != fp["hex"].lower():
            raise OpFailed("fingerprint_mismatch", f"fingerprint register 0x{fp['reg']:02x} reads {got}",
                           {"expected": fp["hex"], "got": got})
        self.state.bindings[(bus, addr)] = Binding(
            bus=bus, addr=addr, part=args["part"], fingerprint=dict(fp),
            write_allow_regs=list(args["write_allow_regs"]), last_verified_ms=self.clock.mono_ms())
        log.info("bound bus%d 0x%02x as %s", bus, addr, args["part"])
        return {"bound": True}

    def _stop_jobs_using(self, bus: int, addr: int) -> list[str]:
        """バインドがなくなったアドレスを読む計測・記録ジョブを止める（job_end reason=unbound）。

        承認の根拠だったバインドが消えたのに、16ビットのコマンド（書き込み扱い）を読み続けないため（F2）。
        """
        stopped = self.sampler.stop_using(bus, addr, "unbound")
        if self.logger:
            stopped += self.logger.stop_using(bus, addr, "unbound")
        return stopped

    async def _x_i2c_unbind(self, args, exec_msg, verdict):
        self.state.bindings.pop((args["bus"], args["addr"]), None)
        self._screens.pop((args["bus"], args["addr"]), None)
        return {"bound": False, "jobs_stopped": self._stop_jobs_using(args["bus"], args["addr"])}

    async def _x_display_draw(self, args, exec_msg, verdict):
        """描画と書き込みの列はデバイスが作る（エージェントは表示する内容だけを送る）。"""
        key = (args["bus"], args["addr"])
        part = self.state.bindings[key].part
        out = await asyncio.to_thread(display.draw, self.i2c, args["bus"], args["addr"], part, args["items"],
                                      args.get("clear", True), self._screens.get(key))
        self._screens[key] = out["fb"]
        return {"drawn": len(args["items"]), "bytes": out["bytes"]}

    async def _x_gpio_read(self, args, exec_msg, verdict):
        return {"value": await asyncio.to_thread(self.gpio.read, args["line"])}

    async def _x_gpio_write(self, args, exec_msg, verdict):
        return await self.safety.set_output(args["line"], args["value"], args.get("hold_ms"))

    async def _x_sample_start(self, args, exec_msg, verdict):
        return self.sampler.start(args)

    async def _x_sample_stop(self, args, exec_msg, verdict):
        return {"stopped": self.sampler.stop(args["job_id"], "stopped")}

    async def _x_camera_capture(self, args, exec_msg, verdict):
        cam = self.cameras.get(args["camera"])
        if cam is None:
            raise OpFailed("io_error", f"camera {args['camera']} is listed but not available on this device")
        c = next(c for c in self.manifest["camera"]["devices"] if c["id"] == args["camera"])
        w, h = args.get("width", c["max_width"]), args.get("height", c["max_height"])
        data, ctype = await asyncio.to_thread(cam.capture, w, h)
        blob_id = new_blob_id()
        # 写真は 64KiB のフレームに分けて先に送る（同じ WebSocket なので result より前に届く）
        for m in make_blob_messages(blob_id, data, ctype, ts=self.clock.now_ms(), meta={"exec_id": exec_msg["id"]}):
            self.emit(m)
        return {"blob_id": blob_id, "content_type": ctype, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    async def _x_log_start(self, args, exec_msg, verdict):
        if self.logger is None:
            raise OpFailed("internal", "logging is not available")
        return self.logger.start(args)

    async def _x_log_stop(self, args, exec_msg, verdict):
        return {"stopped": self.logger.stop(args["log_id"], "stopped") if self.logger else False}


def _errno_name(e: OSError) -> str:
    return errno.errorcode.get(e.errno or 0, str(e.errno))


def hello_state(state: DeviceState, safety: Safety) -> dict:
    return {
        "estop": state.estop,
        "bindings": [b.to_wire() for b in state.bindings.values()],
        "jobs": sorted(state.jobs),
        "outputs": safety.outputs_state(),
        **({} if safety.estop_persist else {"estop_persist": False}),   # 非常停止のラッチを書けない（8.2節）
    }

