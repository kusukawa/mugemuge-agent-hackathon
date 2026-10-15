"""計測ジョブ（PROTOCOL.md 4.6節、5.1節 sample.*）。

sample.start は gate で判定済み（各 reads を i2c.read として判定し、レートは開始時に静的に検査）。
ここでは period_ms ごとに読み、batch_ms ごとに samples を送り、終わったら job_end を送る。
読み出しは executor のバスロックを取るので、exec と計測が同じバスで交互に安全に動く。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .clock import Clock
from .hw.i2c import I2CBus, I2CError
from .messages import new_run_id, make_event, new_id
from .policy import DeviceState, Job

log = logging.getLogger(__name__)


class Sampler:
    def __init__(self, state: DeviceState, i2c: I2CBus, clock: Clock, emit: Callable[[dict], None],
                 run_id: str | None = None):
        self.state = state
        # job_id はデーモンの起動ごとの run_id を含める。連番だけだと再起動で振り直され、
        # クラウドに残っている前の job_1 と混ざる（SPEC_FIXES_2.md F1）
        self.run_id = run_id or new_run_id()
        self.i2c = i2c
        self.clock = clock
        self.emit = emit
        self.lock_for: Callable[[int], asyncio.Lock] = lambda bus: asyncio.Lock()   # executor が差し替える
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_reason: dict[str, str] = {}
        self._reads: dict[str, list[dict]] = {}
        self._next = 1

    def start(self, args: dict) -> dict:
        job_id = f"job_{self.run_id}_{self._next}"
        self._next += 1
        reads = args["reads"]
        self._reads[job_id] = reads
        self.state.jobs[job_id] = Job(job_id, reads_per_s=len(reads) * 1000 / args["period_ms"])
        started = self.clock.now_ms()
        task = asyncio.create_task(
            self._run(job_id, args["period_ms"], args["duration_ms"], args.get("batch_ms", 1000), reads),
            name=job_id)
        task.add_done_callback(lambda t: self._on_done(job_id))
        self._tasks[job_id] = task
        log.info("sample job %s started: %d reads every %d ms for %d ms",
                 job_id, len(reads), args["period_ms"], args["duration_ms"])
        return {"job_id": job_id, "columns": ["t_ms", *(r["name"] for r in reads)], "started_ts": started}

    def stop(self, job_id: str, reason: str) -> bool:
        """止める指示を出す（job_end は task の後始末で送る）。task は終わるまで _tasks に残す。"""
        task = self._tasks.get(job_id)
        if task is None or task.done() or job_id in self._stop_reason:
            return False
        self.state.jobs.pop(job_id, None)   # 同時実行数・レートの枠はすぐ空ける
        self._stop_reason[job_id] = reason
        task.cancel()
        return True

    def stop_all(self, reason: str) -> list[str]:
        return [j for j in sorted(self._tasks) if self.stop(j, reason)]

    def stop_using(self, bus: int, addr: int, reason: str) -> list[str]:
        """そのアドレスを読んでいる計測ジョブを止める（unbind・バインド喪失のとき: F2）。"""
        return [j for j in sorted(self._tasks)
                if any(r["bus"] == bus and r["addr"] == addr for r in self._reads.get(j, [])) and self.stop(j, reason)]

    def _on_done(self, job_id: str) -> None:
        """一度も走らないうちにキャンセルされた task は _run の後始末を通らないので、ここで job_end を送る。"""
        if self._tasks.pop(job_id, None) is None:
            return
        self.state.jobs.pop(job_id, None)
        reason = self._stop_reason.pop(job_id, "stopped")
        self.emit(make_event("job_end", {"job_id": job_id, "reason": reason, "rows": 0}, ts=self.clock.now_ms()))

    async def wait_all(self) -> None:
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _read(self, r: dict) -> str | None:
        async with self.lock_for(r["bus"]):
            try:
                raw = await asyncio.to_thread(self.i2c.read, r["bus"], r["addr"], r.get("reg"), r["len"],
                                              r.get("reg_width", 8))
            except I2CError:
                return None
        return raw.hex()

    async def _run(self, job_id: str, period_ms: int, duration_ms: int, batch_ms: int, reads: list[dict]) -> None:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        rows: list[list] = []
        errors = 0
        seq = 0
        total = 0
        next_flush = t0 + batch_ms / 1000

        def flush() -> None:
            nonlocal rows, errors, seq
            if not rows:
                return
            self.emit({"v": 1, "type": "samples", "id": new_id("s-"), "ts": self.clock.now_ms(),
                       "job_id": job_id, "seq": seq, "rows": rows, "errors": errors})
            rows, errors, seq = [], 0, seq + 1

        reason = "completed"
        try:
            k = 0
            while k * period_ms < duration_ms:
                delay = t0 + k * period_ms / 1000 - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif -delay * 1000 > period_ms:
                    # 遅れが1周期を超えたら、取りこぼした周期は飛ばして時刻を合わせる
                    k = int((loop.time() - t0) * 1000 // period_ms)
                    if k * period_ms >= duration_ms:
                        break
                row: list = [k * period_ms]
                for r in reads:
                    v = await self._read(r)
                    if v is None:
                        errors += 1
                    row.append(v)
                rows.append(row)
                total += 1
                if loop.time() >= next_flush:
                    flush()
                    next_flush += batch_ms / 1000
                k += 1
        except asyncio.CancelledError:
            reason = self._stop_reason.pop(job_id, "stopped")
        except Exception:
            log.exception("sample job %s failed", job_id)
            reason = "error"
        flush()
        self._tasks.pop(job_id, None)
        self.state.jobs.pop(job_id, None)
        log.info("sample job %s ended: %s, %d rows", job_id, reason, total)
        self.emit(make_event("job_end", {"job_id": job_id, "reason": reason, "rows": total}, ts=self.clock.now_ms()))
