"""Web/API: ログイン、承認カード、署名、Agent 向け API、非常停止。Gateway と実物のデーモン（模擬ハード）につなぐ。"""
import asyncio
import contextlib
import re
import sys

import pytest
from cloudrig import DeviceProc, Server, client, until
from conftest import ROOT

from services.common.auth import CallerAuth
from services.common.store import MemoryStore
from services.gateway.app import create_app as create_gateway
from services.web.app import GatewayClient, create_app as create_web
from services.web.security import Sessions, check_login, hash_password, verify_password

sys.path.insert(0, str(ROOT / "tools"))
from agent_cli import Agent  # noqa: E402

PW = "correct horse battery"
BIND = {"bus": 1, "addr": 64, "part": "INA226", "fingerprint": {"reg": 254, "len": 2, "hex": "5449"},
        "write_allow_regs": [0, 5]}


class Cloud:
    """Gateway・Web/API・デーモンを1つのイベントループで動かす（Store は共有の MemoryStore）。"""

    def __init__(self, manifest, signing_key, tmp_path):
        self.store = MemoryStore()
        self.manifest, self.key, self.tmp_path = manifest, signing_key, tmp_path

    async def __aenter__(self):
        insecure = CallerAuth(None, insecure=True)
        self.gw_app = create_gateway(self.store, insecure, heartbeat_ms=1000)
        self.gw = await Server(self.gw_app).start()
        self.web_app = create_web(self.store, insecure, users={"judge": hash_password(PW, n=2 ** 10)},
                                  sessions=Sessions(b"s" * 32), signing_key=self.key, kid="test-key",
                                  gateway=GatewayClient(self.gw.url, use_id_token=False), cookie_secure=False)
        self.web = await Server(self.web_app).start()
        await self.store.create_pairing_code("123456", "user:judge", 2_000_000_000_000)
        async with client(self.gw.url) as c:
            token = (await c.post("/v1/pair", json={"code": "123456", "device_id": "pi-desk-01"})).json()["token"]
        self.token = token
        self.dev = await DeviceProc(self.manifest, self.gw.port, token, self.tmp_path / "audit.jsonl").start()
        await until(lambda: "pi-desk-01" in self.gw_app.state.hub.conns)
        self.agent = Agent(self.gw.url, self.web.url, "pi-desk-01", poll_s=0.05, approval_timeout_s=10,
                           say=lambda *_: None)
        return self

    async def __aexit__(self, *exc):
        await self.dev.stop()
        await self.web.stop()
        await self.gw.stop()

    @contextlib.asynccontextmanager
    async def browser(self):
        async with client(self.web.url) as b:
            r = await b.post("/login", data={"uid": "judge", "password": PW})
            assert r.status_code == 303 and r.headers["location"] == "/"
            yield b

    async def pending_card(self, b):
        """承認待ちのカードが出るまで待ち、(approval_id, csrf, ページ) を返す。"""
        for _ in range(200):
            html = (await b.get("/")).text
            m = re.search(r'action="/approvals/(apr_[0-9a-f]+)/approve"', html)
            if m:
                return m.group(1), re.search(r'name="csrf" value="([^"]+)"', html).group(1), html
            await asyncio.sleep(0.05)
        raise AssertionError("no pending approval card")


def test_browser_approval_reaches_device(manifest, signing_key, tmp_path):
    """M2 の完了条件: ブラウザで承認したグラントでデバイスが approved を返す。"""
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            task = asyncio.create_task(c.agent.run("i2c.bind", BIND))
            async with c.browser() as b:
                aid, csrf, html = await c.pending_card(b)
                # カードに出ているのはデバイスが返した request（summary・rule・args）
                assert "bus1 0x40 を INA226 として扱う" in html and "i2c.bus1.unknown.bind" in html
                r = await b.post(f"/approvals/{aid}/approve", data={"csrf": csrf})
                assert r.status_code == 303
            result = await asyncio.wait_for(task, 10)
            assert result["ok"] and result["policy"]["decision"] == "approved"
            assert (1, 64) in c.dev.d.state.bindings
            a = await c.store.get_approval(aid)
            assert a["status"] == "approved" and a["grant"]["approved_by"] == "user:judge"
    asyncio.run(go())


def test_denied_approval_is_not_executed(manifest, signing_key, tmp_path):
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            args = {"line": 17, "value": 1, "hold_ms": 500}
            task = asyncio.create_task(c.agent.run("gpio.write", args))
            async with c.browser() as b:
                aid, csrf, _ = await c.pending_card(b)
                await b.post(f"/approvals/{aid}/deny", data={"csrf": csrf})
                # 決まったあとでもう一度押しても何も起きない
                r = await b.post(f"/approvals/{aid}/approve", data={"csrf": csrf})
                assert "notice=decided" in r.headers["location"]
            result = await asyncio.wait_for(task, 10)
            assert result["error"]["code"] == "approval_required"
            assert c.dev.gpio.values[17] == 0
    asyncio.run(go())


def test_agent_cannot_fabricate_a_request(manifest, signing_key, tmp_path):
    """侵害された Agent が summary を偽った request を登録しても受け付けない。"""
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            r = await c.agent.exec("gpio.write", {"line": 17, "value": 1})
            req = dict(r["error"]["request"])
            async with client(c.web.url) as w:
                fake = {**req, "summary": "温度センサを読む"}          # summary だけ偽る → 記録と違うので表示は本物
                ok = await w.post("/api/approvals", json=fake)
                assert ok.status_code == 200
                aid = ok.json()["approval_id"]
                assert (await c.store.get_approval(aid))["request"]["summary"] == req["summary"]
                forged = {**req, "args": {"line": 17, "value": 1, "hold_ms": 2000}}   # args を変えると記録にない
                forged["args_sha256"] = "0" * 64
                assert (await w.post("/api/approvals", json=forged)).status_code == 400
                assert (await w.post("/api/approvals", json={"op": "x"})).status_code == 400
    asyncio.run(go())


def test_stale_session_request_is_refused(manifest, signing_key, tmp_path):
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            r = await c.agent.exec("gpio.write", {"line": 17, "value": 1})
            await c.store.update_device("pi-desk-01", {"session_id": "ses_newer000000"})
            async with client(c.web.url) as w:
                assert (await w.post("/api/approvals", json=r["error"]["request"])).status_code == 409
    asyncio.run(go())


def test_login_and_csrf(manifest, signing_key, tmp_path):
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            async with client(c.web.url) as anon:
                assert (await anon.get("/")).headers["location"] == "/login"
                r = await anon.post("/login", data={"uid": "judge", "password": "wrong"})
                assert r.headers["location"] == "/login?error=1"
                r = await anon.post("/devices/pair-code", data={"csrf": "x"})
                assert r.status_code == 303 and r.headers["location"] == "/login"
            async with c.browser() as b:
                assert (await b.post("/devices/pair-code", data={"csrf": "wrong"})).status_code == 403
                csrf = re.search(r'name="csrf" value="([^"]+)"', (await b.get("/")).text).group(1)
                r = await b.post("/devices/pair-code", data={"csrf": csrf})
                code = re.search(r">(\d{6})<", r.text).group(1)
                assert code in c.store.codes
    asyncio.run(go())


def test_estop_button(manifest, signing_key, tmp_path):
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            async with c.browser() as b:
                csrf = re.search(r'name="csrf" value="([^"]+)"', (await b.get("/")).text).group(1)
                r = await b.post("/devices/pi-desk-01/estop", data={"csrf": csrf})
                assert r.headers["location"] == "/?notice=estop_ok"
            assert c.dev.d.state.estop
    asyncio.run(go())


def test_pending_approval_expires(manifest, signing_key, tmp_path):
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            r = await c.agent.exec("gpio.write", {"line": 17, "value": 1})
            async with client(c.web.url) as w:
                aid = (await w.post("/api/approvals", json=r["error"]["request"])).json()["approval_id"]
                c.store.approvals[aid]["created_ms"] -= 11 * 60_000
                assert (await w.get(f"/api/approvals/{aid}")).json()["status"] == "expired"
    asyncio.run(go())


def test_password_and_session_primitives():
    h = hash_password("pw", n=2 ** 10)
    assert verify_password("pw", h) and not verify_password("px", h)
    assert check_login({"a": h}, "a", "pw") and not check_login({"a": h}, "b", "pw")
    s = Sessions(b"k" * 32)
    cookie = s.issue("judge", now=1000)
    assert s.read(cookie, now=1001)["uid"] == "judge"
    assert s.read(cookie, now=1000 + 13 * 3600) is None                 # 12時間で切れる
    body, sig = cookie.rsplit(".", 1)
    assert s.read(body + "." + sig[:-2] + "AA", now=1001) is None        # 改ざん
    with pytest.raises(ValueError):
        Sessions(b"short")


def test_pending_approvals_expire_when_session_ends(manifest, signing_key, tmp_path):
    """デバイスが切れる・つなぎ直す（新しいセッション）と、前のセッションの承認待ちは expired（session_ended）になる。
    カードは「接続が切れたため無効」と出て、押しても署名しない（F7）。"""
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            task = asyncio.create_task(c.agent.run("gpio.write", {"line": 17, "value": 1, "hold_ms": 500}))
            async with c.browser() as b:
                aid, csrf, _ = await c.pending_card(b)
                old_session = c.dev.d.state.session_id
                # デーモンの再起動: 切断で expired になり、新しいセッションでつながる
                await c.dev.stop()
                await until(lambda: c.store.approvals[aid]["status"] == "expired")
                assert c.store.approvals[aid]["reason"] == "session_ended"
                c.dev = await DeviceProc(manifest, c.gw.port, c.token, tmp_path / "audit.jsonl").start()
                await until(lambda: (c.store.devices.get("pi-desk-01") or {}).get("session_id") not in (None, old_session)
                            and c.store.devices["pi-desk-01"].get("online"))
                card = (await b.get(f"/ui/approvals/{aid}")).json()
                assert card["status"] == "expired" and card["reason"] == "session_ended"
                assert f"/approvals/{aid}/approve" not in (await b.get("/")).text
                r = await b.post(f"/approvals/{aid}/approve", data={"csrf": csrf})
                assert "notice=decided" in r.headers["location"] and "grant" not in c.store.approvals[aid]
            task.cancel()

            # 新しいセッションでつながったとき、残っていた別セッションの承認待ちも expired にする
            await c.store.create_approval("apr_stale0000001", {
                "status": "pending", "created_ms": 10 ** 13, "run_id": "r", "requested_by": "t",
                "request": {"device_id": "pi-desk-01", "session_id": "ses_gone00000001", "op": "gpio.write",
                            "args": {}, "args_sha256": "0" * 64, "rule": "x", "summary": "x"}})
            await c.dev.stop()
            c.dev = await DeviceProc(manifest, c.gw.port, c.token, tmp_path / "audit.jsonl").start()
            await until(lambda: c.store.approvals["apr_stale0000001"]["status"] == "expired")

            # 押した時点でセッションが変わっていたら、署名せずに expired にする（Gateway の後始末より先に押された場合）
            await c.store.create_approval("apr_race00000001", {
                "status": "pending", "created_ms": 10 ** 13, "run_id": "r", "requested_by": "t",
                "request": {"device_id": "pi-desk-01", "session_id": "ses_gone00000002", "op": "gpio.write",
                            "args": {"line": 17, "value": 1}, "args_sha256": "0" * 64, "rule": "x", "summary": "x"}})
            async with c.browser() as b:
                csrf = re.search(r'name="csrf" value="([^"]+)"', (await b.get("/")).text).group(1)
                r = await b.post("/approvals/apr_race00000001/approve", data={"csrf": csrf})
                assert "notice=session_ended" in r.headers["location"]
            a = c.store.approvals["apr_race00000001"]
            assert a["status"] == "expired" and a["reason"] == "session_ended" and "grant" not in a
    asyncio.run(go())


def test_estop_survives_restart_and_is_cleared_only_by_approval(manifest, signing_key, tmp_path):
    """非常停止はディスクに残り、デーモンを再起動しても止まったまま。画面の「解除を求める」で承認カードが出て、
    人が承認すると Web/API がグラント付きで送り、解除される。"""
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            async with c.browser() as b:
                csrf = re.search(r'name="csrf" value="([^"]+)"', (await b.get("/")).text).group(1)
                await b.post("/devices/pi-desk-01/estop", data={"csrf": csrf})
                assert c.dev.d.state.estop and (tmp_path / "estop.json").exists()
                # 再起動しても止まったまま（以前は再起動で承認なしに解除されていた）
                await c.dev.stop()
                c.dev = await DeviceProc(manifest, c.gw.port, c.token, tmp_path / "audit.jsonl").start()
                await until(lambda: (c.store.devices.get("pi-desk-01") or {}).get("online")
                            and c.store.devices["pi-desk-01"]["session_id"] == c.dev.d.state.session_id)
                assert c.dev.d.state.estop
                assert (await c.agent.exec("gpio.write", {"line": 17, "value": 1, "hold_ms": 500}))["error"]["code"] \
                    == "estop_active"
                await until(lambda: c.store.devices["pi-desk-01"].get("state", {}).get("estop"))
                page = (await b.get("/")).text
                assert "非常停止の解除を求める" in page
                r = await b.post("/devices/pi-desk-01/estop-clear", data={"csrf": csrf})
                assert r.headers["location"] == "/?notice=estop_clear_requested"
                # 2回押しても承認待ちは1枚
                await b.post("/devices/pi-desk-01/estop-clear", data={"csrf": csrf})
                pend = [a for a in c.store.approvals.values() if a["status"] == "pending"]
                assert len(pend) == 1 and pend[0]["request"]["op"] == "sys.estop_clear"
                assert c.dev.d.state.estop                              # まだ解除されていない
                aid, csrf2, html = await c.pending_card(b)
                assert "非常停止を解除する" in html and "解除を求める」ボタンから" in html
                r = await b.post(f"/approvals/{aid}/approve", data={"csrf": csrf2})
                assert r.headers["location"] == "/?notice=estop_cleared"
                assert not c.dev.d.state.estop and not (tmp_path / "estop.json").exists()
                assert (await c.agent.exec("gpio.write", {"line": 17, "value": 0}))["ok"]
    asyncio.run(go())
