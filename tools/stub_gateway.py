"""開発用の最小ゲートウェイ。

WebSocket で待ち受け、hello に welcome を返し、心拍の ping を送る。標準入力から op を打つと exec を送り、
結果を表示する。デバイスが送ってきたメッセージはすべてスキーマで検査し、違反があれば目立つように出す。

    PYTHONPATH=src python3 tools/stub_gateway.py [--auto-approve] [--port 8765]

承認が要る操作は `approve` で（--auto-approve なら自動で）例専用の鍵で署名したグラントを付けて再送する。
例専用の鍵はシードが公開されているので、examples/manifest.pi-grove.yaml の公開鍵とだけ組み合わせて使う。
本番の鍵ではない。秘密鍵をファイルに保存することもしない。

テストからは StubGateway クラスを直接使う（tests/test_integration.py）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from websockets.asyncio.server import ServerConnection, serve  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from mugemuge_agentd.approval import sign_grant  # noqa: E402
from mugemuge_agentd.messages import message_errors, new_id, parse_message  # noqa: E402

# tools/gen_examples.py と同じ例専用の鍵（examples/manifest.pi-grove.yaml の kid: api-2026-10 に対応）
EXAMPLE_KEY = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"mugemuge-example-key-do-not-use").digest())
EXAMPLE_KID = "api-2026-10"


def now_ms() -> int:
    return int(time.time() * 1000)


class StubGateway:
    def __init__(self, host="127.0.0.1", port=8765, *, key: Ed25519PrivateKey = EXAMPLE_KEY, kid=EXAMPLE_KID,
                 heartbeat_ms=3000, auto_approve=False, on_message=None):
        self.host, self.port = host, port
        self.key, self.kid = key, kid
        self.heartbeat_ms = heartbeat_ms
        self.auto_approve = auto_approve
        self.on_message = on_message           # デバイスからのメッセージごとに呼ぶ（CLI の表示用）
        self.server = None
        self.ws: ServerConnection | None = None
        self.hello: dict | None = None
        self.session_id: str | None = None
        self.muted = False                     # True の間は ping を送らない（デッドマンの確認用）
        self.inbox: list[dict] = []            # デバイスから届いた全メッセージ
        self.violations: list[tuple[dict, list[str]]] = []
        self.connected = asyncio.Event()
        self.last_approval_request: tuple[dict, dict] | None = None   # (元の exec, request)
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0

    # ---- サーバ ------------------------------------------------------------------

    async def start(self):
        self.server = await serve(self._handler, self.host, self.port, subprotocols=["mugemuge.v1"],
                                  max_size=64 * 1024, compression=None)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        """待ち受けを止め、今の接続も切る（ゲートウェイが落ちた状態を作る）。"""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.ws = None
        self.connected.clear()

    async def _handler(self, ws: ServerConnection):
        try:
            hello = parse_message(await asyncio.wait_for(ws.recv(), 10))
        except Exception as e:
            print(f"!! bad hello: {e}", file=sys.stderr)
            return
        self._record(hello)
        if hello["type"] != "hello":
            return
        self.hello = hello
        self.session_id = "ses_" + secrets.token_hex(10)
        await ws.send(json.dumps({"v": 1, "type": "welcome", "id": new_id("w-"), "ts": now_ms(),
                                  "re": hello["id"], "session_id": self.session_id, "protocol": 1,
                                  "heartbeat_ms": self.heartbeat_ms, "server_ts": now_ms()}))
        self.ws = ws
        self.connected.set()
        pinger = asyncio.create_task(self._ping(ws))
        try:
            async for text in ws:
                try:
                    msg = json.loads(text)
                except ValueError:
                    print(f"!! non-JSON frame: {text[:200]}", file=sys.stderr)
                    continue
                self._record(msg)
                await self._dispatch(msg)
        except ConnectionClosed:
            pass
        finally:
            pinger.cancel()
            if self.ws is ws:
                self.ws = None
                self.connected.clear()

    async def _ping(self, ws):
        n = 0
        while True:
            await asyncio.sleep(self.heartbeat_ms / 1000)
            if self.muted:
                continue
            n += 1
            try:
                await ws.send(json.dumps({"v": 1, "type": "ping", "id": f"p-{n:06d}", "ts": now_ms()}))
            except ConnectionClosed:
                return

    def _record(self, msg: dict):
        errs = message_errors(msg)
        if errs:
            self.violations.append((msg, errs))
            print(f"!! SCHEMA VIOLATION in {msg.get('type')}: {errs}", file=sys.stderr)
        self.inbox.append(msg)
        if self.on_message:
            self.on_message(msg)

    async def _dispatch(self, msg: dict):
        if msg.get("type") != "result":
            return
        fut = self._pending.pop(msg.get("re"), None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    # ---- exec ----------------------------------------------------------------------

    def exec_msg(self, op, args, approval=None, xid=None, deadline_ms=None) -> dict:
        self._seq += 1
        m = {"v": 1, "type": "exec", "id": xid or new_id("x-"), "ts": now_ms(), "op": op, "args": args,
             "run_id": "run_stub", "step_id": f"stp_{self._seq:04d}",
             "traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"}
        if deadline_ms is not None:
            m["deadline_ms"] = deadline_ms
        if approval is not None:
            m["approval"] = approval
        return m

    async def send_exec(self, m: dict, timeout=15.0) -> dict:
        if self.ws is None:
            raise RuntimeError("device is not connected")
        fut = asyncio.get_running_loop().create_future()
        self._pending[m["id"]] = fut
        await self.ws.send(json.dumps(m, ensure_ascii=False))
        r = await asyncio.wait_for(fut, timeout)
        if not r["ok"] and r["error"]["code"] in ("approval_required", "approval_invalid"):
            self.last_approval_request = (m, r["error"]["request"])
            if self.auto_approve and "approval" not in m:
                print(f"   (auto-approve) {r['error']['request']['summary']}")
                return await self.approve()
        return r

    async def exec(self, op, args, approval=None, **kw) -> dict:
        return await self.send_exec(self.exec_msg(op, args, approval, **kw))

    def grant_for(self, request: dict) -> dict:
        return sign_grant(self.key, kid=self.kid, device_id=request["device_id"], session_id=request["session_id"],
                          op=request["op"], args=request["args"], approved_by="user:stub",
                          iat=now_ms() // 1000, ttl_s=300)

    async def approve(self) -> dict:
        """直前に approval_required になった exec を、署名したグラント付きで（新しい id で）再送する。"""
        if self.last_approval_request is None:
            raise RuntimeError("nothing to approve")
        m, req = self.last_approval_request
        self.last_approval_request = None
        return await self.send_exec(self.exec_msg(m["op"], m["args"], self.grant_for(req)))

    def events(self, name=None) -> list[dict]:
        return [m for m in self.inbox if m.get("type") == "event" and (name is None or m["event"] == name)]


# ---- 対話 CLI ---------------------------------------------------------------------

HELP = """\
commands (数値は 10 進でも 0x.. でもよい):
  info                                  sys.info
  scan [bus]                            i2c.scan（既定 bus 1）
  read <bus> <addr> <reg|-> <len> [16]  i2c.read（reg を - にするとポインタを書かない。末尾 16 で reg_width 16）
  write <bus> <addr> <reg> <hex>        i2c.write
  bind <bus> <addr> <part> <fp_reg> <fp_len> <fp_hex> [regs,...]
  unbind <bus> <addr>
  gin <line>                            gpio.read
  gout <line> <0|1> [hold_ms]           gpio.write
  sample <period_ms> <duration_ms> <name:bus:addr:reg:len> ...   sample.start（batch 1000ms）
  stop <job_id>                         sample.stop
  estop | clear                         sys.estop / sys.estop_clear
  approve                               直前の承認待ちを署名して再送
  op <op> '<json args>'                 任意の exec
  mute | unmute                         ping を止める／再開（デッドマンの確認用）
  status                                接続状態
  quit"""


def _n(s: str) -> int:
    return int(s, 0)


def parse_command(line: str) -> tuple[str, dict] | None:
    w = shlex.split(line)
    if not w:
        return None
    c, a = w[0], w[1:]
    if c == "info":
        return "sys.info", {}
    if c == "scan":
        return "i2c.scan", {"bus": _n(a[0]) if a else 1}
    if c == "read":
        args = {"bus": _n(a[0]), "addr": _n(a[1]), "len": _n(a[3])}
        if a[2] != "-":
            args["reg"] = _n(a[2])
        if len(a) > 4:
            args["reg_width"] = _n(a[4])
        return "i2c.read", args
    if c == "write":
        return "i2c.write", {"bus": _n(a[0]), "addr": _n(a[1]), "reg": _n(a[2]), "hex": a[3].lower()}
    if c == "bind":
        regs = [_n(x) for x in a[6].split(",")] if len(a) > 6 and a[6] else []
        return "i2c.bind", {"bus": _n(a[0]), "addr": _n(a[1]), "part": a[2],
                            "fingerprint": {"reg": _n(a[3]), "len": _n(a[4]), "hex": a[5].lower()},
                            "write_allow_regs": regs}
    if c == "unbind":
        return "i2c.unbind", {"bus": _n(a[0]), "addr": _n(a[1])}
    if c == "gin":
        return "gpio.read", {"line": _n(a[0])}
    if c == "gout":
        args = {"line": _n(a[0]), "value": _n(a[1])}
        if len(a) > 2:
            args["hold_ms"] = _n(a[2])
        return "gpio.write", args
    if c == "sample":
        reads = []
        for spec in a[2:]:
            name, bus, addr, reg, ln = spec.split(":")
            reads.append({"name": name, "bus": _n(bus), "addr": _n(addr), "reg": _n(reg), "len": _n(ln)})
        return "sample.start", {"period_ms": _n(a[0]), "duration_ms": _n(a[1]), "batch_ms": 1000, "reads": reads}
    if c == "stop":
        return "sample.stop", {"job_id": a[0]}
    if c == "estop":
        return "sys.estop", {}
    if c == "clear":
        return "sys.estop_clear", {}
    if c == "op":
        return a[0], json.loads(a[1]) if len(a) > 1 else {}
    raise ValueError(f"unknown command: {c} (help で一覧)")


def _show(msg: dict):
    t = msg.get("type")
    if t == "pong":
        return
    if t == "hello":
        st = msg.get("state", {})
        print(f"<< hello {msg.get('device_id')} manifest={msg.get('manifest_sha256', '')[:12]}… state={st}")
    elif t == "samples":
        print(f"<< samples {msg['job_id']} seq={msg['seq']} rows={len(msg['rows'])} errors={msg.get('errors', 0)} "
              f"first={msg['rows'][0] if msg['rows'] else None}")
    elif t == "event":
        print(f"<< event {msg['event']} {json.dumps(msg['detail'], ensure_ascii=False)}")
    elif t == "bye":
        print(f"<< bye {msg['reason']}")


def _show_result(r: dict):
    pol = r["policy"]
    tag = f"[{pol['decision']} {pol['rule']}]"
    dup = " (duplicate)" if r.get("duplicate") else ""
    if r["ok"]:
        print(f"<< ok {tag} {json.dumps(r['data'], ensure_ascii=False)} {r.get('duration_ms', '')}ms{dup}")
    else:
        e = r["error"]
        print(f"<< {e['code']} {tag} {e['message']}{dup}")
        if "request" in e:
            print(f"   承認カード: {e['request']['summary']}  （approve で署名して再送）")
        if "detail" in e:
            print(f"   detail: {json.dumps(e['detail'], ensure_ascii=False)}")


async def repl(gw: StubGateway):
    loop = asyncio.get_running_loop()
    print(HELP)
    while True:
        try:
            line = await loop.run_in_executor(None, input, "gw> ")
        except EOFError:
            return
        line = line.strip()
        if not line:
            continue
        if line in ("quit", "exit"):
            return
        if line == "help":
            print(HELP)
            continue
        if line in ("mute", "unmute"):
            gw.muted = line == "mute"
            print(f"   ping {'stopped' if gw.muted else 'resumed'}")
            continue
        if line == "status":
            print(f"   connected={gw.ws is not None} session={gw.session_id} muted={gw.muted} "
                  f"violations={len(gw.violations)}")
            continue
        try:
            if line == "approve":
                r = await gw.approve()
            else:
                parsed = parse_command(line)
                if parsed is None:
                    continue
                r = await gw.exec(*parsed)
            _show_result(r)
        except (ValueError, IndexError, KeyError) as e:
            print(f"   ?? {e}")
        except (RuntimeError, asyncio.TimeoutError) as e:
            print(f"   !! {e or type(e).__name__}")


async def main_async(args):
    gw = StubGateway(args.host, args.port, heartbeat_ms=args.heartbeat_ms, auto_approve=args.auto_approve,
                     on_message=_show)
    await gw.start()
    print(f"stub gateway listening on ws://{args.host}:{gw.port}/v1/device"
          f"{' (auto-approve)' if args.auto_approve else ''}")
    try:
        await repl(gw)
    finally:
        await gw.stop()


def main():
    p = argparse.ArgumentParser(description="開発用の最小ゲートウェイ")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--heartbeat-ms", type=int, default=3000)
    p.add_argument("--auto-approve", action="store_true", help="承認待ちを例専用の鍵で自動的に署名して再送する")
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
