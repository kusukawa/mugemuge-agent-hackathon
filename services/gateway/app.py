"""Device Gateway（PROTOCOL.md 2〜4章、12章、docs/CLOUD.md 2〜3章）。

Gateway は exec の中身を判定しない（判定はデバイス）。形式の検査と、デバイスへの中継・応答の待ち合わせだけを行う。

    uvicorn --factory services.gateway.app:app_from_env --ws-max-size 65536   # 本番は Dockerfile から
"""
from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from mugemuge_agentd.manifest import ManifestError, manifest_sha256, validate
from mugemuge_agentd.messages import MalformedMessage, message_errors, new_id, parse_message

from ..common.auth import CallerAuth
from ..common.blobs import BlobStore, blob_path, make_blob_store
from ..common.store import Store, make_store
from ..common.tracing import child_span, setup_tracing
from .devstate import DeviceView

log = logging.getLogger("mugemuge.gateway")

SUBPROTOCOL = "mugemuge.v1"
HELLO_TIMEOUT_S = 10
EXEC_GRACE_S = 5
LAST_SEEN_EVERY_MS = 30_000
PAIR_FAILURES_PER_MIN = 20
EXEC_WAIT_FOR_DEVICE_MS = 5000
CLOCK_SKEW_MS = 60_000     # デバイスの時計がこれ以上ずれていたら記録の開始時刻を補正する（F6）
DRAIN_CHECK_S = 5          # 世代交代でデバイスが旧インスタンスに残る時間の上限（F3。Firestore の読み取り 1回/5秒）
DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def now_ms() -> int:
    return int(time.time() * 1000)


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _reject_float(s):
    raise ValueError(f"non-integer number is not allowed: {s}")


def loads_int_only(raw: bytes) -> dict:
    """内部 API の body も、デバイスへ流す前に小数表記を拒否する（承認の args_sha256 を一意に保つため）。"""
    obj = json.loads(raw, parse_float=_reject_float, parse_constant=_reject_float)
    if not isinstance(obj, dict):
        raise ValueError("body must be a JSON object")
    return obj


@dataclass(eq=False)
class Conn:
    ws: WebSocket
    device_id: str
    session_id: str
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    last_seen_written_ms: int = 0
    view: DeviceView | None = None                       # 画面の「現在の状態」の写し
    blob_parts: dict[str, dict] = field(default_factory=dict)   # 組み立て中の写真（blob_id → 断片）
    sent: dict[str, dict] = field(default_factory=dict)  # exec の id → 送った exec（result と突き合わせる）

    async def send(self, msg: dict) -> None:
        await self.ws.send_text(json.dumps(msg, ensure_ascii=False, separators=(",", ":")))

    def fail_pending(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("device disconnected"))
        self.pending.clear()


class Hub:
    """デバイス ID → 今の接続。--max-instances=1 前提でプロセス内に持つ（PROTOCOL.md 12章）。"""

    def __init__(self) -> None:
        self.conns: dict[str, Conn] = {}

    async def attach(self, conn: Conn) -> None:
        old = self.conns.get(conn.device_id)
        self.conns[conn.device_id] = conn
        if old is not None:
            log.info("device %s reconnected; closing previous session %s", conn.device_id, old.session_id)
            old.fail_pending()
            try:
                await old.ws.close(code=4000, reason="replaced by a new connection")
            except Exception:
                pass

    async def wait_for(self, device_id: str, timeout_s: float) -> Conn | None:
        """デバイスがつながるまで最大 timeout_s 待つ。世代交代で旧インスタンスから移ってくる途中の exec のため（F3）。"""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        while (conn := self.conns.get(device_id)) is None and loop.time() < end:
            await asyncio.sleep(0.05)
        return conn

    def detach(self, conn: Conn) -> bool:
        if self.conns.get(conn.device_id) is conn:
            del self.conns[conn.device_id]
            return True
        return False


MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_BLOBS_IN_FLIGHT = 8


# EXEC_FULL_CALLERS にない呼び出し元（Web/API）が /internal/exec で送れる op。安全側の操作と、テスター画面の読み出し。
# Web/API は署名鍵を持つので、承認が要る操作まで送れると、Web/API が侵害されたときに単独で実行できてしまう（SPEC_FIXES_3.md P2）。
# そのため承認（グラント）を付けて送れるのは sys.estop_clear だけ。読み出しはデバイスが自動で許可するものしか通らない
# （16 ビットの reg の i2c.read はデバイスが書き込みとして判定し、承認を求める。PROTOCOL.md 5.1節）
LIMITED_EXEC_OPS = frozenset({"sys.estop", "sys.estop_clear", "log.stop", "i2c.read", "gpio.read"})
LIMITED_APPROVAL_OPS = frozenset({"sys.estop_clear"})


def create_app(store: Store | None = None, caller_auth: CallerAuth | None = None, *,
               heartbeat_ms: int = 3000, instance: str | None = None, blobs: BlobStore | None = None,
               full_exec_callers: set[str] | None = None) -> FastAPI:
    """instance を渡すと「世代交代」を有効にする（Cloud Run では K_REVISION から作る）。

    Cloud Run は新しいリビジョンを出しても、既存の WebSocket を古いリビジョンのインスタンスに残す（最大
    --timeout まで）。そのままだと、デバイスは古い Gateway に、Agent の exec は新しい Gateway に届いて
    device_offline になる。そこで各インスタンスは起動時に自分を leader として記録し、自分より新しい
    leader が現れたら、つないでいるデバイスに bye（server_drain）を送って切る。デバイスは張り直して
    新しい Gateway につながる。
    """
    store = store or make_store()
    blobs = blobs or make_blob_store()
    started_ms = now_ms()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if instance is not None:
            await store.set_gateway_leader(instance, started_ms)
            log.info("gateway instance %s started; recorded as leader", instance)
            task = asyncio.create_task(_drain_loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="mugemuge-gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    caller_auth = caller_auth or CallerAuth.from_env("GATEWAY_AUDIENCE", "INTERNAL_CALLERS")
    if full_exec_callers is None:
        # 何でも送れる呼び出し元（Agent のサービスアカウント）。書かれていなければ誰も何でもは送れない（安全側に倒す）。
        # 開発モードの呼び出し元 "dev" は何でも送れる
        full_exec_callers = {e.strip() for e in os.environ.get("EXEC_FULL_CALLERS", "").split(",") if e.strip()}
        if caller_auth.insecure:
            full_exec_callers.add("dev")
    hub = Hub()
    pair_failures: collections.deque[int] = collections.deque()
    app.state.hub = hub
    app.state.store = store

    async def check_drain() -> int:
        """自分より新しい leader がいれば、つないでいるデバイスを全部手放す。手放した数を返す。"""
        leader = await store.get_gateway_leader()
        if instance is None or leader is None or leader["instance"] == instance \
                or leader["started_ms"] <= started_ms or not hub.conns:
            return 0
        conns = list(hub.conns.values())
        log.info("newer gateway %s is up; draining %d device(s)", leader["instance"], len(conns))
        for conn in conns:
            try:
                await conn.send({"v": 1, "type": "bye", "id": new_id("b-"), "ts": now_ms(), "reason": "server_drain"})
                await conn.ws.close(code=1012, reason="server_drain")
            except Exception:
                pass
        return len(conns)

    async def _drain_loop() -> None:
        while True:
            await asyncio.sleep(DRAIN_CHECK_S)
            try:
                await check_drain()
            except Exception:
                log.exception("drain check failed")

    app.state.check_drain = check_drain

    # ---- デバイス -------------------------------------------------------------------

    @app.websocket("/v1/device")
    async def device_ws(ws: WebSocket):
        auth = ws.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        dev = await store.device_by_token(token_sha256(token)) if token.startswith("mgd_") else None
        if dev is None:
            log.warning("device connection rejected: unknown token from %s", ws.client)
            await ws.close(code=4401)          # accept 前に閉じる＝ハンドシェイクを 403 で断る
            return
        offered = ws.scope.get("subprotocols") or []
        await ws.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in offered else None)

        async def bye(reason: str) -> None:
            await ws.send_text(json.dumps({"v": 1, "type": "bye", "id": new_id("b-"), "ts": now_ms(),
                                           "reason": reason}))
            await ws.close(code=1008)

        try:
            hello = parse_message(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT_S))
        except (asyncio.TimeoutError, MalformedMessage, ValueError, WebSocketDisconnect) as e:
            log.warning("bad hello from %s: %s", dev["device_id"], e)
            await ws.close(code=1008)
            return
        if hello["type"] != "hello" or hello["device_id"] != dev["device_id"]:
            log.warning("device_id mismatch: token is for %s, hello says %s", dev["device_id"], hello.get("device_id"))
            await bye("device_mismatch")
            return
        if 1 not in hello["protocols"]:
            await bye("protocol_unsupported")
            return
        try:
            validate(hello["manifest"])
            ok_hash = manifest_sha256(hello["manifest"]) == hello["manifest_sha256"]
        except (ManifestError, TypeError):
            ok_hash = False
        if not ok_hash:
            log.warning("manifest of %s is invalid or does not match its sha256", dev["device_id"])
            await ws.close(code=1008)
            return

        conn = Conn(ws, dev["device_id"], "ses_" + new_id())
        conn.view = DeviceView(hello["manifest"], hello["state"])
        await hub.attach(conn)
        await conn.send({"v": 1, "type": "welcome", "id": new_id("w-"), "ts": now_ms(), "re": hello["id"],
                         "session_id": conn.session_id, "protocol": 1, "heartbeat_ms": heartbeat_ms,
                         "server_ts": now_ms()})
        await store.update_device(conn.device_id, {
            "online": True, "session_id": conn.session_id, "manifest": hello["manifest"],
            "manifest_sha256": hello["manifest_sha256"], "state": conn.view.state(), "i2c_seen": {},
            "boot_id": hello["boot_id"],
            "agent": hello["agent"], "last_seen_ms": now_ms(), "connected_ms": now_ms()})
        log.info("device %s connected: session %s", conn.device_id, conn.session_id)
        await _expire_session_approvals(conn.device_id, current=conn.session_id)
        # デーモンの再起動・電源断で終わった記録は job_end が届かないので、ここで「中断」にする（SPEC_REVIEW.md B6）
        running = set(hello["state"].get("jobs", []))
        for lg in await store.list_logs(conn.device_id):
            if lg.get("status") == "running" and lg["log_id"] not in running:
                await store.put_log(conn.device_id, lg["log_id"], {"status": "interrupted",
                                                                   "ended_ts": lg.get("last_batch_ts") or now_ms()})
                log.info("device %s: log %s was interrupted (not running after reconnect)", conn.device_id, lg["log_id"])
        pinger = asyncio.create_task(_ping(conn, heartbeat_ms))
        try:
            while True:
                text = await ws.receive_text()
                await _on_device_message(conn, text)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("device %s: receive loop failed", conn.device_id)
        finally:
            pinger.cancel()
            conn.fail_pending()
            if hub.detach(conn):
                await store.update_device(conn.device_id, {"online": False, "last_seen_ms": now_ms()})
            await _expire_session_approvals(conn.device_id, ended=conn.session_id)
            log.info("device %s disconnected (session %s)", conn.device_id, conn.session_id)

    async def _expire_session_approvals(device_id: str, *, ended: str | None = None, current: str | None = None) -> None:
        """終わったセッション向けの承認待ちを expired（reason=session_ended）にする（F7）。

        承認はセッションに縛られるので、セッションが変わったあとに承認しても wrong_session になるだけ。
        押せないカードを残さない。ended はそのセッションだけ、current は「それ以外の全セッション」。
        """
        try:
            for a in await store.list_approvals("pending"):
                req = a.get("request") or {}
                if req.get("device_id") != device_id:
                    continue
                sid = req.get("session_id")
                if (ended is not None and sid == ended) or (current is not None and sid != current):
                    if await store.decide_approval(a["approval_id"], {"status": "expired", "reason": "session_ended",
                                                                      "decided_ms": now_ms()}):
                        log.info("approval %s expired: session %s of %s ended", a["approval_id"], sid, device_id)
        except Exception:
            log.exception("device %s: could not expire approvals of ended sessions", device_id)

    async def _ping(conn: Conn, interval_ms: int) -> None:
        n = 0
        while True:
            await asyncio.sleep(interval_ms / 1000)
            n += 1
            try:
                await conn.send({"v": 1, "type": "ping", "id": f"p-{n:08d}", "ts": now_ms()})
            except Exception:
                return

    async def _on_device_message(conn: Conn, text: str) -> None:
        try:
            msg = parse_message(text)
        except (MalformedMessage, ValueError) as e:
            log.warning("device %s sent a malformed frame: %s", conn.device_id, str(e)[:200])
            return
        t = msg["type"]
        if t == "result":
            req = (msg.get("error") or {}).get("request")
            if req is not None and req.get("device_id") == conn.device_id:
                # 承認カードに出せるのは、デバイス自身が発行した request だけ（Web/API がここを引く）
                await store.put_device_request(conn.device_id, req, msg["ts"])
            fut = conn.pending.pop(msg["re"], None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            sent = conn.sent.pop(msg["re"], None)
            if sent is not None and not msg.get("duplicate") and conn.view.apply_result(sent, msg):
                await _save_view(conn)
            if sent is not None and sent["op"] == "log.start" and msg.get("ok") and not msg.get("duplicate"):
                d = msg["data"]
                started, skewed = _device_time(d["started_ts"], msg["ts"])
                await store.put_log(conn.device_id, d["log_id"], {
                    "name": sent["args"]["name"], "args": sent["args"], "columns": d["columns"],
                    "started_ts": started, "started_ts_final": not skewed,
                    **({"clock_corrected": True} if skewed else {}), "status": "running", "rows": 0, "photos": 0,
                    "parts": {r["name"]: b["part"] for r in sent["args"]["reads"]
                              for b in [conn.view.bindings.get((r["bus"], r["addr"]))] if b},
                    "approved_by": (msg.get("policy") or {}).get("decision")})
        elif t == "blob":
            await _on_blob(conn, msg)
        elif t == "samples" and msg["job_id"].startswith("log_"):
            await _on_log_batch(conn, msg)
        elif t == "samples":
            await store.add_samples(conn.device_id, msg)
        elif t == "event":
            log.info("device %s event %s %s", conn.device_id, msg["event"], msg["detail"])
            await store.add_event(conn.device_id, msg)
            if conn.view.apply_event(msg):
                await _save_view(conn)
            d = msg.get("detail") or {}
            if msg["event"] == "job_end" and str(d.get("job_id", "")).startswith("log_"):
                await store.put_log(conn.device_id, d["job_id"], {"status": d.get("reason", "completed"),
                                                                  "ended_ts": msg["ts"]})
            elif msg["event"] == "spool_rows_dropped" and str(d.get("job_id", "")).startswith("log_"):
                # Pi の spool が満杯で捨てたバッチの範囲。記録ページに「欠落」として出す（F5）
                cur = await store.get_log(conn.device_id, d["job_id"]) or {}
                dropped = (cur.get("dropped") or []) + [[d["seq_from"], d["seq_to"]]]
                await store.put_log(conn.device_id, d["job_id"], {"dropped": dropped[-100:]})
            elif msg["event"] == "spool_photos_paused" and str(d.get("job_id", "")).startswith("log_"):
                await store.put_log(conn.device_id, d["job_id"], {"photos_paused_ts": msg["ts"]})
        elif t == "bye":
            log.info("device %s says bye: %s", conn.device_id, msg["reason"])
        if now_ms() - conn.last_seen_written_ms > LAST_SEEN_EVERY_MS:
            conn.last_seen_written_ms = now_ms()
            await store.update_device(conn.device_id, {"last_seen_ms": conn.last_seen_written_ms})

    async def _on_blob(conn: Conn, msg: dict) -> None:
        """写真の断片を組み立てる。sha256 と大きさを確かめてから保管する。同じ blob_id の再送は上書き。"""
        bid = msg["blob_id"]
        part = conn.blob_parts.get(bid)
        if part is None:
            if len(conn.blob_parts) >= MAX_BLOBS_IN_FLIGHT:
                conn.blob_parts.pop(next(iter(conn.blob_parts)))      # 途中で途切れた古いものを捨てる
            part = conn.blob_parts[bid] = {"chunks": {}, "size": 0}
        data = base64.b64decode(msg["data"])
        part["size"] += len(data)
        if part["size"] > MAX_BLOB_BYTES:
            conn.blob_parts.pop(bid, None)
            log.warning("device %s: blob %s is too large; dropped", conn.device_id, bid)
            return
        part["chunks"][msg["seq"]] = data
        if not msg["last"]:
            return
        conn.blob_parts.pop(bid, None)
        whole = b"".join(part["chunks"][i] for i in sorted(part["chunks"]))
        if len(whole) != msg["total_bytes"] or hashlib.sha256(whole).hexdigest() != msg["sha256"]:
            log.warning("device %s: blob %s is corrupt (size or sha256 mismatch); dropped", conn.device_id, bid)
            return
        if await store.get_blob_meta(conn.device_id, bid):
            return                       # 再送。保管済みなので上書きしない（Gateway の SA に上書きの権限を持たせない）
        path = blob_path(conn.device_id, bid, msg["content_type"])
        await blobs.put(path, whole, msg["content_type"])
        await store.put_blob_meta(conn.device_id, bid, {"blob_id": bid, "path": path, "content_type": msg["content_type"],
                                                        "bytes": len(whole), "sha256": msg["sha256"], "ts": msg["ts"],
                                                        "meta": msg.get("meta") or {}})

    def _device_time(value: int, msg_ts: int) -> tuple[int, bool]:
        """デバイスの壁時計の時刻を、受信時刻との差でサーバの時計に合わせる（F6）。

        msg_ts は送ったときのデバイスの壁時計。受信時刻と CLOCK_SKEW_MS より離れていれば、デバイスの時計がずれている
        （RTC のない Pi の起動直後、NTP 同期の前）とみなし、その差だけずらす。戻り値は (時刻, ずれていたか)。
        """
        skew = now_ms() - msg_ts
        if abs(skew) > CLOCK_SKEW_MS:
            return value + skew, True
        return value, False

    async def _on_log_batch(conn: Conn, msg: dict) -> None:
        """記録ジョブのバッチを保存し、ack を返す（デバイスはそこまでを spool から消す）。"""
        log_id = msg["job_id"]
        doc = {"seq": msg["seq"], "ts": msg["ts"], "started_ts": msg.get("started_ts"), "errors": msg.get("errors", 0),
               "rows": [{"t": r[0], "v": list(r[1:])} for r in msg["rows"]], "photos": msg.get("photos", [])}
        new = await store.put_log_batch(conn.device_id, log_id, doc)
        meta = await store.get_log(conn.device_id, log_id) or {}
        fields: dict = {"last_batch_ts": msg["ts"]}
        # 開始時刻は、デバイスの時計が合っているバッチ（|ts − 受信時刻| ≤ 60秒）が来たときに1回だけ確定する。
        # それまでは受信時刻との差で補正した値を仮に使う。補正したら clock_corrected を付ける（F6）
        if msg.get("started_ts") and not meta.get("started_ts_final"):
            started, skewed = _device_time(msg["started_ts"], msg["ts"])
            fields["started_ts"] = started
            fields["started_ts_final"] = not skewed
            if skewed or (meta.get("started_ts") and abs(started - meta["started_ts"]) > CLOCK_SKEW_MS):
                fields["clock_corrected"] = True
        if new:
            fields["rows"] = meta.get("rows", 0) + len(msg["rows"])
            fields["photos"] = meta.get("photos", 0) + len(msg.get("photos", []))
        if msg["rows"]:
            last = msg["rows"][-1]
            if last[0] >= (meta.get("last_row") or {}).get("t", -1):
                fields["last_row"] = {"t": last[0], "v": list(last[1:])}
        if msg.get("photos"):
            p = msg["photos"][-1]
            if p["t"] >= (meta.get("last_photo") or {}).get("t", -1):
                fields["last_photo"] = p
        await store.put_log(conn.device_id, log_id, fields)
        # バッチが指す写真が全部保管済みのときだけ ack を返す。足りなければ返さず、デバイスの再送（写真ごと）を待つ。
        # 先に ack すると、デバイスは写真のファイルも消すので、写真がどこにも残らない（SPEC_REVIEW.md B5）
        for ph in msg.get("photos", []):
            if not await store.get_blob_meta(conn.device_id, ph["blob_id"]):
                log.warning("device %s: %s batch %d refers to photo %s that is not stored yet; no ack",
                            conn.device_id, log_id, msg["seq"], ph["blob_id"])
                return
        await conn.send({"v": 1, "type": "ack", "id": new_id("a-"), "ts": now_ms(), "re": msg["id"],
                         "job_id": log_id, "seq": msg["seq"]})

    async def _save_view(conn: Conn) -> None:
        await store.update_device(conn.device_id, {"state": conn.view.state(), "i2c_seen": conn.view.i2c_seen,
                                                   "state_ts": now_ms()})

    @app.post("/v1/pair")
    async def pair(request: Request):
        t = now_ms()
        while pair_failures and pair_failures[0] < t - 60_000:
            pair_failures.popleft()
        if len(pair_failures) >= PAIR_FAILURES_PER_MIN:
            raise HTTPException(429, "too many failed pairing attempts; wait a minute")
        try:
            body = loads_int_only(await request.body())
            code, device_id = body["code"], body["device_id"]
        except (ValueError, KeyError):
            raise HTTPException(400, "body must be {code, device_id}") from None
        if not (isinstance(code, str) and code.isdigit() and len(code) == 6) or not (
                isinstance(device_id, str) and DEVICE_ID_RE.match(device_id)):
            raise HTTPException(400, "invalid code or device_id")
        owner = await store.consume_pairing_code(code, t)
        if owner is None:
            pair_failures.append(t)
            raise HTTPException(403, "invalid or expired pairing code")
        existing = await store.get_device(device_id)
        if existing and existing.get("owner") not in (None, owner):
            raise HTTPException(409, "device is paired to another user")
        token = "mgd_" + secrets.token_urlsafe(32)
        await store.set_device_token(device_id, owner, token_sha256(token))
        old = hub.conns.get(device_id)
        if old is not None:      # 古いトークンでつながっている接続は切る
            hub.detach(old)
            old.fail_pending()
            await old.ws.close(code=4401, reason="token replaced")
        log.info("device %s paired by %s", device_id, owner)
        return {"token": token}

    # ---- 内部 API ---------------------------------------------------------------------

    @app.post("/internal/exec")
    async def internal_exec(request: Request, caller: str = Depends(caller_auth)):
        try:
            body = loads_int_only(await request.body())
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        device_id = body.get("device_id")
        msg = {"v": 1, "type": "exec", "id": body.get("id") or new_id("x-"), "ts": now_ms()}
        for k in ("op", "args", "run_id", "step_id", "traceparent", "deadline_ms", "approval"):
            if k in body and body[k] is not None:
                msg[k] = body[k]
        errs = message_errors(msg)
        if errs or not isinstance(device_id, str):
            return JSONResponse({"error": "bad_request", "detail": errs or ["device_id is required"]}, 400)
        if caller not in full_exec_callers and (
                msg["op"] not in LIMITED_EXEC_OPS or ("approval" in msg and msg["op"] not in LIMITED_APPROVAL_OPS)):
            log.warning("exec %s%s from %s refused: this caller may only send %s, with approval only %s", msg["op"],
                        " with approval" if "approval" in msg else "", caller,
                        ", ".join(sorted(LIMITED_EXEC_OPS)), ", ".join(sorted(LIMITED_APPROVAL_OPS)))
            return JSONResponse({"error": "op_not_allowed_for_caller", "op": msg["op"]}, 403)
        # 未接続ならすぐには諦めず、min(deadline_ms, 5秒) まで接続を待つ（新しいリビジョンに exec が先に来て、
        # デバイスがまだ旧インスタンスにいる間の窓を埋める: F3）
        conn = await hub.wait_for(device_id, min(msg.get("deadline_ms", 2000), EXEC_WAIT_FOR_DEVICE_MS) / 1000)
        if conn is None:
            return JSONResponse({"error": "device_offline", "device_id": device_id}, 503)

        # 「デバイスとの往復」のスパン。デバイスにはこのスパンを親として渡す（監査ログと result に残る）
        with child_span(f"device.exec {msg['op']}", msg.get("traceparent"), {
                "mugemuge.device_id": device_id, "mugemuge.op": msg["op"], "mugemuge.exec_id": msg["id"],
                "mugemuge.run_id": msg["run_id"], "mugemuge.step_id": msg["step_id"],
                "mugemuge.approved": "approval" in msg}) as (span, tp):
            if tp:
                msg["traceparent"] = tp
            fut = conn.pending.get(msg["id"])
            if fut is None:
                fut = asyncio.get_running_loop().create_future()
                conn.pending[msg["id"]] = fut
                conn.sent[msg["id"]] = {"op": msg["op"], "args": msg["args"]}
                while len(conn.sent) > 256:                 # 応答のなかった exec を溜めこまない
                    conn.sent.pop(next(iter(conn.sent)))
                try:
                    await conn.send(msg)
                except Exception:
                    conn.pending.pop(msg["id"], None)
                    span.set_attribute("mugemuge.error", "device_offline")
                    return JSONResponse({"error": "device_offline", "device_id": device_id}, 503)
            log.info("exec %s %s → %s by %s (run %s, step %s)", msg["id"], msg["op"], device_id, caller,
                     msg["run_id"], msg["step_id"])
            timeout = msg.get("deadline_ms", 2000) / 1000 + EXEC_GRACE_S
            try:
                result = await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                conn.pending.pop(msg["id"], None)
                span.set_attribute("mugemuge.error", "device_timeout")
                return JSONResponse({"error": "device_timeout", "id": msg["id"]}, 504)
            except ConnectionError:
                span.set_attribute("mugemuge.error", "device_offline")
                return JSONResponse({"error": "device_offline", "device_id": device_id, "id": msg["id"]}, 503)
            pol = result.get("policy", {})
            span.set_attribute("mugemuge.decision", pol.get("decision", ""))
            span.set_attribute("mugemuge.rule", pol.get("rule", ""))
            span.set_attribute("mugemuge.ok", bool(result.get("ok")))
            if not result.get("ok"):
                span.set_attribute("mugemuge.error", result["error"]["code"])
            if "duration_ms" in result:
                span.set_attribute("mugemuge.device_duration_ms", result["duration_ms"])
            return result

    @app.get("/internal/devices/{device_id}")
    async def internal_device(device_id: str, caller: str = Depends(caller_auth)):
        d = await store.get_device(device_id)
        if d is None:
            raise HTTPException(404, "unknown device")
        d.pop("token_sha256", None)
        d["online"] = device_id in hub.conns
        return d

    @app.get("/internal/devices/{device_id}/samples/{job_id}")
    async def internal_samples(device_id: str, job_id: str, caller: str = Depends(caller_auth)):
        """計測ジョブの生データ（行は {"t", "v"}）。換算は Agent 側のデコーダが行う。"""
        docs = await store.list_samples(device_id, job_id)
        rows = [r for d in docs for r in d["rows"]]
        seqs = [d["seq"] for d in docs]
        missing = sorted(set(range(max(seqs) + 1)) - set(seqs)) if seqs else []
        return {"device_id": device_id, "job_id": job_id, "rows": rows, "batches": len(docs),
                "missing_seq": missing, "errors": sum(d.get("errors", 0) for d in docs)}

    @app.get("/internal/devices/{device_id}/events")
    async def internal_events(device_id: str, limit: int = 50, caller: str = Depends(caller_auth)):
        return {"device_id": device_id, "events": await store.list_events(device_id, min(max(limit, 1), 200))}

    @app.get("/internal/devices/{device_id}/logs")
    async def internal_logs(device_id: str, caller: str = Depends(caller_auth)):
        return {"device_id": device_id, "logs": await store.list_logs(device_id)}

    @app.get("/internal/devices/{device_id}/logs/{log_id}")
    async def internal_log(device_id: str, log_id: str, caller: str = Depends(caller_auth)):
        meta = await store.get_log(device_id, log_id)
        if meta is None:
            raise HTTPException(404, "unknown log")
        return meta

    @app.post("/internal/devices/{device_id}/logs/{log_id}/decoders")
    async def internal_log_decoders(device_id: str, log_id: str, request: Request, caller: str = Depends(caller_auth)):
        """Agent が記録の列ごとの換算方法（デコーダと校正値）を登録する。画面の換算に使う。"""
        body = loads_int_only(await request.body())
        decs = body.get("decoders")
        if not isinstance(decs, dict):
            raise HTTPException(400, "decoders must be an object")
        await store.put_log(device_id, log_id, {"decoders": decs})
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True, "devices_online": len(hub.conns)}

    return app


def app_from_env() -> FastAPI:
    """uvicorn --factory から呼ぶ。環境変数がそろっていなければ起動を止める。"""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(levelname)s %(name)s: %(message)s")
    if os.environ.get("MUGEMUGE_TRACE", "1") == "1" and os.environ.get("K_SERVICE"):
        setup_tracing()
    rev = os.environ.get("K_REVISION")
    instance = f"{rev}:{secrets.token_hex(4)}" if rev else None
    return create_app(heartbeat_ms=int(os.environ.get("HEARTBEAT_MS", "3000")), instance=instance)
