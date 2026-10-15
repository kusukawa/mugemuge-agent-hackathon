"""長期の記録ジョブ（log.start、PROTOCOL.md 5.1節・8.5節）。

計測ジョブ（sample.start、最長10分）との違い:
- 数時間〜数日。interval_ms ごとに reads を読み、photo があれば photo.interval_ms ごとに写真を撮る
- まず Pi のディスク（spool）に書く。バッチ（10秒か60行ごと、写真を撮ったときも区切る）を samples として送り、
  クラウドの ack（そのバッチまで保存した）を受けたら消す。つながっていない間は送らずに貯め、つながったら古い順に送り直す
- デッドマンでは止めない（読み出しと撮影だけで何も動かさないので、通信断の間こそ記録を続ける）。非常停止では止める
- デーモンを再起動すると記録自体は終わるが、未送信の分はディスクに残り、次の起動で送る

spool のレイアウト: <spool>/<log_id>/meta.json、batch-<seq:06d>.json、<blob_id>.<ext>
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from .clock import Clock
from .hw.camera import Camera, CameraError
from .hw.i2c import I2CBus, I2CError
from .messages import new_run_id, make_blob_messages, make_event, new_blob_id, new_id
from .policy import DeviceState, Job

log = logging.getLogger(__name__)

BATCH_MS = 30_000          # 長期の記録で Firestore のドキュメントが増えすぎないよう、30秒か120行ごと
BATCH_ROWS = 120
RESEND_AFTER_S = 30
SEND_WINDOW = 4            # 同時に送る（ack 待ちの）バッチの数の上限（I3）
PHOTO_PAUSE_RATIO = 0.8    # 写真の総量が spool の上限のこの割合を超えたら撮らない


def fit_long_side(w: int, h: int, max_px: int) -> tuple[int, int]:
    """長辺が max_px に収まるよう縦横比を保って縮める（記録の写真: F5）。"""
    long_side = max(w, h)
    if long_side <= max_px:
        return w, h
    return max(1, w * max_px // long_side), max(1, h * max_px // long_side)


class LogJob:
    def __init__(self, log_id: str, args: dict, started_ts: int, dirpath: Path, mono_start_ms: int = 0):
        self.log_id, self.args, self.started_ts, self.dir = log_id, args, started_ts, dirpath
        self.mono_start_ms = mono_start_ms     # 開始時の単調時計。壁時計が跳んでも開始時刻を計算し直せる（F6）
        self.rows: list[list] = []
        self.photos: list[dict] = []
        self.errors = 0
        self.seq = 0
        self.photos_paused = False
        self.last_photo_bytes = 0
        self.total_rows = 0
        self.total_photos = 0
        self.task: asyncio.Task | None = None
        self.stop_reason: str | None = None


class Logger:
    def __init__(self, manifest: dict, state: DeviceState, i2c: I2CBus, cameras: dict[str, Camera], clock: Clock,
                 spool_dir: Path, send: Callable[[dict], bool], emit: Callable[[dict], None], run_id: str | None = None):
        self.manifest, self.state, self.i2c, self.cameras, self.clock = manifest, state, i2c, cameras, clock
        self.run_id = run_id or new_run_id()     # log_id に含める（F1。計測の job_id と同じ形）
        self._next = 1
        self.spool = Path(spool_dir)
        self.spool.mkdir(parents=True, exist_ok=True)
        self.spool_limit = manifest["log"]["spool_mb"] * 1024 * 1024
        self.photo_max_px = manifest["log"]["photo_max_px"]
        self.send = send            # つながっていれば送って True、いなければ False（保持しない）
        self.emit = emit            # event 用（切断中は client が保持する）
        self.lock_for: Callable[[int], asyncio.Lock] = lambda bus: asyncio.Lock()
        self.jobs: dict[str, LogJob] = {}
        self.sent_at: dict[tuple[str, int], float] = {}
        self._resender: asyncio.Task | None = None

    # ---- 開始と停止 ------------------------------------------------------------------

    def start(self, args: dict) -> dict:
        # 起動ごとの run_id ＋連番。再起動をまたいでも重ならない（spool に同じ名前があれば番号を進める）
        while True:
            log_id = f"log_{self.run_id}_{self._next}"
            self._next += 1
            if not (self.spool / log_id).exists():
                break
        started = self.clock.now_ms()
        d = self.spool / log_id
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"log_id": log_id, "args": args, "started_ts": started}))
        job = LogJob(log_id, args, started, d, mono_start_ms=self.clock.mono_ms())
        self.jobs[log_id] = job
        self.state.jobs[log_id] = Job(log_id, reads_per_s=len(args["reads"]) * 1000 / args["interval_ms"])
        job.task = asyncio.create_task(self._run(job), name=log_id)
        log.info("log %s started: %s every %d ms for %d ms%s", log_id, args["name"], args["interval_ms"],
                 args["duration_ms"], " with photos" if args.get("photo") else "")
        return {"log_id": log_id, "columns": ["t_ms", *(r["name"] for r in args["reads"])], "started_ts": started}

    def stop(self, log_id: str, reason: str) -> bool:
        job = self.jobs.get(log_id)
        if job is None or job.task is None or job.task.done() or job.stop_reason:
            return False
        job.stop_reason = reason
        self.state.jobs.pop(log_id, None)
        job.task.cancel()
        return True

    def stop_all(self, reason: str) -> list[str]:
        return [j for j in list(self.jobs) if self.stop(j, reason)]

    def stop_using(self, bus: int, addr: int, reason: str) -> list[str]:
        """そのアドレスを読んでいる記録ジョブを止める（unbind・バインド喪失のとき: F2）。"""
        return [j for j, job in list(self.jobs.items())
                if any(r["bus"] == bus and r["addr"] == addr for r in job.args["reads"]) and self.stop(j, reason)]

    async def wait_all(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- 記録 ------------------------------------------------------------------------

    async def _read(self, r: dict) -> str | None:
        async with self.lock_for(r["bus"]):
            try:
                raw = await asyncio.to_thread(self.i2c.read, r["bus"], r["addr"], r.get("reg"), r["len"],
                                              r.get("reg_width", 8))
            except I2CError:
                return None
        return raw.hex()

    async def _photo(self, job: LogJob, t_ms: int) -> None:
        ph = job.args["photo"]
        cam = self.cameras.get(ph["camera"])
        if cam is None:
            job.errors += 1
            return
        # 写真の総量が上限の 80% を超えるなら撮らない（行は続ける。F5 の段階①）。
        # 撮ってから超えると、その1枚のせいで段階②（古いバッチを捨てる）が先に働くので、次の1枚の分も見込む
        photo_bytes = self._spool_bytes(photos_only=True)
        if photo_bytes + job.last_photo_bytes > self.spool_limit * PHOTO_PAUSE_RATIO:
            if not job.photos_paused:
                job.photos_paused = True
                log.warning("log %s: spool photos %d bytes > %d%% of %d; photos paused", job.log_id, photo_bytes,
                            PHOTO_PAUSE_RATIO * 100, self.spool_limit)
                self.emit(make_event("spool_photos_paused", {"job_id": job.log_id, "photo_bytes": photo_bytes,
                                                             "limit_bytes": self.spool_limit}, ts=self.clock.now_ms()))
            job.errors += 1
            return
        job.photos_paused = False
        w, h = fit_long_side(ph.get("width", 1280), ph.get("height", 960), self.photo_max_px)
        try:
            data, ctype = await asyncio.to_thread(cam.capture, w, h)
        except CameraError as e:
            log.warning("log %s: photo failed: %s", job.log_id, e)
            job.errors += 1
            return
        blob_id = new_blob_id()
        ext = "jpg" if ctype == "image/jpeg" else "png"
        (job.dir / f"{blob_id}.{ext}").write_bytes(data)
        job.last_photo_bytes = len(data)
        job.photos.append({"t": t_ms, "blob_id": blob_id})
        job.total_photos += 1

    async def _run(self, job: LogJob) -> None:
        loop = asyncio.get_running_loop()
        a = job.args
        t0 = loop.time()
        next_photo = 0
        last_flush = loop.time()
        reason = "completed"
        try:
            k = 0
            while k * a["interval_ms"] < a["duration_ms"]:
                delay = t0 + k * a["interval_ms"] / 1000 - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif -delay * 1000 > a["interval_ms"]:
                    k = int((loop.time() - t0) * 1000 // a["interval_ms"])   # 取りこぼした周期は飛ばす
                    if k * a["interval_ms"] >= a["duration_ms"]:
                        break
                t_ms = k * a["interval_ms"]
                if a["reads"]:
                    row = [t_ms]
                    for r in a["reads"]:
                        v = await self._read(r)
                        job.errors += v is None
                        row.append(v)
                    job.rows.append(row)
                    job.total_rows += 1
                took_photo = False
                if a.get("photo") and t_ms >= next_photo:
                    await self._photo(job, t_ms)
                    next_photo = t_ms + a["photo"]["interval_ms"]
                    took_photo = True
                if took_photo or len(job.rows) >= BATCH_ROWS or loop.time() - last_flush >= BATCH_MS / 1000:
                    self._flush(job)
                    last_flush = loop.time()
                k += 1
        except asyncio.CancelledError:
            reason = job.stop_reason or "stopped"
        except Exception:
            log.exception("log %s failed", job.log_id)
            reason = "error"
        self._flush(job)
        self.jobs.pop(job.log_id, None)
        self.state.jobs.pop(job.log_id, None)
        meta = json.loads((job.dir / "meta.json").read_text())
        (job.dir / "meta.json").write_text(json.dumps({**meta, "ended_ts": self.clock.now_ms(), "end_reason": reason,
                                                       "last_seq": job.seq - 1}))
        self._enforce_spool_limit()
        log.info("log %s ended: %s, %d rows, %d photos", job.log_id, reason, job.total_rows, job.total_photos)
        self.emit(make_event("job_end", {"job_id": job.log_id, "reason": reason, "rows": job.total_rows,
                                         "photos": job.total_photos}, ts=self.clock.now_ms()))

    def _flush(self, job: LogJob) -> None:
        if not job.rows and not job.photos:
            return
        batch = {"seq": job.seq, "rows": job.rows, "photos": job.photos, "errors": job.errors,
                 "started_ts": job.started_ts}
        (job.dir / f"batch-{job.seq:06d}.json").write_text(json.dumps(batch))
        job.rows, job.photos, job.errors = [], [], 0
        job.seq += 1
        if self._in_flight() < SEND_WINDOW:           # 枠がなければ spool に置き、ack で空いたら送る
            self._send_batch(job.log_id, job.dir, batch)
        self._enforce_spool_limit()

    def _started_ts_now(self, log_id: str, stored: int) -> int:
        """開始時刻を「今の壁時計 − 単調時計で測った経過時間」で計算し直す（F6）。

        RTC のない Pi は起動直後の時計がずれていて、NTP の同期で跳ぶ。同期の前に始めた記録でも、同期のあとに
        送るバッチには正しい開始時刻が載る。動いていない記録（再起動のあとの再送）は記録しておいた値のまま。
        """
        job = self.jobs.get(log_id)
        if job is None:
            return stored
        return self.clock.now_ms() - (self.clock.mono_ms() - job.mono_start_ms)

    def _enforce_spool_limit(self) -> None:
        """spool 全体が上限を超えたら、未確認の最も古いバッチ（とその写真）から捨てる（F5 の段階②）。

        ack 済みのバッチはすでに消えているので、残っているのは全部未確認。捨てた範囲は event で残し、
        クラウドは記録の「欠落」として表示する。
        """
        total = self._spool_bytes()
        if total <= self.spool_limit:
            return
        batches = sorted((f.stat().st_mtime, f.parent.name, int(f.stem.split("-")[1]), f)
                         for f in self.spool.glob("*/batch-*.json"))
        dropped: dict[str, list[int]] = {}
        for _, log_id, seq, f in batches:
            if total <= self.spool_limit:
                break
            size = f.stat().st_size
            b = json.loads(f.read_text())
            for ph in b["photos"]:
                for p in f.parent.glob(f"{ph['blob_id']}.*"):
                    size += p.stat().st_size
                    p.unlink(missing_ok=True)
            f.unlink()
            total -= size
            self.sent_at.pop((log_id, seq), None)
            dropped.setdefault(log_id, []).append(seq)
        for log_id, seqs in dropped.items():
            for lo, hi in _runs(sorted(seqs)):
                log.warning("log %s: spool over %d bytes; dropped batches %d-%d", log_id, self.spool_limit, lo, hi)
                self.emit(make_event("spool_rows_dropped", {"job_id": log_id, "seq_from": lo, "seq_to": hi},
                                     ts=self.clock.now_ms()))

    # ---- 送信と再送 --------------------------------------------------------------------

    def _send_batch(self, log_id: str, d: Path, batch: dict) -> bool:
        photos = []
        for ph in batch["photos"]:
            f = next(d.glob(f"{ph['blob_id']}.*"), None)
            if f is None:
                # 手元にない写真はバッチから外す（クラウドは写真が全部そろうまで ack しないので、永久に再送になる）
                continue
            photos.append(ph)
            ctype = "image/jpeg" if f.suffix == ".jpg" else "image/png"
            for m in make_blob_messages(ph["blob_id"], f.read_bytes(), ctype, ts=self.clock.now_ms(),
                                        meta={"job_id": log_id, "t": ph["t"]}):
                if not self.send(m):
                    return False
        msg = {"v": 1, "type": "samples", "id": new_id("s-"), "ts": self.clock.now_ms(), "job_id": log_id,
               "seq": batch["seq"], "rows": batch["rows"], "errors": batch["errors"],
               "started_ts": self._started_ts_now(log_id, batch["started_ts"])}
        if photos:
            msg["photos"] = photos
        if not self.send(msg):
            return False
        self.sent_at[(log_id, batch["seq"])] = time.monotonic()
        return True

    def ack(self, log_id: str, seq: int) -> None:
        """クラウドが log_id の seq のバッチを保存した。そのバッチと写真だけを消す。

        「seq まで」として扱うと、途中で失われて届いていないバッチまで消えてしまう（SPEC_REVIEW.md B1）。
        """
        d = self.spool / log_id
        if not d.is_dir():
            return
        f = d / f"batch-{seq:06d}.json"
        if f.exists():
            b = json.loads(f.read_text())
            for ph in b["photos"]:
                for p in d.glob(f"{ph['blob_id']}.*"):
                    p.unlink(missing_ok=True)
            f.unlink()
        self.sent_at.pop((log_id, seq), None)
        self.resend()                                 # 空いた枠で次のバッチを送る
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        if log_id not in self.jobs and "ended_ts" in meta and not any(d.glob("batch-*.json")):
            shutil.rmtree(d, ignore_errors=True)      # 終わっていて全部届いた記録は spool から消す

    def _in_flight(self) -> int:
        """送ったが ack がまだで、再送の時間（RESEND_AFTER_S）になっていないバッチの数。"""
        now = time.monotonic()
        return sum(1 for t in self.sent_at.values() if now - t < RESEND_AFTER_S)

    def resend(self, force: bool = False) -> int:
        """未確認のバッチを古い順に送る。同時に送るのは SEND_WINDOW 個まで（I3）。送った数を返す。

        ack が返るたびに呼んで次を送る（長い通信断のあとに一斉に飛ばさない）。30秒の再送タイマは、
        送ったが ack のないものにだけかかる。force（つながった直後）は前の接続で送った分を送っていないものとみなす。
        """
        if force:
            self.sent_at.clear()
        n = 0
        room = SEND_WINDOW - self._in_flight()
        for d in sorted(self.spool.iterdir()) if self.spool.exists() else []:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("batch-*.json")):
                if room <= 0:
                    return n
                s = int(f.stem.split("-")[1])
                sent = self.sent_at.get((d.name, s))
                if sent is not None and time.monotonic() - sent < RESEND_AFTER_S:
                    continue
                if not self._send_batch(d.name, d, json.loads(f.read_text())):
                    return n
                n += 1
                room -= 1
        return n

    def start_resender(self) -> None:
        async def loop():
            while True:
                await asyncio.sleep(10)
                try:
                    self.resend()
                except Exception:
                    log.exception("log resend failed")
        if self._resender is None:
            self._resender = asyncio.create_task(loop(), name="log-resender")

    async def stop_resender(self) -> None:
        if self._resender:
            self._resender.cancel()
            self._resender = None

    def _spool_bytes(self, photos_only: bool = False) -> int:
        return sum(f.stat().st_size for f in self.spool.rglob("*")
                   if f.is_file() and not (photos_only and f.suffix == ".json"))

    def pending(self) -> dict:
        """spool に残っている未確認のバッチ（表示・テスト用）。"""
        return {d.name: len(list(d.glob("batch-*.json"))) for d in self.spool.iterdir() if d.is_dir()}


def _runs(seqs: list[int]) -> list[tuple[int, int]]:
    """[1, 2, 3, 7, 8] → [(1, 3), (7, 8)]"""
    out: list[tuple[int, int]] = []
    for x in seqs:
        if out and x == out[-1][1] + 1:
            out[-1] = (out[-1][0], x)
        else:
            out.append((x, x))
    return out
