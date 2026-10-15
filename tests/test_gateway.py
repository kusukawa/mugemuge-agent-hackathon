"""Gateway ⇄ 実物のデーモン（模擬ハード）を WebSocket でつなぐ結合テスト。"""
import asyncio
import hashlib
import json

import pytest
from fastapi import Request
from cloudrig import DeviceProc, Server, client, until

from mugemuge_agentd.approval import sign_grant
from services.common.auth import CallerAuth
from services.common.store import MemoryStore
from services.gateway.app import create_app


class GW:
    def __init__(self, manifest, tmp_path, instance=None, store=None, caller_auth=None, full_exec_callers=None):
        self.store = store or MemoryStore()
        self.app = create_app(self.store, caller_auth or CallerAuth(None, insecure=True), heartbeat_ms=1000,
                              instance=instance, full_exec_callers=full_exec_callers)
        self.manifest = manifest
        self.tmp_path = tmp_path

    async def __aenter__(self):
        self.server = await Server(self.app).start()
        self.http = client(self.server.url)
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()
        await self.server.stop()

    async def pair(self, device_id="pi-desk-01", owner="user:test"):
        await self.store.create_pairing_code("123456", owner, expires_ms=2_000_000_000_000)
        r = await self.http.post("/v1/pair", json={"code": "123456", "device_id": device_id})
        assert r.status_code == 200, r.text
        return r.json()["token"]

    async def device(self, token, manifest=None):
        d = await DeviceProc(manifest or self.manifest, self.server.port, token, self.tmp_path / "audit.jsonl").start()
        await until(lambda: "pi-desk-01" in self.app.state.hub.conns or d.task.done(), timeout=5)
        return d

    async def exec(self, op, args, headers=None, **kw):
        body = {"device_id": "pi-desk-01", "op": op, "args": args, "run_id": "run_t", "step_id": "stp_1", **kw}
        return await self.http.post("/internal/exec", json=body, headers=headers)


def test_pair_connect_and_session_flow(manifest, signing_key, tmp_path):
    async def go():
        async with GW(manifest, tmp_path) as gw:
            token = await gw.pair()
            assert token.startswith("mgd_")
            stored = await gw.store.get_device("pi-desk-01")
            assert stored["token_sha256"] == hashlib.sha256(token.encode()).hexdigest() and token not in str(stored)

            dev = await gw.device(token)
            d = (await gw.http.get("/internal/devices/pi-desk-01")).json()
            assert d["online"] and d["session_id"].startswith("ses_") and "token_sha256" not in d
            assert d["manifest"]["device"]["id"] == "pi-desk-01"

            r = (await gw.exec("i2c.scan", {"bus": 1})).json()
            assert r["ok"] and r["data"]["found"] == [64, 118]

            bind = {"bus": 1, "addr": 64, "part": "INA226", "fingerprint": {"reg": 254, "len": 2, "hex": "5449"},
                    "write_allow_regs": [0, 5]}
            r = (await gw.exec("i2c.bind", bind)).json()
            req = r["error"]["request"]
            assert r["error"]["code"] == "approval_required" and req["session_id"] == d["session_id"]
            grant = sign_grant(signing_key, kid="test-key", device_id=req["device_id"], session_id=req["session_id"],
                               op=req["op"], args=req["args"], approved_by="user:test",
                               iat=dev.d.clock.corrected_s())
            r = (await gw.exec("i2c.bind", bind, approval=grant)).json()
            assert r["ok"] and r["policy"]["decision"] == "approved"

            r = (await gw.exec("sample.start", {"period_ms": 100, "duration_ms": 500, "batch_ms": 200, "reads": [
                {"name": "v", "bus": 1, "addr": 64, "reg": 2, "len": 2}]})).json()
            job = r["data"]["job_id"]
            await until(lambda: any(e["event"] == "job_end" for e in gw.store.events.get("pi-desk-01", [])))
            docs = await gw.store.list_samples("pi-desk-01", job)
            rows = [row for doc in docs for row in doc["rows"]]
            assert len(rows) == 5 and rows[0] == {"t": 0, "v": ["0a14"]}
            got = (await gw.http.get(f"/internal/devices/pi-desk-01/samples/{job}")).json()
            assert len(got["rows"]) == 5 and got["missing_seq"] == [] and got["errors"] == 0
            evs = (await gw.http.get("/internal/devices/pi-desk-01/events")).json()["events"]
            assert evs[0]["event"] == "job_end" and evs[0]["detail"]["job_id"] == job

            # 同じ id の再送は二重に実行されない
            r1 = (await gw.exec("i2c.read", {"bus": 1, "addr": 64, "reg": 2, "len": 2}, id="x-same-000001")).json()
            r2 = (await gw.exec("i2c.read", {"bus": 1, "addr": 64, "reg": 2, "len": 2}, id="x-same-000001")).json()
            assert r1["ok"] and r2["duplicate"] is True

            await dev.stop()
            await until(lambda: "pi-desk-01" not in gw.app.state.hub.conns)
            assert (await gw.store.get_device("pi-desk-01"))["online"] is False
            r = await gw.exec("i2c.scan", {"bus": 1})
            assert r.status_code == 503 and r.json()["error"] == "device_offline"
    asyncio.run(go())


def test_exec_body_is_checked(manifest, tmp_path):
    async def go():
        async with GW(manifest, tmp_path) as gw:
            dev = await gw.device(await gw.pair())
            body = json.dumps({"device_id": "pi-desk-01", "op": "i2c.read", "run_id": "r", "step_id": "s",
                               "args": {"bus": 1, "addr": 64, "reg": 2, "len": 2.0}})
            r = await gw.http.post("/internal/exec", content=body)
            assert r.status_code == 400 and "non-integer" in r.text
            r = await gw.exec("pico.flash", {})
            assert r.status_code == 400 and r.json()["error"] == "bad_request"
            await dev.stop()
    asyncio.run(go())


def test_unknown_token_is_rejected(manifest, tmp_path):
    async def go():
        async with GW(manifest, tmp_path) as gw:
            await gw.pair()
            dev = await DeviceProc(manifest, gw.server.port, "mgd_wrong", tmp_path / "a.jsonl").start()
            await asyncio.sleep(0.5)
            assert gw.app.state.hub.conns == {}
            await dev.stop()
    asyncio.run(go())


def test_token_for_other_device_gets_bye(manifest, raw_manifest, tmp_path):
    async def go():
        async with GW(manifest, tmp_path) as gw:
            token = await gw.pair(device_id="pi-other-02")
            dev = await DeviceProc(manifest, gw.server.port, token, tmp_path / "a.jsonl").start()
            await asyncio.sleep(0.5)
            assert gw.app.state.hub.conns == {}
            assert not (await gw.store.get_device("pi-other-02")).get("online")
            await dev.stop()
    asyncio.run(go())


def test_repairing_disconnects_old_token(manifest, tmp_path):
    async def go():
        async with GW(manifest, tmp_path) as gw:
            dev = await gw.device(await gw.pair())
            await gw.pair()                       # 新しいトークン → 古い接続は切られ、古いトークンでは戻れない
            await asyncio.sleep(0.8)
            assert gw.app.state.hub.conns == {}
            await dev.stop()
    asyncio.run(go())


def test_pairing_failures_are_rate_limited(manifest, tmp_path):
    async def go():
        async with GW(manifest, tmp_path) as gw:
            codes = []
            for i in range(21):
                r = await gw.http.post("/v1/pair", json={"code": f"{i:06d}", "device_id": "pi-desk-01"})
                codes.append(r.status_code)
            assert codes[:20] == [403] * 20 and codes[20] == 429
    asyncio.run(go())


def test_pairing_to_another_owners_device_is_refused(manifest, tmp_path):
    async def go():
        async with GW(manifest, tmp_path) as gw:
            await gw.pair(owner="user:a")
            await gw.store.create_pairing_code("222222", "user:b", expires_ms=2_000_000_000_000)
            r = await gw.http.post("/v1/pair", json={"code": "222222", "device_id": "pi-desk-01"})
            assert r.status_code == 409
    asyncio.run(go())


@pytest.mark.parametrize("body", [{"code": "12345", "device_id": "pi-desk-01"},
                                  {"code": "123456", "device_id": "Bad ID"}, {"code": "123456"}])
def test_pair_body_validation(manifest, tmp_path, body):
    async def go():
        async with GW(manifest, tmp_path) as gw:
            assert (await gw.http.post("/v1/pair", json=body)).status_code == 400
    asyncio.run(go())


def test_old_gateway_drains_devices_to_newer_one(manifest, tmp_path):
    """Cloud Run の新リビジョン: 古いインスタンスはデバイスに server_drain を送って手放し、デバイスは新しい方へ張り直す。"""
    async def go():
        store = MemoryStore()
        async with GW(manifest, tmp_path, instance="rev-1:a", store=store) as old:
            await store.set_gateway_leader("rev-1:a", 1)
            dev = await old.device(await old.pair())
            assert await old.app.state.check_drain() == 0          # 自分が leader の間は何もしない
            async with GW(manifest, tmp_path, instance="rev-2:b", store=store) as new:
                await store.set_gateway_leader("rev-2:b", 2_000_000_000_000)
                # デーモンの接続先を新しいポートに向ける（Cloud Run では同じ URL のまま新リビジョンに振られる）
                dev.d.client.url = f"ws://127.0.0.1:{new.server.port}/v1/device"
                assert await old.app.state.check_drain() == 1
                await until(lambda: "pi-desk-01" in new.app.state.hub.conns, timeout=5)
                assert "pi-desk-01" not in old.app.state.hub.conns
                r = (await new.exec("i2c.scan", {"bus": 1})).json()
                assert r["ok"]
                await dev.stop()
    asyncio.run(go())


def test_exec_waits_for_device_during_handover(manifest, tmp_path):
    """新しいインスタンスに exec が先に来ても、デバイスが移ってくるのを待って転送する。来なければ device_offline（F3）。"""
    async def go():
        store = MemoryStore()
        async with GW(manifest, tmp_path, instance="rev-1:a", store=store) as old:
            await store.set_gateway_leader("rev-1:a", 1)
            dev = await old.device(await old.pair())
            async with GW(manifest, tmp_path, instance="rev-2:b", store=store) as new:
                await store.set_gateway_leader("rev-2:b", 2_000_000_000_000)
                dev.d.client.url = f"ws://127.0.0.1:{new.server.port}/v1/device"
                pending = asyncio.create_task(new.exec("i2c.scan", {"bus": 1}, deadline_ms=4000))
                await asyncio.sleep(0.3)
                assert not pending.done()                          # まだデバイスは旧インスタンス。即エラーにしない
                assert await old.app.state.check_drain() == 1
                r = await pending
                assert r.status_code == 200 and r.json()["ok"]
                await dev.stop()
                loop = asyncio.get_running_loop()
                t0 = loop.time()
                r = await new.exec("i2c.scan", {"bus": 1}, deadline_ms=500)
                assert r.status_code == 503 and r.json()["error"] == "device_offline"
                assert 0.4 < loop.time() - t0 < 2.0              # 待つのは min(deadline_ms, 5秒) まで
    asyncio.run(go())


class HeaderCaller(CallerAuth):
    """テスト用: ID トークンの代わりに x-test-caller ヘッダを呼び出し元のメールアドレスとして受ける。"""

    async def __call__(self, request: Request):
        return request.headers["x-test-caller"]


def test_web_service_can_only_send_estop_ops(manifest, signing_key, tmp_path):
    """Web/API（署名鍵を持つ）から送れるのは非常停止の2つと記録の停止だけ。承認つきでも i2c.bind などは 403（P2）。
    Agent（EXEC_FULL_CALLERS）は今までどおり何でも送れ、判定はデバイスがする。"""
    WEB, AGENT = "mugemuge-web@p.iam.gserviceaccount.com", "mugemuge-agent@p.iam.gserviceaccount.com"

    async def go():
        async with GW(manifest, tmp_path, caller_auth=HeaderCaller(None, {WEB, AGENT}),
                      full_exec_callers={AGENT}) as gw:
            dev = await gw.device(await gw.pair())
            web, agent = {"x-test-caller": WEB}, {"x-test-caller": AGENT}
            bind = {"bus": 1, "addr": 64, "part": "INA226", "fingerprint": {"reg": 254, "len": 2, "hex": "5449"},
                    "write_allow_regs": [0, 5]}
            r = (await gw.exec("i2c.bind", bind, headers=agent)).json()
            req = r["error"]["request"]
            grant = sign_grant(signing_key, kid="test-key", device_id=req["device_id"], session_id=req["session_id"],
                               op=req["op"], args=req["args"], approved_by="user:test",
                               iat=dev.d.clock.corrected_s())
            for op, args, extra in [("i2c.bind", bind, {"approval": grant}), ("i2c.scan", {"bus": 1}, {}),
                                    ("gpio.write", {"line": 17, "value": 1}, {})]:
                r = await gw.exec(op, args, headers=web, **extra)
                assert r.status_code == 403 and r.json() == {"error": "op_not_allowed_for_caller", "op": op}
            assert (1, 64) not in dev.d.state.bindings

            # テスター画面の読み出しは送れる。ただし承認つきでは送れない（16 ビットの reg はコマンドの書き込みになる）
            r = (await gw.exec("i2c.read", {"bus": 1, "addr": 64, "reg": 254, "len": 2}, headers=web)).json()
            assert r["ok"] and r["data"]["hex"] == "5449"
            cmd = {"bus": 1, "addr": 64, "reg": 0x30A2, "reg_width": 16, "len": 1}
            r = (await gw.exec("i2c.read", cmd, headers=agent)).json()
            assert r["error"]["code"] == "approval_required"
            req = r["error"]["request"]
            cmd_grant = sign_grant(signing_key, kid="test-key", device_id=req["device_id"],
                                   session_id=req["session_id"], op=req["op"], args=req["args"],
                                   approved_by="user:test", iat=dev.d.clock.corrected_s())
            r = await gw.exec("i2c.read", cmd, headers=web, approval=cmd_grant)
            assert r.status_code == 403 and r.json() == {"error": "op_not_allowed_for_caller", "op": "i2c.read"}
            r = (await gw.exec("i2c.read", cmd, headers=web)).json()
            assert r["error"]["code"] == "approval_required"           # 承認なしならデバイスが求めるだけ

            r = (await gw.exec("sys.estop", {}, headers=web)).json()
            assert r["ok"] and dev.d.state.estop
            r = (await gw.exec("sys.estop_clear", {}, headers=web)).json()
            assert r["error"]["code"] == "approval_required"           # 解除は今までどおりデバイスが承認を求める
            r = await gw.exec("log.stop", {"log_id": "log_none_1"}, headers=web)
            assert r.status_code == 200                                 # 記録の停止は安全側（届けばデバイスが判定）

            r = (await gw.exec("i2c.bind", bind, headers=agent, approval=grant)).json()
            assert r["error"]["code"] == "estop_active"                # Agent は送れる（判定はデバイス）
            await dev.stop()
    asyncio.run(go())
