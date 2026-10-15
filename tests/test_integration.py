"""WebSocket を通した結合テスト: stub_gateway ⇄ デーモン（FakeI2C／FakeGPIO）。"""
import asyncio
import json
import sys

import pytest
from conftest import ROOT

from mugemuge_agentd.daemon import Daemon
from mugemuge_agentd.hw.gpio import FakeGPIO
from mugemuge_agentd.hw.i2c import FakeI2C

sys.path.insert(0, str(ROOT / "tools"))
from stub_gateway import StubGateway, parse_command  # noqa: E402

FAST_BACKOFF = (0.1, 0.2, 0.4)


async def until(cond, timeout=5.0, step=0.02):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


class Pair:
    """ゲートウェイとデーモンを同じイベントループで動かす。"""

    def __init__(self, manifest, signing_key, tmp_path, heartbeat_ms=500):
        self.gw = StubGateway(port=0, key=signing_key, kid="test-key", heartbeat_ms=heartbeat_ms)
        self.manifest = manifest
        self.tmp_path = tmp_path
        self.i2c, self.gpio = FakeI2C(), FakeGPIO()

    async def __aenter__(self):
        await self.gw.start()
        self.d = Daemon(self.manifest, self.i2c, self.gpio, gateway_url=f"ws://127.0.0.1:{self.gw.port}/v1/device",
                        token=None, audit_path=self.tmp_path / "audit.jsonl", backoff=FAST_BACKOFF)
        self.task = asyncio.create_task(self.d.run())
        await asyncio.wait_for(self.gw.connected.wait(), 5)
        return self

    async def __aexit__(self, *exc):
        self.d.request_stop()
        await asyncio.wait_for(self.task, 10)
        await self.gw.stop()
        assert self.gw.violations == [], self.gw.violations


def ok(r):
    assert r["ok"], r
    return r["data"]


def test_session_flow_over_websocket(manifest, signing_key, tmp_path):
    async def go():
        async with Pair(manifest, signing_key, tmp_path) as p:
            gw = p.gw
            assert gw.hello["device_id"] == "pi-desk-01"
            assert gw.hello["state"]["outputs"] == [{"line": 17, "value": 0}]

            assert ok(await gw.exec("i2c.scan", {"bus": 1}))["found"] == [64, 118]
            assert ok(await gw.exec("i2c.read", {"bus": 1, "addr": 64, "reg": 0xFE, "len": 2}))["hex"] == "5449"
            assert ok(await gw.exec("i2c.read", {"bus": 1, "addr": 64, "reg": 0xFF, "len": 2}))["hex"] == "2260"

            bind = {"bus": 1, "addr": 64, "part": "INA226", "fingerprint": {"reg": 254, "len": 2, "hex": "5449"},
                    "write_allow_regs": [0, 5]}
            r = await gw.exec("i2c.bind", bind)
            assert r["error"]["code"] == "approval_required"
            r = await gw.approve()
            assert r["policy"]["decision"] == "approved" and ok(r) == {"bound": True}

            assert ok(await gw.exec("i2c.write", {"bus": 1, "addr": 64, "reg": 5, "hex": "0a00"})) == {"written": 2}

            r = await gw.exec("sample.start", {"period_ms": 100, "duration_ms": 1000, "batch_ms": 300, "reads": [
                {"name": "bus_v", "bus": 1, "addr": 64, "reg": 2, "len": 2},
                {"name": "current", "bus": 1, "addr": 64, "reg": 4, "len": 2}]})
            job = ok(r)["job_id"]
            await until(lambda: gw.events("job_end"), timeout=5)
            rows = [row for m in gw.inbox if m["type"] == "samples" for row in m["rows"]]
            assert len(rows) == 10 and rows[0][1:] == ["0a14", "04b0"]
            assert gw.events("job_end")[0]["detail"] == {"job_id": job, "reason": "completed", "rows": 10}

            r = await gw.exec("gpio.write", {"line": 17, "value": 1, "hold_ms": 5000})
            assert r["error"]["code"] == "policy_denied"
            r = await gw.exec("i2c.write", {"bus": 0, "addr": 0x50, "reg": 0, "hex": "ff"})
            assert r["error"]["code"] == "policy_denied"
            assert p.gpio.values[17] == 0

            # 同じ id の再送は実行せずに前回の結果を返す
            m = gw.exec_msg("i2c.write", {"bus": 1, "addr": 64, "reg": 5, "hex": "0b00"})
            r1 = await gw.send_exec(m)
            r2 = await gw.send_exec(m)
            assert r1["ok"] and r2["duplicate"] is True
            assert [w for w in p.i2c.devices[1][64].writes if w[1] == bytes.fromhex("0b00")] == [(5, b"\x0b\x00")]
        # 終了時: bye が届き、監査ログが残る
        assert p.gw.inbox[-1]["type"] == "bye"
        # 再送（duplicate）は実行しないので監査ログには載らない
        assert len((tmp_path / "audit.jsonl").read_text().splitlines()) == 10
    asyncio.run(go())


def test_malformed_exec_gets_bad_request(manifest, signing_key, tmp_path):
    async def go():
        async with Pair(manifest, signing_key, tmp_path) as p:
            gw = p.gw
            # 小数表記はパーサの段階で拒否される
            text = json.dumps(gw.exec_msg("i2c.read", {"bus": 1, "addr": 64, "reg": 254, "len": 2})).replace(
                '"len": 2', '"len": 2.0')
            fut = asyncio.get_running_loop().create_future()
            xid = json.loads(text)["id"]
            gw._pending[xid] = fut
            await gw.ws.send(text)
            r = await asyncio.wait_for(fut, 5)
            assert r["ok"] is False and r["error"]["code"] == "bad_request"

            m = gw.exec_msg("i2c.read", {"bus": 1, "addr": 64, "len": 2})
            m["op"] = "pico.flash"
            fut = asyncio.get_running_loop().create_future()
            gw._pending[m["id"]] = fut
            await gw.ws.send(json.dumps(m))
            assert (await asyncio.wait_for(fut, 5))["error"]["code"] == "unknown_op"
    asyncio.run(go())


def test_deadman_when_gateway_goes_away(manifest, signing_key, tmp_path):
    """ゲートウェイを止める → deadman_ms 後に出力が safe_value に戻る → 再接続後に deadman_tripped が届く。"""
    async def go():
        async with Pair(manifest, signing_key, tmp_path) as p:
            gw = p.gw
            p.d.safety.deadman_ms = 600          # スキーマの下限は 2000。テストを速くするため直接縮める
            args = {"line": 17, "value": 1, "hold_ms": 2000}
            r = await gw.exec("gpio.write", args)
            assert r["error"]["code"] == "approval_required"
            assert ok(await gw.approve())["value"] == 1 and p.gpio.values[17] == 1

            port = gw.port
            await gw.stop()                       # ゲートウェイが落ちた
            await asyncio.sleep(0.3)
            assert p.gpio.values[17] == 1         # まだデッドマン前
            await until(lambda: p.gpio.values[17] == 0, timeout=2)
            assert p.d.state.session_id is None
            before = len(gw.inbox)

            gw.port = port
            await gw.start()                      # 復旧 → デーモンが張り直す
            await asyncio.wait_for(gw.connected.wait(), 5)
            await until(lambda: gw.events("deadman_tripped"), timeout=3)
            new = gw.inbox[before:]
            assert new[0]["type"] == "hello" and new[0]["state"]["outputs"] == [{"line": 17, "value": 0}]
            ev = gw.events("deadman_tripped")[0]
            assert ev["detail"]["outputs_reverted"] == [17] and ev["detail"]["silent_ms"] >= 600
            # welcome 直後に届いた最初のメッセージが deadman_tripped
            assert [m["type"] for m in new[1:2]] == ["event"]
            # 前のセッションのグラントはもう通らない
            r = await gw.exec("gpio.write", args, approval=gw.grant_for({
                "device_id": "pi-desk-01", "session_id": "ses_oldsession000", "op": "gpio.write", "args": args}))
            assert r["error"]["code"] == "approval_invalid" and r["error"]["detail"]["reason"] == "wrong_session"
            assert p.gpio.values[17] == 0
    asyncio.run(go())


def test_deadman_when_pings_stop(manifest, signing_key, tmp_path):
    async def go():
        async with Pair(manifest, signing_key, tmp_path, heartbeat_ms=500) as p:
            gw = p.gw
            p.d.safety.deadman_ms = 1200
            p.d.safety.lines[17]["max_on_ms"] = 10_000   # 自動 OFF より先にデッドマンが働くことを見る
            await gw.exec("gpio.write", {"line": 17, "value": 1})
            ok(await gw.approve())
            await asyncio.sleep(1.5)
            assert p.gpio.values[17] == 1         # ping が届いている間は作動しない
            gw.muted = True
            await until(lambda: gw.events("deadman_tripped"), timeout=2)
            assert p.gpio.values[17] == 0
            assert gw.events("deadman_tripped")[0]["detail"]["outputs_reverted"] == [17]
            gw.muted = False
    asyncio.run(go())


def test_relay_auto_off_event(manifest, signing_key, tmp_path):
    async def go():
        async with Pair(manifest, signing_key, tmp_path) as p:
            gw = p.gw
            await gw.exec("gpio.write", {"line": 17, "value": 1, "hold_ms": 300})
            d = ok(await gw.approve())
            assert d["value"] == 1 and "revert_at_ms" in d
            await until(lambda: gw.events("output_reverted"), timeout=2)
            assert p.gpio.values[17] == 0
            assert gw.events("output_reverted")[0]["detail"]["line"] == 17
    asyncio.run(go())


@pytest.mark.parametrize("line, op, args", [
    ("scan", "i2c.scan", {"bus": 1}),
    ("read 1 0x40 0xfe 2", "i2c.read", {"bus": 1, "addr": 64, "reg": 254, "len": 2}),
    ("read 1 0x40 - 2", "i2c.read", {"bus": 1, "addr": 64, "len": 2}),
    ("bind 1 0x40 INA226 0xfe 2 5449 0,5", "i2c.bind",
     {"bus": 1, "addr": 64, "part": "INA226", "fingerprint": {"reg": 254, "len": 2, "hex": "5449"},
      "write_allow_regs": [0, 5]}),
    ("gout 17 1 1500", "gpio.write", {"line": 17, "value": 1, "hold_ms": 1500}),
    ("sample 100 10000 bus_v:1:0x40:2:2", "sample.start",
     {"period_ms": 100, "duration_ms": 10000, "batch_ms": 1000,
      "reads": [{"name": "bus_v", "bus": 1, "addr": 64, "reg": 2, "len": 2}]}),
])
def test_stub_command_parser(line, op, args):
    assert parse_command(line) == (op, args)
