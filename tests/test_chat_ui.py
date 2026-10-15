"""チャット画面の中継（Web/API → Agent）。チャットは始めたブラウザのセッションだけが読み書きできる。"""
import asyncio
import contextlib
import json
import re

from cloudrig import DeviceProc, Server, client, until
from test_agent import ScriptedLlm, call, last_response, say
from test_web import PW

from services.agent.agent import Runtime
from services.agent.app import AgentService, create_app as create_agent
from services.agent.device_tools import CloudClient
from services.common.auth import CallerAuth
from services.common.store import MemoryStore
from services.gateway.app import create_app as create_gateway
from services.web.app import AgentClient, GatewayClient, create_app as create_web
from services.web.security import Sessions, hash_password


class UI:
    def __init__(self, manifest, signing_key, tmp_path):
        self.manifest, self.key, self.tmp_path = manifest, signing_key, tmp_path
        self.store = MemoryStore()

    async def __aenter__(self):
        insecure = CallerAuth(None, insecure=True)
        self.gw_app = create_gateway(self.store, insecure, heartbeat_ms=1000)
        self.gw = await Server(self.gw_app).start()
        self.web_port_holder = Server(None)          # Web の URL を先に決める（Agent が承認 API を呼ぶため）
        self.llm = ScriptedLlm(steps=[], seen=[])
        self.svc = AgentService(Runtime(CloudClient(self.gw.url, self.web_port_holder.url)), model_obj=self.llm)
        self.agent = await Server(create_agent(self.svc)).start()
        self.web_app = create_web(self.store, insecure, users={"judge": hash_password(PW, n=2 ** 10)},
                                  sessions=Sessions(b"s" * 32), signing_key=self.key, kid="test-key",
                                  gateway=GatewayClient(self.gw.url, use_id_token=False), cookie_secure=False,
                                  agent=AgentClient(self.agent.url, use_id_token=False))
        self.web = Server(self.web_app, port=self.web_port_holder.port)
        await self.web.start()
        await self.store.create_pairing_code("123456", "user:judge", 2_000_000_000_000)
        async with client(self.gw.url) as c:
            token = (await c.post("/v1/pair", json={"code": "123456", "device_id": "pi-desk-01"})).json()["token"]
        self.dev = await DeviceProc(self.manifest, self.gw.port, token, self.tmp_path / "audit.jsonl").start()
        await until(lambda: "pi-desk-01" in self.gw_app.state.hub.conns)
        return self

    async def __aexit__(self, *exc):
        await self.dev.stop()
        await self.svc.rt.cloud.aclose()
        for s in (self.web, self.agent, self.gw):
            await s.stop()

    @contextlib.asynccontextmanager
    async def browser(self):
        async with client(self.web.url) as b:
            await b.post("/login", data={"uid": "judge", "password": PW})
            yield b


def test_chat_through_web_with_inline_approval(manifest, signing_key, tmp_path):
    async def go():
        async with UI(manifest, signing_key, tmp_path) as u:
            async with u.browser() as a, u.browser() as other:
                page = (await a.get("/")).text
                assert "エージェントと話す" in page
                assert 'data-busy="エージェントを準備しています' in page       # 押した瞬間に準備中の表示に変わる
                csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
                r = await a.post("/chat/pi-desk-01", data={"csrf": csrf})
                assert r.status_code == 303
                url = r.headers["location"]
                chat_id = url.rsplit("/", 1)[1]
                html = (await a.get(url)).text
                cfg = json.loads(re.search(r'id="cfg">(.*?)</script>', html).group(1))
                assert cfg["chat"] == chat_id and cfg["csrf"] == csrf

                # CSRF なしの送信は拒否
                assert (await a.post(f"/ui/chat/{chat_id}/messages", json={"text": "x"})).status_code == 403

                u.llm.steps = [call("gpio_write", line=17, value=1, hold_ms=300),
                               lambda req: call("wait_for_approval", approval_id=last_response(req)["approval_id"]),
                               say("リレーを 0.3 秒入れました")]
                r = await a.post(f"/ui/chat/{chat_id}/messages", json={"text": "リレーを入れて"},
                                 headers={"x-csrf": csrf})
                assert r.status_code == 200 and r.json()["status"] == "waiting_approval"
                ev = (await a.get(f"/ui/chat/{chat_id}/events")).json()
                assert ev["events"][0] == {**ev["events"][0], "author": "user", "text": "リレーを入れて"}
                res = next(x for e in ev["events"] for x in e.get("results", []) if x["name"] == "gpio_write")
                aid = res["response"]["approval_id"]
                assert res["response"]["trace_id"] is None or isinstance(res["response"]["trace_id"], str)

                card = (await a.get(f"/ui/approvals/{aid}")).json()
                assert card["status"] == "pending" and "relay-1" in card["request"]["summary"]

                # 別のブラウザからはこのチャットは見えない
                assert (await other.get(f"/ui/chat/{chat_id}/events")).status_code == 404
                assert (await other.get(url)).status_code == 404

                # 画面の承認ボタン（フォームの POST）→ Agent に通知 → 再開
                await a.post(f"/approvals/{aid}/approve", data={"csrf": csrf})
                await until(lambda: u.svc.logs[chat_id].status == "idle", timeout=5)
                ev = (await a.get(f"/ui/chat/{chat_id}/events")).json()
                assert ev["events"][-1]["text"] == "リレーを 0.3 秒入れました"
                assert (await a.get(f"/ui/approvals/{aid}")).json()["status"] == "approved"
    asyncio.run(go())
