"""仮想デバイス（M4）: 「仮想デバイスで試す」→ 専用のデバイスが本物と同じ経路でつながる。審査員どうしで干渉しない。"""
import asyncio
import contextlib
import re
import sys

from cloudrig import Server, client, until
from conftest import ROOT
from test_web import PW

from mugemuge_agentd.approval import public_key_to_b64
from services.common.auth import CallerAuth
from services.common.store import MemoryStore
from services.gateway.app import create_app as create_gateway
from services.virtual.app import VirtualFleet, create_app as create_virtual
from services.web.app import GatewayClient, VirtualClient, create_app as create_web
from services.web.security import Sessions, hash_password

sys.path.insert(0, str(ROOT / "tools"))
from agent_cli import Agent  # noqa: E402


class VCloud:
    def __init__(self, signing_key, ttl_s=3600, max_devices=10):
        self.store = MemoryStore()
        self.key, self.ttl_s, self.max_devices = signing_key, ttl_s, max_devices

    async def __aenter__(self):
        insecure = CallerAuth(None, insecure=True)
        self.gw_app = create_gateway(self.store, insecure, heartbeat_ms=1000)
        self.gw = await Server(self.gw_app).start()
        self.fleet = VirtualFleet(self.gw.url, [{"kid": "test-key", "key": public_key_to_b64(self.key.public_key())}],
                                  ttl_s=self.ttl_s, max_devices=self.max_devices)
        self.virt = await Server(create_virtual(self.fleet)).start()
        self.web_app = create_web(self.store, insecure, users={"judge": hash_password(PW, n=2 ** 10)},
                                  sessions=Sessions(b"s" * 32), signing_key=self.key, kid="test-key",
                                  gateway=GatewayClient(self.gw.url, use_id_token=False), cookie_secure=False,
                                  virtual=VirtualClient(self.virt.url, use_id_token=False), virtual_ttl_s=self.ttl_s)
        self.web = await Server(self.web_app).start()
        return self

    async def __aexit__(self, *exc):
        await self.fleet.stop_all()
        await self.web.stop()
        await self.virt.stop()
        await self.gw.stop()

    @contextlib.asynccontextmanager
    async def browser(self):
        async with client(self.web.url) as b:
            await b.post("/login", data={"uid": "judge", "password": PW})
            yield b

    @staticmethod
    async def csrf(b) -> str:
        return re.search(r'name="csrf" value="([^"]+)"', (await b.get("/")).text).group(1)

    async def make_virtual(self, b) -> str:
        r = await b.post("/devices/virtual", data={"csrf": await self.csrf(b)})
        assert r.headers["location"] == "/?notice=virtual_ok", r.headers["location"]
        page = (await b.get("/")).text
        device_id = re.search(r"<b>(virt-[0-9a-f]+)</b>", page).group(1)
        await until(lambda: device_id in self.gw_app.state.hub.conns, timeout=10)
        return device_id


def test_virtual_device_flow_and_isolation(signing_key):
    async def go():
        async with VCloud(signing_key) as c:
            async with c.browser() as a, c.browser() as b:      # 同じ test アカウントの審査員2人
                assert "はじめかた" in (await a.get("/")).text                 # 初めての人への案内
                dev_a = await c.make_virtual(a)
                assert "はじめかた" not in (await a.get("/")).text
                assert "仮想デバイスで試す" not in (await a.get("/")).text      # 1つのセッションに1台
                assert (await a.post("/devices/virtual", data={"csrf": await c.csrf(a)})).headers["location"] \
                    == "/?notice=virtual_exists"
                assert dev_a not in (await b.get("/")).text                  # B には A の仮想デバイスが見えない

                agent = Agent(c.gw.url, c.web.url, dev_a, poll_s=0.05, approval_timeout_s=10, say=lambda *_: None)
                found = (await agent.exec("i2c.scan", {"bus": 1}))["data"]["found"]
                assert found == [0x40, 0x44, 0x76]

                bind = {"bus": 1, "addr": 0x76, "part": "BMP280", "fingerprint": {"reg": 0xD0, "len": 1, "hex": "58"},
                        "write_allow_regs": [0xF4, 0xF5]}
                task = asyncio.create_task(agent.run("i2c.bind", bind))
                for _ in range(200):
                    page_a = (await a.get("/")).text
                    m = re.search(r'action="/approvals/(apr_[0-9a-f]+)/approve"', page_a)
                    if m:
                        break
                    await asyncio.sleep(0.05)
                aid = m.group(1)
                assert aid not in (await b.get("/")).text                    # B には A のカードも見えない
                r = await b.post(f"/approvals/{aid}/approve", data={"csrf": await c.csrf(b)})
                assert r.status_code == 403                                  # B は A の承認を押せない
                await a.post(f"/approvals/{aid}/approve", data={"csrf": await c.csrf(a)})
                res = await asyncio.wait_for(task, 10)
                assert res["ok"] and res["policy"]["decision"] == "approved"

                # 本物と同じ判定: リレーの5秒 ON は承認でも覆らない
                r = await agent.exec("gpio.write", {"line": 17, "value": 1, "hold_ms": 5000})
                assert r["error"]["code"] == "policy_denied"
    asyncio.run(go())


def test_virtual_devices_are_capped_and_expire(signing_key):
    async def go():
        async with VCloud(signing_key, ttl_s=1, max_devices=1) as c:
            async with c.browser() as a, c.browser() as b:
                dev = await c.make_virtual(a)
                r = await b.post("/devices/virtual", data={"csrf": await c.csrf(b)})
                assert r.headers["location"] == "/?notice=virtual_full"
                await asyncio.sleep(1.2)
                c.fleet.reap()
                await until(lambda: dev not in c.gw_app.state.hub.conns, timeout=10)
                assert dev not in (await a.get("/")).text                    # 期限切れは一覧から消える
                assert (await c.store.get_device(dev))["online"] is False
    asyncio.run(go())


def test_virtual_device_can_be_deleted_by_its_owner_and_recreated(signing_key):
    async def go():
        async with VCloud(signing_key) as c:
            async with c.browser() as a, c.browser() as b:
                dev = await c.make_virtual(a)
                assert f'action="/devices/{dev}/delete"' in (await a.get("/")).text
                r = await b.post(f"/devices/{dev}/delete", data={"csrf": await c.csrf(b)})
                assert r.status_code == 403                                  # 他のセッションは消せない
                r = await a.post(f"/devices/{dev}/delete", data={"csrf": await c.csrf(a)})
                assert r.headers["location"] == "/?notice=virtual_deleted"
                await until(lambda: dev not in c.gw_app.state.hub.conns, timeout=10)
                assert dev not in c.fleet.devices
                page = (await a.get("/")).text
                assert dev not in page and "仮想デバイスで試す" in page        # 消えて、作り直せる
                assert await c.make_virtual(a) != dev
    asyncio.run(go())


def test_real_device_cannot_be_deleted_from_the_ui(signing_key):
    async def go():
        async with VCloud(signing_key) as c:
            await c.store.update_device("kusupi5", {"online": True, "owner": "user:pi"})
            async with c.browser() as a:
                assert 'action="/devices/kusupi5/delete"' not in (await a.get("/")).text
                r = await a.post("/devices/kusupi5/delete", data={"csrf": await c.csrf(a)})
                assert r.status_code == 403
    asyncio.run(go())


def test_virtual_manifest_has_only_the_production_key():
    from services.virtual.app import virtual_manifest
    m = virtual_manifest("virt-abcd", "wss://gw.example/v1/device", [{"kid": "api-2026-10", "key": "k" * 43}])
    assert [k["kid"] for k in m["approval"]["public_keys"]] == ["api-2026-10"]     # 例専用の鍵は入れない
    assert m["device"]["board"] == "virtual" and [ln["line"] for ln in m["gpio"]["lines"]] == [17, 27]
