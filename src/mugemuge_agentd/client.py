"""Gateway への WebSocket クライアント（PROTOCOL.md 2〜4章）。

接続は常にデバイスから張る。hello → welcome のあとで送受信を始め、切れたら指数バックオフで張り直す。
受信フレームは必ず messages.parse_message() を通す（小数表記の拒否とスキーマ検証）。
クラウドから何か受け取るたびに safety.touch() を呼ぶ（デッドマンの判定）。

送信は1本の送信タスクに集める。welcome を受けるまで（＝切断中）は event と samples を保持しておき、
welcome の直後に event → samples の順に送る。result は保持しない（クラウドが同じ id で再送すれば
executor が前回の結果を返す）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import deque
from typing import Callable
from urllib.parse import urlparse

import websockets
from websockets.asyncio.client import ClientConnection, connect

from . import PROTOCOL_VERSION, __version__
from .clock import Clock
from .executor import Executor, hello_state
from .manifest import manifest_sha256
from .messages import MalformedMessage, new_id, parse_message
from .policy import DeviceState
from .safety import Safety

log = logging.getLogger(__name__)

SUBPROTOCOL = "mugemuge.v1"
MAX_FRAME = 64 * 1024
BACKOFF_S = (1, 2, 4, 8, 16, 30)
JITTER = 0.2
WELCOME_TIMEOUT_S = 10
HELD_EVENTS_MAX = 256
HELD_SAMPLES_MAX = 600
KNOWN_OPS = {"sys.info", "sys.estop", "sys.estop_clear", "i2c.scan", "i2c.read", "i2c.write", "i2c.bind",
             "i2c.unbind", "gpio.read", "gpio.write", "sample.start", "sample.stop", "camera.capture", "log.start",
             "log.stop"}


def backoff_delay(attempt: int, schedule: tuple[float, ...] = BACKOFF_S) -> float:
    base = schedule[min(attempt, len(schedule) - 1)]
    return base * random.uniform(1 - JITTER, 1 + JITTER)


def check_gateway_url(url: str) -> None:
    """wss:// のみ。開発用の ws:// は loopback に限る。"""
    u = urlparse(url)
    if u.scheme == "wss":
        return
    if u.scheme == "ws" and u.hostname in ("localhost", "127.0.0.1", "::1"):
        return
    raise ValueError(f"gateway url must be wss:// (ws:// is allowed only for loopback): {url}")


def read_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        import uuid

        return str(uuid.uuid4())


class Client:
    def __init__(
        self,
        manifest: dict,
        state: DeviceState,
        executor: Executor,
        safety: Safety,
        clock: Clock,
        *,
        url: str,
        token: str | None,
        backoff: tuple[float, ...] = BACKOFF_S,
    ):
        check_gateway_url(url)
        self.manifest = manifest
        self.manifest_hash = manifest_sha256(manifest)
        self.state = state
        self.executor = executor
        self.safety = safety
        self.clock = clock
        self.url = url
        self.token = token
        self.backoff = backoff
        self.boot_id = read_boot_id()
        self.ready = False                       # welcome を受けてから切れるまで True
        self._ws: ClientConnection | None = None
        self._q: asyncio.Queue[dict] | None = None
        self._held_events: deque[dict] = deque(maxlen=HELD_EVENTS_MAX)
        self._held_samples: deque[dict] = deque(maxlen=HELD_SAMPLES_MAX)
        self._inflight: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self.on_ready: Callable[[], None] | None = None   # テスト用のフック
        self.on_connected: list[Callable[[], None]] = []  # welcome のあと（記録ジョブの再送）
        self.on_ack: Callable[[str, int], None] | None = None

    # ---- 送信 --------------------------------------------------------------------

    def send_now(self, msg: dict) -> bool:
        """つながっていれば送る（True）。つながっていなければ何もしない（False）。記録ジョブの spool が使う。"""
        if self.ready and self._q is not None:
            self._q.put_nowait(msg)
            return True
        return False

    def emit(self, msg: dict) -> None:
        """どこからでも呼べる非ブロッキングの送信。safety / sampler / executor に渡す。"""
        if self.ready and self._q is not None:
            self._q.put_nowait(msg)
        else:
            self._hold(msg)

    def _hold(self, msg: dict) -> None:
        if msg["type"] == "event":
            if len(self._held_events) == self._held_events.maxlen:
                log.warning("held event buffer full; dropping oldest")
            self._held_events.append(msg)
        elif msg["type"] == "samples" and not msg["job_id"].startswith("log_"):
            # 記録ジョブのバッチは保持しない（spool から記録ジョブ自身が再送する。保持すると二重になる: B9）
            self._held_samples.append(msg)
        # result / pong は保持しない

    async def _sender(self, ws: ClientConnection, q: asyncio.Queue) -> None:
        while True:
            msg = await q.get()
            try:
                await ws.send(json.dumps(msg, ensure_ascii=False, separators=(",", ":")))
            except websockets.ConnectionClosed:
                self._hold(msg)
                return

    # ---- 接続ループ ----------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                welcomed = await self._connect_once()
                if welcomed:
                    attempt = 0
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException, MalformedMessage) as e:
                log.warning("connection to %s failed: %s", self.url, e)
            if self._stop.is_set():
                break
            delay = backoff_delay(attempt, self.backoff)
            attempt += 1
            log.info("reconnecting in %.1f s", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _connect_once(self) -> bool:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        async with connect(self.url, subprotocols=[SUBPROTOCOL], additional_headers=headers,
                           max_size=MAX_FRAME, open_timeout=10, compression=None) as ws:
            self._ws = ws
            try:
                return await self._session(ws)
            finally:
                self._ws = None

    async def _session(self, ws: ClientConnection) -> bool:
        hello = {
            "v": 1, "type": "hello", "id": new_id("h-"), "ts": self.clock.now_ms(),
            "device_id": self.manifest["device"]["id"],
            "agent": {"name": "mugemuge-agentd", "version": __version__},
            "protocols": [PROTOCOL_VERSION],
            "boot_id": self.boot_id,
            "manifest": self.manifest,
            "manifest_sha256": self.manifest_hash,
            "state": hello_state(self.state, self.safety),
        }
        await ws.send(json.dumps(hello, ensure_ascii=False, separators=(",", ":")))

        text = await asyncio.wait_for(ws.recv(), timeout=WELCOME_TIMEOUT_S)
        received = self.clock.mono_ms()
        self.safety.touch()
        first = parse_message(text)
        if first["type"] == "bye":
            log.error("gateway refused the connection: %s", first["reason"])
            return False
        if first["type"] != "welcome" or first["re"] != hello["id"]:
            raise MalformedMessage(f"expected welcome for {hello['id']}, got {first['type']}")
        self._on_welcome(first, received)

        q: asyncio.Queue[dict] = asyncio.Queue()
        for held in (self._held_events, self._held_samples):
            while held:
                q.put_nowait(held.popleft())
        self._q = q
        self.ready = True
        sender = asyncio.create_task(self._sender(ws, q), name="ws-sender")
        if self.on_ready:
            self.on_ready()
        for cb in self.on_connected:
            try:
                cb()
            except Exception:
                log.exception("on_connected callback failed")
        try:
            async for text in ws:
                self.safety.touch()
                self._on_frame(text)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.ready = False
            self._q = None
            self.state.session_id = None      # 前のセッションの承認はもう通らない
            sender.cancel()
            while not q.empty():
                self._hold(q.get_nowait())
            log.warning("disconnected from gateway (code=%s)", ws.close_code)
        return True

    def _on_welcome(self, w: dict, received_mono_ms: int) -> None:
        self.state.session_id = w["session_id"]
        self.clock.sync(w["server_ts"], received_mono_ms)
        deadman = self.safety.deadman_ms
        if w["heartbeat_ms"] * 2 > deadman:
            log.warning("heartbeat_ms %d is more than half of deadman_ms %d; deadman may trip spuriously",
                        w["heartbeat_ms"], deadman)
        log.info("connected: session %s, heartbeat %d ms, clock offset %+d ms",
                 w["session_id"], w["heartbeat_ms"], self.clock.offset_ms)

    # ---- 受信 --------------------------------------------------------------------

    def _on_frame(self, text: str | bytes) -> None:
        received_mono = self.clock.mono_ms()
        if isinstance(text, bytes):
            log.warning("ignoring binary frame")
            return
        try:
            msg = parse_message(text)
        except (MalformedMessage, ValueError) as e:
            self._on_malformed(text, str(e))
            return
        t = msg["type"]
        if t == "ping":
            self.emit({"v": 1, "type": "pong", "id": new_id("q-"), "ts": self.clock.now_ms(), "re": msg["id"]})
        elif t == "exec":
            task = asyncio.create_task(self._exec(msg, received_mono), name=f"exec-{msg['id']}")
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        elif t == "bye":
            log.warning("gateway says bye: %s", msg["reason"])
        elif t == "ack":
            if self.on_ack:
                self.on_ack(msg["job_id"], msg["seq"])
        elif t == "pong":
            pass
        else:
            log.warning("ignoring unexpected %s from gateway", t)

    async def _exec(self, msg: dict, received_mono: int) -> None:
        result = await self.executor.handle(msg, received_mono)
        self.emit(result)

    def _on_malformed(self, text: str, why: str) -> None:
        """exec らしいものには bad_request / unknown_op を返す。それ以外は記録して捨てる。"""
        try:
            loose = json.loads(text)
        except ValueError:
            loose = None
        if not (isinstance(loose, dict) and loose.get("type") == "exec" and isinstance(loose.get("id"), str)
                and 1 <= len(loose["id"]) <= 64):
            log.warning("dropping malformed frame: %s", why[:200])
            return
        op = loose.get("op")
        exec_like = {"id": loose["id"], "op": op if isinstance(op, str) else "exec",
                     "args": loose.get("args") if isinstance(loose.get("args"), dict) else None}
        for k in ("run_id", "step_id"):
            if isinstance(loose.get(k), str):
                exec_like[k] = loose[k][:64]
        code = "unknown_op" if isinstance(op, str) and op not in KNOWN_OPS else "bad_request"
        self.emit(self.executor.reject_malformed(exec_like, code, why))

    # ---- 終了 --------------------------------------------------------------------

    async def say_bye(self, reason: str = "shutdown") -> None:
        ws = self._ws
        if ws is None:
            return
        # 先に積まれている job_end などを送り切ってから bye（最大1秒）
        for _ in range(20):
            if self._q is None or self._q.empty():
                break
            await asyncio.sleep(0.05)
        try:
            await ws.send(json.dumps({"v": 1, "type": "bye", "id": new_id("b-"), "ts": self.clock.now_ms(),
                                      "reason": reason}))
            await ws.close()
        except websockets.WebSocketException:
            pass
