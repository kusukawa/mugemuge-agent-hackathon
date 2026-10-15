"""安全機構（PROTOCOL.md 8章）: 出力の管理と自動 OFF、デッドマン、非常停止。

ここは「実行してよいか」を判定しない（それは gate）。判定を通った書き込みを行い、
時間が来たら・通信が途絶えたら・非常停止が来たら、出力を safe_value に戻す。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable

from .clock import Clock
from .hw.gpio import GPIO, line_configs
from .messages import make_event
from .policy import DeviceState

log = logging.getLogger(__name__)

WATCHDOG_TICK_S = 0.1


class Safety:
    def __init__(self, manifest: dict, state: DeviceState, gpio: GPIO, clock: Clock, emit: Callable[[dict], None],
                 latch_path: str | Path | None = None):
        self.manifest = manifest
        self.state = state
        # 非常停止のラッチをディスクに残す。再起動で承認なしに解除されないように、起動時に読み戻す
        self.latch_path = Path(latch_path) if latch_path else None
        self.estop_persist = True               # ラッチをディスクに残せるか（hello の state.estop_persist）
        if self.latch_path and self.latch_path.exists():
            self.state.estop = True
            try:
                info = json.loads(self.latch_path.read_text())
            except (OSError, ValueError):
                info = {}
            log.warning("emergency stop is still latched (engaged at %s by %s); clear it with an approved "
                        "sys.estop_clear", info.get("ts"), info.get("by"))
        elif self.latch_path and not self._latch_writable():
            # 書けないまま動くと、非常停止をかけても再起動で承認なしに解除される。止めた状態で起動し、
            # つながったまま原因を調べられるようにする（起動は拒否しない: SPEC_FIXES_3.md P4）
            self.state.estop = True
            self.estop_persist = False
            log.error("cannot write the emergency stop latch in %s; starting with the emergency stop engaged",
                      self.latch_path.parent)
        self.gpio = gpio
        self.clock = clock
        self.emit = emit
        self.stop_jobs: Callable[[str], list[str]] = lambda reason: []   # sampler が差し替える
        self.deadman_ms: int = manifest["safety"]["deadman_ms"]
        self.lines = {ln["line"]: ln for ln in manifest["gpio"]["lines"] if ln["direction"] == "output"}
        self.values: dict[int, int] = {}
        self.revert_at_ms: dict[int, int] = {}
        self._timers: dict[int, asyncio.TimerHandle] = {}
        self._last_rx_mono: int | None = None   # None ＝ デッドマンは未武装（まだ何も受けていない／作動済み）
        self._watchdog: asyncio.Task | None = None

    # ---- 起動・終了（8.3節） -----------------------------------------------------

    def init_outputs(self) -> None:
        """全ラインを要求し、出力を safe_value に初期化する。接続を始める前に呼ぶ。"""
        self.gpio.configure(line_configs(self.manifest))
        self.values = {line: ln["safe_value"] for line, ln in self.lines.items()}

    def revert_all_sync(self) -> list[int]:
        """終了時・例外時用。イベントループの外からでも呼べる。"""
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        self.revert_at_ms.clear()
        changed = []
        for line, ln in self.lines.items():
            try:
                self.gpio.write(line, ln["safe_value"])
            except OSError:
                log.exception("failed to revert GPIO%d to safe value", line)
                continue
            if self.values.get(line) != ln["safe_value"]:
                changed.append(line)
            self.values[line] = ln["safe_value"]
        return changed

    async def revert_all(self) -> list[int]:
        return await asyncio.to_thread(self.revert_all_sync)

    # ---- 出力（gpio.write の実行部） -------------------------------------------

    async def set_output(self, line: int, value: int, hold_ms: int | None) -> dict:
        """gate を通った gpio.write を実行する。能動側なら min(hold_ms, max_on_ms) 後に自動で戻す。"""
        ln = self.lines[line]
        self._cancel_timer(line)
        await asyncio.to_thread(self.gpio.write, line, value)
        self.values[line] = value
        data: dict = {"value": value}
        if value != ln["safe_value"]:
            limits = [x for x in (hold_ms, ln["max_on_ms"]) if x is not None]
            if limits:
                hold = min(limits)
                reason = "max_on_ms" if hold == ln["max_on_ms"] else "hold_ms"
                at = self.clock.now_ms() + hold
                self.revert_at_ms[line] = at
                self._timers[line] = asyncio.get_running_loop().call_later(
                    hold / 1000, lambda: asyncio.ensure_future(self._auto_revert(line, reason)))
                data["revert_at_ms"] = at
        return data

    def _cancel_timer(self, line: int) -> None:
        t = self._timers.pop(line, None)
        if t is not None:
            t.cancel()
        self.revert_at_ms.pop(line, None)

    async def _auto_revert(self, line: int, reason: str) -> None:
        self._timers.pop(line, None)
        self.revert_at_ms.pop(line, None)
        ln = self.lines[line]
        try:
            await asyncio.to_thread(self.gpio.write, line, ln["safe_value"])
        except OSError:
            log.exception("auto revert of GPIO%d failed", line)
            return
        self.values[line] = ln["safe_value"]
        log.info("GPIO%d (%s) reverted to %d (%s)", line, ln["label"], ln["safe_value"], reason)
        self.emit(make_event("output_reverted", {"line": line, "label": ln["label"], "value": ln["safe_value"],
                                                 "reason": reason}, ts=self.clock.now_ms()))

    def outputs_state(self) -> list[dict]:
        out = []
        for line in sorted(self.lines):
            o = {"line": line, "value": self.values.get(line, self.lines[line]["safe_value"])}
            if line in self.revert_at_ms:
                o["revert_at_ms"] = self.revert_at_ms[line]
            out.append(o)
        return out

    # ---- デッドマン（8.1節） ------------------------------------------------------

    def touch(self) -> None:
        """クラウドから何か（種別を問わず）受け取るたびに呼ぶ。"""
        self._last_rx_mono = self.clock.mono_ms()

    def start_watchdog(self) -> None:
        if self._watchdog is None:
            self._watchdog = asyncio.create_task(self._watch(), name="deadman")

    async def stop_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            self._watchdog = None

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK_S)
            last = self._last_rx_mono
            if last is None:
                continue
            silent = self.clock.mono_ms() - last
            if silent >= self.deadman_ms:
                self._last_rx_mono = None
                await self.trip_deadman(silent)

    async def trip_deadman(self, silent_ms: int) -> None:
        reverted = await self.revert_all()
        jobs = self.stop_jobs("deadman")
        log.warning("deadman tripped after %d ms of silence (outputs reverted: %s, jobs stopped: %s)",
                    silent_ms, reverted, jobs)
        self.emit(make_event("deadman_tripped", {"silent_ms": silent_ms, "outputs_reverted": reverted,
                                                 "jobs_stopped": jobs}, ts=self.clock.now_ms()))

    # ---- 非常停止（8.2節） --------------------------------------------------------

    def _latch_writable(self) -> bool:
        """ラッチの置き場所に一時ファイルを書いて消せるか。"""
        probe = self.latch_path.with_suffix(".probe")
        try:
            self.latch_path.parent.mkdir(parents=True, exist_ok=True)
            with open(probe, "w") as f:
                f.write("probe")
                f.flush()
                os.fsync(f.fileno())
            probe.unlink()
            return True
        except OSError:
            return False

    def _write_latch(self, by: str | None) -> bool:
        """ラッチを書く。書けなければ False（止める動作そのものは続ける）。"""
        if not self.latch_path:
            return True
        try:
            self.latch_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.latch_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump({"ts": self.clock.now_ms(), "by": by}, f)
                f.flush()
                os.fsync(f.fileno())             # 直後の電源断でも残す
            os.replace(tmp, self.latch_path)
            return True
        except OSError:
            self.estop_persist = False
            log.exception("could not persist the emergency stop latch; a restart would clear it without approval")
            return False

    async def engage_estop(self, by: str | None) -> dict:
        self.state.estop = True
        persisted = self._write_latch(by)
        reverted = await self.revert_all()
        jobs = self.stop_jobs("estop")
        log.warning("emergency stop engaged by %s", by)
        detail = {"by": by, "outputs_reverted": reverted, "jobs_stopped": jobs}
        if not persisted:
            detail["persisted"] = False                 # 再起動で承認なしに解除されうる（8.2節）
        self.emit(make_event("estop_engaged", detail, ts=self.clock.now_ms()))
        return {"estop": True, "outputs_reverted": reverted, "jobs_stopped": jobs}

    def clear_estop(self, approved_by: str | None) -> dict:
        self.state.estop = False
        if self.latch_path:
            self.latch_path.unlink(missing_ok=True)
        log.warning("emergency stop cleared (approved_by=%s)", approved_by)
        self.emit(make_event("estop_cleared", {"approved_by": approved_by}, ts=self.clock.now_ms()))
        return {"estop": False}
