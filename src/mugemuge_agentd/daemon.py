"""部品の組み立てと、起動から終了までの流れ（PROTOCOL.md 8.3節）。

    起動: 出力を safe_value に初期化 → デッドマン監視を開始 → 接続ループ
    終了: 計測ジョブを止める → 出力を safe_value に戻す → bye を送る → 切断
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import __version__
from .audit import AuditLog
from .client import BACKOFF_S, Client
from .clock import Clock
from .executor import Executor
from .gate import Gate
from .logger import Logger
from .hw.gpio import GPIO
from .hw.i2c import I2CBus
from .policy import DeviceState
from .safety import Safety
from .sampler import Sampler

from .messages import new_run_id

log = logging.getLogger(__name__)


class Daemon:
    def __init__(
        self,
        manifest: dict,
        i2c: I2CBus,
        gpio: GPIO,
        *,
        gateway_url: str,
        token: str | None,
        audit_path: str | Path | None,
        clock: Clock | None = None,
        backoff: tuple[float, ...] = BACKOFF_S,
        cameras: dict | None = None,
        spool_dir: str | Path | None = None,
    ):
        self.manifest = manifest
        self.i2c = i2c
        self.gpio = gpio
        self.clock = clock or Clock()
        self.state = DeviceState(device_id=manifest["device"]["id"])
        self.gate = Gate(manifest, self.state, now_ms=self.clock.mono_ms())   # 間隔とレートは単調時計（PROTOCOL.md 8章）
        audit = AuditLog(audit_path, manifest["logging"]["audit_max_mb"]) if audit_path else None
        if spool_dir is None:
            spool_dir = (Path(audit_path).parent if audit_path else Path("/tmp/mugemuge")) / "spool"
        # emit は client ができてから差し込む（循環を避けるため一段かませる）。
        # 非常停止のラッチは spool と同じ場所に残し、再起動しても止まったまま起動する
        self.safety = Safety(manifest, self.state, gpio, self.clock, self._emit,
                             latch_path=Path(spool_dir).parent / "estop.json")
        self.run_id = new_run_id()
        self.sampler = Sampler(self.state, i2c, self.clock, self._emit, run_id=self.run_id)
        self.cameras = cameras or {}
        self.logger = Logger(manifest, self.state, i2c, self.cameras, self.clock, Path(spool_dir),
                             send=lambda m: self.client.send_now(m), emit=self._emit, run_id=self.run_id)

        def stop_jobs(reason: str) -> list[str]:
            # 記録ジョブ（読み出しと撮影だけ）はデッドマンでは止めない。非常停止では止める（PROTOCOL.md 8.1節）
            stopped = self.sampler.stop_all(reason)
            if reason != "deadman":
                stopped += self.logger.stop_all(reason)
            return stopped

        self.safety.stop_jobs = stop_jobs
        self.executor = Executor(manifest, self.state, self.gate, i2c, gpio, self.safety, self.sampler, audit,
                                 self.clock, self._emit, __version__, cameras=self.cameras, logger=self.logger)
        self.client = Client(manifest, self.state, self.executor, self.safety, self.clock,
                             url=gateway_url, token=token, backoff=backoff)
        self.client.on_connected.append(lambda: self.logger.resend(force=True))
        self.client.on_ack = self.logger.ack
        self._stop: asyncio.Event | None = None

    def _emit(self, msg: dict) -> None:
        self.client.emit(msg)

    async def run(self) -> None:
        self._stop = asyncio.Event()
        self.safety.init_outputs()
        log.info("outputs initialised to safe values: %s", self.safety.outputs_state())
        self.safety.start_watchdog()
        self.logger.start_resender()
        client_task = asyncio.create_task(self.client.run(), name="client")
        try:
            await self._stop.wait()
        finally:
            await self.shutdown(client_task)

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self, client_task: asyncio.Task | None = None, reason: str = "shutdown") -> None:
        self.client.stop()                       # 以後は張り直さない
        self.sampler.stop_all("stopped")
        self.logger.stop_all("stopped")
        await self.sampler.wait_all()
        await self.logger.wait_all()
        await self.logger.stop_resender()
        reverted = await self.safety.revert_all()
        log.info("outputs reverted to safe values on %s: %s", reason, reverted)
        await self.safety.stop_watchdog()
        await self.client.say_bye(reason)        # bye を送ってから閉じる
        if client_task is not None:
            try:
                await asyncio.wait_for(client_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                client_task.cancel()
        await asyncio.to_thread(self.i2c.close)
        await asyncio.to_thread(self.gpio.close)
