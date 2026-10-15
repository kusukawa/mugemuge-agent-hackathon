"""マニフェストに基づくルール解決（PROTOCOL.md 6章）。

ここでは「この op をこの引数で実行してよいか」の静的な判定だけを行う。
レート上限・承認の検証・非常停止は gate.py、I/O と指紋の読み直しは実行側が行う。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

RESERVED_I2C = set(range(0x00, 0x08)) | set(range(0x78, 0x80))

READ_OPS = {"sys.info", "i2c.scan", "i2c.read", "gpio.read", "camera.capture"}   # 何も動かさない
# display.draw で描ける部品 → (幅, 高さ, 先頭の列)。hw/display.py が描く。同じ SH1107 でも基板で見える列が違う
DISPLAY_PARTS = {
    "SH1107": (128, 128, 0),            # 128x128（Grove OLED 1.12" など）
    "SH1107_64x128": (64, 128, 32),     # 64x128（M5Stack Unit OLED など）。RAM の 32〜95 列だけが見える
}
ALWAYS_SAFE_OPS = {"sys.estop", "i2c.unbind", "sample.stop", "log.stop"}


class Level(str, Enum):
    ALLOW = "allow"
    APPROVAL = "approval"
    DENY = "deny"

    @property
    def strictness(self) -> int:
        return {"allow": 0, "approval": 1, "deny": 2}[self.value]


class BadRequest(ValueError):
    pass


@dataclass
class Binding:
    bus: int
    addr: int
    part: str
    fingerprint: dict
    write_allow_regs: list[int]
    last_verified_ms: int = 0          # 指紋を最後に確かめた時刻（単調時計 mono_ms。壁時計の飛びに影響されない）

    def to_wire(self) -> dict:
        return {
            "bus": self.bus,
            "addr": self.addr,
            "part": self.part,
            "fingerprint": dict(self.fingerprint),
            "write_allow_regs": list(self.write_allow_regs),
        }


@dataclass
class Job:
    job_id: str
    reads_per_s: float


@dataclass
class DeviceState:
    device_id: str
    session_id: str | None = None
    estop: bool = False
    bindings: dict[tuple[int, int], Binding] = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)


@dataclass
class Decision:
    level: Level
    rule: str
    reason: str | None = None          # deny の理由コード（例: i2c.reserved_address）
    summary: str = ""                  # 承認カードに出す一文
    safe: bool = False                 # 安全側の操作（非常停止中も通り、レート上限もかけない）
    fingerprint_check: tuple[int, int] | None = None  # 実行前に指紋を読み直すバインド
    i2c_reads: int = 0                 # レート上限に数える I2C 読み出し回数
    i2c_writes: int = 0
    gpio_writes: int = 0
    displays: int = 0                  # display.draw の画面数（limits.display_per_s）


def fmt_ms(ms: int) -> str:
    """承認カードに出す時間。丸めない（1.5時間を「1時間」、1.5秒を「1秒」と書かない）。"""
    if ms < 1000:
        return f"{ms}ミリ秒"
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    sec, msr = divmod(rest, 1000)
    parts = []
    if h:
        parts.append(f"{h}時間")
    if m:
        parts.append(f"{m}分")
    if sec or msr:
        parts.append(f"{sec}.{msr:03d}".rstrip("0").rstrip(".") + "秒")
    return "".join(parts)


def _h(addr: int) -> str:
    return f"0x{addr:02x}"


def _reg(reg: int | None) -> str:
    return "（レジスタ指定なし）" if reg is None else f"レジスタ 0x{reg:02x}"


class Policy:
    def __init__(self, manifest: dict):
        self.m = manifest
        self.buses = {b["bus"]: b for b in manifest["i2c"]["buses"]}
        self.fixed = {
            (b["bus"], a["addr"]): a for b in manifest["i2c"]["buses"] for a in b["addresses"]
        }
        self.lines = {ln["line"]: ln for ln in manifest["gpio"]["lines"]}
        self.limits = manifest["limits"]
        self.sample = manifest["sample"]
        self.log = manifest["log"]
        self.cameras = {c["id"]: c for c in manifest.get("camera", {}).get("devices", [])}

    # ---- entry point -------------------------------------------------------

    def evaluate(self, op: str, args: dict, state: DeviceState, now_ms: int = 0) -> Decision:
        handler = getattr(self, "_op_" + op.replace(".", "_"), None)
        if handler is None:
            raise BadRequest(f"unknown op: {op}")
        return handler(args, state, now_ms)

    # ---- sys ---------------------------------------------------------------

    def _op_sys_info(self, args, state, now_ms):
        return Decision(Level.ALLOW, "sys.info")

    def _op_sys_estop(self, args, state, now_ms):
        return Decision(Level.ALLOW, "sys.estop", safe=True)

    def _op_sys_estop_clear(self, args, state, now_ms):
        level = Level(self.m["safety"]["estop_clear"])
        return Decision(level, "sys.estop_clear", summary="非常停止を解除する")

    # ---- i2c ---------------------------------------------------------------

    def _bus(self, args) -> dict | None:
        return self.buses.get(args["bus"])

    def _check_reg(self, args) -> None:
        width = args.get("reg_width", 8)
        reg = args.get("reg")
        if reg is not None and reg > (0xFF if width == 8 else 0xFFFF):
            raise BadRequest(f"reg 0x{reg:x} does not fit reg_width {width}")

    def _addr_rule(self, bus: int, addr: int, action: str, state: DeviceState, reg: int | None = None):
        """(level, rule, reason, binding) を返す。action は read/write/bind。"""
        b = self.buses.get(bus)
        if b is None:
            return Level.DENY, f"i2c.bus{bus}", "i2c.bus_not_listed", None
        if addr in RESERVED_I2C:
            return Level.DENY, f"i2c.bus{bus}.{_h(addr)}.{action}", "i2c.reserved_address", None
        fixed = self.fixed.get((bus, addr))
        binding = state.bindings.get((bus, addr))
        # 優先順: protected な固定ルール → バインド → protected でない固定ルール → 未知
        if fixed is not None and (fixed["protected"] or binding is None or action == "bind"):
            rule = f"i2c.bus{bus}.{_h(addr)}.{action}"
            if action == "read":
                return Level(fixed["read"]), rule, None, None
            if action == "write":
                if reg is not None and reg in fixed["write_allow_regs"]:
                    return Level.ALLOW, rule, None, None
                return Level(fixed["write"]), rule, None, None
            if fixed["protected"]:
                return Level.DENY, rule, "i2c.protected_address", None
            return Level(b["unknown"]["bind"]), rule, None, None
        if binding is not None and action != "bind":
            rule = f"i2c.bus{bus}.bound.{action}"
            if action == "read":
                return Level.ALLOW, rule, None, binding
            if reg is not None and reg in binding.write_allow_regs:
                return Level.ALLOW, rule, None, binding
            return Level(b["bound"]["other_write"]), rule, None, binding
        return Level(b["unknown"][action]), f"i2c.bus{bus}.unknown.{action}", None, binding

    def _op_i2c_scan(self, args, state, now_ms):
        b = self._bus(args)
        if b is None:
            return Decision(Level.DENY, f"i2c.bus{args['bus']}", "i2c.bus_not_listed")
        rule = f"i2c.bus{args['bus']}.scan"
        if not b["scan"]["enabled"]:
            return Decision(Level.DENY, rule, "i2c.scan_disabled")
        n = b["scan"]["last"] - b["scan"]["first"] + 1
        return Decision(Level.ALLOW, rule, i2c_reads=n)

    def _op_i2c_read(self, args, state, now_ms):
        self._check_reg(args)
        if args["len"] > self.limits["max_read_len"]:
            return Decision(Level.DENY, "limits.max_read_len", "limits.max_read_len")
        reg = args.get("reg")
        if reg is not None and args.get("reg_width", 8) == 16:
            # 16 ビットの reg は SHT3x などでは「コマンド」そのもの（ソフトリセットやヒーター ON もありうる）。
            # 読み出しでも、その reg への書き込みとしてルールを解決する（6.2節）。
            level, rule, reason, _ = self._addr_rule(args["bus"], args["addr"], "write", state, reg)
            return Decision(
                level, rule, reason,
                summary=f"bus{args['bus']} {_h(args['addr'])} にコマンド 0x{reg:04x} を送り {args['len']} バイト読む",
                i2c_reads=1,
            )
        level, rule, reason, _ = self._addr_rule(args["bus"], args["addr"], "read", state)
        return Decision(
            level, rule, reason,
            summary=f"bus{args['bus']} {_h(args['addr'])} の{_reg(reg)}から {args['len']} バイト読む",
            i2c_reads=1,
        )

    def _op_i2c_write(self, args, state, now_ms):
        self._check_reg(args)
        n = len(args["hex"]) // 2
        if n > self.limits["max_write_len"]:
            return Decision(Level.DENY, "limits.max_write_len", "limits.max_write_len")
        reg = args.get("reg")
        level, rule, reason, binding = self._addr_rule(args["bus"], args["addr"], "write", state, reg)
        check = None
        # 指紋の読み直しは、バインドで recheck: false と承認されていなければ行う（SPEC_REVIEW.md B3）
        if binding is not None and level is not Level.DENY and binding.fingerprint.get("recheck", True):
            recheck = self.buses[args["bus"]]["bound"]["fingerprint_recheck_ms"]
            if now_ms - binding.last_verified_ms >= recheck:
                check = (args["bus"], args["addr"])
        part = f"（{binding.part}）" if binding else ""
        return Decision(
            level, rule, reason,
            summary=f"bus{args['bus']} {_h(args['addr'])}{part} の{_reg(reg)}に {args['hex']} を書き込む",
            fingerprint_check=check,
            i2c_writes=1,
        )

    def _op_i2c_bind(self, args, state, now_ms):
        b = self._bus(args)
        if b is None:
            return Decision(Level.DENY, f"i2c.bus{args['bus']}", "i2c.bus_not_listed")
        if len(args["write_allow_regs"]) > b["bound"]["max_write_allow_regs"]:
            return Decision(Level.DENY, f"i2c.bus{args['bus']}.bind", "i2c.too_many_write_allow_regs")
        # 「書き込み前に指紋を読み直さない」バインドは、マニフェストが許すときだけ（引数は Agent が作るので、
        # 注入された Agent が毎回付けても、所有者が許していなければ通らない: SPEC_FIXES_2.md I1）
        if args["fingerprint"].get("recheck", True) is False and not b["bound"]["allow_no_recheck"]:
            return Decision(Level.DENY, f"i2c.bus{args['bus']}.bind", "i2c.no_recheck_not_allowed")
        level, rule, reason, _ = self._addr_rule(args["bus"], args["addr"], "bind", state)
        regs = ", ".join(f"0x{r:02x}" for r in args["write_allow_regs"]) or "なし"
        norecheck = "。書き込み前の指紋の確認をしない" if args["fingerprint"].get("recheck", True) is False else ""
        return Decision(
            level, rule, reason,
            summary=f"bus{args['bus']} {_h(args['addr'])} を {args['part']} として扱う（自動で書き込めるレジスタ: {regs}{norecheck}）",
            i2c_reads=1,  # バインド時に指紋を読む
        )

    def _op_i2c_unbind(self, args, state, now_ms):
        return Decision(Level.ALLOW, f"i2c.bus{args['bus']}.unbind", safe=True)

    # ---- display -----------------------------------------------------------

    def _op_display_draw(self, args, state, now_ms):
        """表示デバイスとしてバインド済みのアドレスにだけ描く。レベルはマニフェストの bound.display（既定 approval）。"""
        b = self._bus(args)
        if b is None:
            return Decision(Level.DENY, f"i2c.bus{args['bus']}", "i2c.bus_not_listed")
        rule = f"i2c.bus{args['bus']}.bound.display"
        binding = state.bindings.get((args["bus"], args["addr"]))
        if binding is None:
            return Decision(Level.DENY, rule, "display.not_bound")
        if binding.part not in DISPLAY_PARTS:
            return Decision(Level.DENY, rule, "display.unsupported_part")
        fixed = self.fixed.get((args["bus"], args["addr"]))
        if fixed is not None and fixed["protected"]:
            return Decision(Level.DENY, rule, "i2c.protected_address")
        check = None
        if binding.fingerprint.get("recheck", True):
            if now_ms - binding.last_verified_ms >= b["bound"]["fingerprint_recheck_ms"]:
                check = (args["bus"], args["addr"])
        texts = " / ".join(it["text"] for it in args["items"]) or "（空）"
        return Decision(
            Level(b["bound"]["display"]), rule,
            summary=f"bus{args['bus']} {_h(args['addr'])}（{binding.part}）の画面に表示する: {texts}",
            fingerprint_check=check, displays=1,
        )

    # ---- gpio --------------------------------------------------------------

    def _op_gpio_read(self, args, state, now_ms):
        ln = self.lines.get(args["line"])
        if ln is None:
            return Decision(Level.DENY, f"gpio.{args['line']}.read", "gpio.line_not_listed")
        return Decision(Level(ln["read"]), f"gpio.{args['line']}.read")

    def _op_gpio_write(self, args, state, now_ms):
        line = args["line"]
        rule = f"gpio.{line}.write"
        ln = self.lines.get(line)
        if ln is None:
            return Decision(Level.DENY, rule, "gpio.line_not_listed")
        if ln["direction"] != "output":
            return Decision(Level.DENY, rule, "gpio.not_output")
        if args["value"] == ln["safe_value"]:
            return Decision(Level.ALLOW, rule + ".safe", safe=True,
                            summary=f"{ln['label']}（GPIO{line}）を安全値 {ln['safe_value']} に戻す")
        hold = args.get("hold_ms")
        max_on = ln["max_on_ms"]
        if max_on is not None and hold is not None and hold > max_on:
            return Decision(Level.DENY, rule, "gpio.hold_exceeds_max_on")
        effective = hold if hold is not None else max_on
        how = fmt_ms(effective) if effective is not None else "次の書き込みまで"
        return Decision(
            Level(ln["write"]), rule,
            summary=f"{ln['label']}（GPIO{line}）を {how} {args['value']} にする",
            gpio_writes=1,
        )

    # ---- sample ------------------------------------------------------------

    def _op_sample_start(self, args, state, now_ms):
        s = self.sample
        if args["period_ms"] < s["min_period_ms"]:
            return Decision(Level.DENY, "sample.start", "sample.period_too_short")
        if args["duration_ms"] > s["max_duration_ms"]:
            return Decision(Level.DENY, "sample.start", "sample.duration_too_long")
        reads = args["reads"]
        if len(reads) > s["max_reads_per_tick"]:
            return Decision(Level.DENY, "sample.start", "sample.too_many_reads")
        names = [r["name"] for r in reads]
        if len(names) != len(set(names)):
            raise BadRequest("sample.start: duplicate read name")
        # 記録ジョブ（log_）は log.max_concurrent_logs で別に数える（SPEC_REVIEW.md B4）。読み出しのレートは両方を合わせて数える
        if sum(1 for j in state.jobs if not j.startswith("log_")) >= s["max_concurrent_jobs"]:
            return Decision(Level.DENY, "sample.start", "sample.too_many_jobs")
        rate = len(reads) * 1000 / args["period_ms"]
        running = sum(j.reads_per_s for j in state.jobs.values())
        if running + rate > self.limits["i2c_read_per_s"]:
            return Decision(Level.DENY, "sample.start", "sample.read_rate_exceeded")

        worst = Decision(Level.ALLOW, "sample.start")
        for r in reads:
            d = self._op_i2c_read({k: v for k, v in r.items() if k != "name"}, state, now_ms)
            if d.level.strictness > worst.level.strictness:
                worst = d
        cols = ", ".join(names)
        return Decision(
            worst.level, worst.rule, worst.reason,
            summary=f"{fmt_ms(args['period_ms'])}ごとに {fmt_ms(args['duration_ms'])}計測する（{cols}）",
        )

    def _op_sample_stop(self, args, state, now_ms):
        return Decision(Level.ALLOW, "sample.stop", safe=True)

    # ---- camera ------------------------------------------------------------

    def _op_camera_capture(self, args, state, now_ms):
        cam = self.cameras.get(args["camera"])
        rule = f"camera.{args['camera']}.capture"
        if cam is None:
            return Decision(Level.DENY, rule, "camera.not_listed")
        if args.get("width", 0) > cam["max_width"] or args.get("height", 0) > cam["max_height"]:
            return Decision(Level.DENY, rule, "camera.size_exceeds_max")
        label = cam.get("label") or args["camera"]
        return Decision(Level(cam["capture"]), rule, summary=f"{label}（{args['camera']}）で写真を1枚撮る")

    # ---- log（長期の記録） -------------------------------------------------

    def _op_log_start(self, args, state, now_ms):
        lg = self.log
        if args["interval_ms"] < lg["min_interval_ms"]:
            return Decision(Level.DENY, "log.start", "log.interval_too_short")
        if args["duration_ms"] > lg["max_duration_ms"]:
            return Decision(Level.DENY, "log.start", "log.duration_too_long")
        reads = args["reads"]
        if len(reads) > self.sample["max_reads_per_tick"]:
            return Decision(Level.DENY, "log.start", "log.too_many_reads")
        names = [r["name"] for r in reads]
        if len(names) != len(set(names)):
            raise BadRequest("log.start: duplicate read name")
        if not reads and "photo" not in args:
            raise BadRequest("log.start: nothing to record (reads or photo)")
        if sum(1 for j in state.jobs if j.startswith("log_")) >= lg["max_concurrent_logs"]:
            return Decision(Level.DENY, "log.start", "log.too_many_logs")
        rate = len(reads) * 1000 / args["interval_ms"]
        if sum(j.reads_per_s for j in state.jobs.values()) + rate > self.limits["i2c_read_per_s"]:
            return Decision(Level.DENY, "log.start", "log.read_rate_exceeded")
        worst = Decision(Level.ALLOW, "log.start")
        for r in reads:
            d = self._op_i2c_read({k: v for k, v in r.items() if k != "name"}, state, now_ms)
            if d.level.strictness > worst.level.strictness:
                worst = d
        photo = args.get("photo")
        if photo is not None:
            if photo["interval_ms"] < lg["min_photo_interval_ms"]:
                return Decision(Level.DENY, "log.start", "log.photo_interval_too_short")
            cam = self.cameras.get(photo["camera"])
            if cam is not None and photo["interval_ms"] < cam["min_interval_ms"]:
                return Decision(Level.DENY, "log.start", "camera.interval_below_min")
            d = self._op_camera_capture({k: v for k, v in photo.items() if k != "interval_ms"}, state, now_ms)
            if d.level.strictness > worst.level.strictness:
                worst = d
        cols = "・".join(names) or "なし"
        pic = f"、写真を {fmt_ms(photo['interval_ms'])}ごとに {photo['camera']} で" if photo else ""
        return Decision(
            worst.level, worst.rule, worst.reason,
            summary=f"「{args['name'][:24]}」を {fmt_ms(args['interval_ms'])}ごとに {fmt_ms(args['duration_ms'])}記録する（{cols}{pic}）"[:200],
        )

    def _op_log_stop(self, args, state, now_ms):
        return Decision(Level.ALLOW, "log.stop", safe=True)
