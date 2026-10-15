"""「現在の状態」: Gateway が追う状態の写しと、テスター表示。"""
import asyncio
import contextlib
import re

from cloudrig import DeviceProc, Server, client, until
from conftest import T0_S
from test_web import PW

from mugemuge_agentd.approval import sign_grant
from mugemuge_agentd.hw.virtual import Environment, virtual_i2c
from services.common.auth import CallerAuth
from services.common.store import MemoryStore
from services.gateway.app import create_app as create_gateway
from services.gateway.devstate import DeviceView
from services.web.app import GatewayClient, create_app as create_web
from services.web.security import Sessions, hash_password


def test_view_follows_results_and_events(manifest):
    v = DeviceView(manifest, {"estop": False, "bindings": [], "jobs": [], "outputs": [{"line": 17, "value": 0}]})
    v.apply_result({"op": "gpio.write", "args": {"line": 17, "value": 1}},
                   {"ok": True, "data": {"value": 1, "revert_at_ms": 5}})
    assert v.state()["outputs"] == [{"line": 17, "value": 1, "revert_at_ms": 5}]
    v.apply_event({"event": "output_reverted", "detail": {"line": 17, "value": 0}})
    assert v.state()["outputs"] == [{"line": 17, "value": 0}]
    bind = {"bus": 1, "addr": 0x76, "part": "BMP280", "fingerprint": {"reg": 0xD0, "len": 1, "hex": "58"},
            "write_allow_regs": [0xF4]}
    v.apply_result({"op": "i2c.bind", "args": bind}, {"ok": True, "data": {"bound": True}})
    v.apply_result({"op": "i2c.scan", "args": {"bus": 1}}, {"ok": True, "ts": 9, "data": {"bus": 1, "found": [0x76]}})
    v.apply_result({"op": "sample.start", "args": {}}, {"ok": True, "data": {"job_id": "job_1"}})
    st = v.state()
    assert st["bindings"][0]["part"] == "BMP280" and st["jobs"] == ["job_1"] and v.i2c_seen["1"]["found"] == [0x76]
    v.apply_event({"event": "binding_dropped", "detail": {"bus": 1, "addr": 0x76}})
    v.apply_event({"event": "job_end", "detail": {"job_id": "job_1"}})
    v.apply_result({"op": "i2c.write", "args": {}}, {"ok": False, "error": {"code": "policy_denied"}})
    assert v.state()["bindings"] == [] and v.state()["jobs"] == []
    v.apply_result({"op": "gpio.write", "args": {"line": 17, "value": 1}}, {"ok": True, "data": {"value": 1}})
    v.apply_result({"op": "sys.estop", "args": {}}, {"ok": True, "data": {"estop": True}})
    assert v.state()["estop"] is True and v.state()["outputs"] == [{"line": 17, "value": 0}]


class Rig:
    def __init__(self, manifest, signing_key, tmp_path):
        self.manifest, self.key, self.tmp_path = manifest, signing_key, tmp_path
        self.store = MemoryStore()

    async def __aenter__(self):
        insecure = CallerAuth(None, insecure=True)
        self.gw_app = create_gateway(self.store, insecure, heartbeat_ms=1000)
        self.gw = await Server(self.gw_app).start()
        self.gwc = GatewayClient(self.gw.url, use_id_token=False)
        self.web = await Server(create_web(self.store, insecure, users={"judge": hash_password(PW, n=2 ** 10)},
                                           sessions=Sessions(b"s" * 32), signing_key=self.key, kid="test-key",
                                           gateway=self.gwc, cookie_secure=False)).start()
        await self.store.create_pairing_code("123456", "user:judge", 2_000_000_000_000)
        async with client(self.gw.url) as c:
            token = (await c.post("/v1/pair", json={"code": "123456", "device_id": "pi-desk-01"})).json()["token"]
        self.dev = DeviceProc(self.manifest, self.gw.port, token, self.tmp_path / "audit.jsonl")
        self.dev.d.executor.i2c = self.dev.d.sampler.i2c = virtual_i2c(Environment())
        await self.dev.start()
        await until(lambda: "pi-desk-01" in self.gw_app.state.hub.conns)
        return self

    async def __aexit__(self, *exc):
        await self.dev.stop()
        await self.web.stop()
        await self.gw.stop()

    async def exec(self, op, args, approve=False):
        body = {"device_id": "pi-desk-01", "op": op, "args": args, "run_id": "r", "step_id": "s"}
        st, r = await self.gwc.exec(body)
        if approve and not r["ok"] and r["error"]["code"] == "approval_required":
            req = r["error"]["request"]
            body["approval"] = sign_grant(self.key, kid="test-key", device_id=req["device_id"],
                                          session_id=req["session_id"], op=op, args=args, approved_by="user:t",
                                          iat=self.dev.d.clock.corrected_s())
            st, r = await self.gwc.exec(body)
        assert r["ok"], r
        return r

    @contextlib.asynccontextmanager
    async def browser(self):
        async with client(self.web.url) as b:
            await b.post("/login", data={"uid": "judge", "password": PW})
            yield b


def test_state_and_tester(manifest, signing_key, tmp_path):
    async def go():
        async with Rig(manifest, signing_key, tmp_path) as r:
            await r.exec("i2c.scan", {"bus": 1})
            await r.exec("i2c.bind", {"bus": 1, "addr": 0x76, "part": "BMP280",
                                      "fingerprint": {"reg": 0xD0, "len": 1, "hex": "58"},
                                      "write_allow_regs": [0xF4, 0xF5]}, approve=True)
            serial = (await r.exec("i2c.read", {"bus": 1, "addr": 0x44, "reg": 0x3780, "len": 6, "reg_width": 16},
                                   approve=True))["data"]["hex"]
            await r.exec("i2c.bind", {"bus": 1, "addr": 0x44, "part": "SHT30",
                                      "fingerprint": {"reg": 0x3780, "len": 6, "hex": serial},
                                      "write_allow_regs": [0x22, 0xE000]}, approve=True)
            async with r.browser() as b:
                st = (await b.get("/ui/devices/pi-desk-01/state")).json()
                assert st["online"] and st["i2c_seen"]["1"]["found"] == [0x40, 0x44, 0x76]
                assert {x["part"] for x in st["state"]["bindings"]} == {"BMP280", "SHT30"}
                assert [ln["line"] for ln in st["gpio_lines"]] == [17, 27]

                # 設定前: BMP280 はスリープ、SHT30 は定期計測でない
                t = (await b.get("/ui/devices/pi-desk-01/tester")).json()
                notes = {x["part"]: x["note"] for x in t["readings"]}
                assert "スリープ" in notes["BMP280"] and "計測モード" in notes["SHT30"]
                assert {g["line"]: g["value"] for g in t["gpio"]} == {17: 0, 27: 1}

                await r.exec("i2c.write", {"bus": 1, "addr": 0x76, "reg": 0xF4, "hex": "27"})
                await r.exec("i2c.write", {"bus": 1, "addr": 0x44, "reg": 0x22, "hex": "36"})
                t = (await b.get("/ui/devices/pi-desk-01/tester")).json()
                vals = {x["part"]: x["values"] for x in t["readings"]}
                assert 20 < vals["BMP280"]["temperature_c"] < 30 and 1000 < vals["BMP280"]["pressure_hpa"] < 1010
                assert 20 < vals["SHT30"]["temperature_c"] < 30 and 40 < vals["SHT30"]["humidity_pct"] < 80

                # リレーを入れると状態に出て、自動 OFF で戻る
                await r.exec("gpio.write", {"line": 17, "value": 1, "hold_ms": 300}, approve=True)
                outs = (await b.get("/ui/devices/pi-desk-01/state")).json()["state"]["outputs"]
                assert outs[0]["value"] == 1 and "revert_at_ms" in outs[0]
                await asyncio.sleep(0.6)
                outs = (await b.get("/ui/devices/pi-desk-01/state")).json()["state"]["outputs"]
                assert outs == [{"line": 17, "value": 0}]
    asyncio.run(go())


def test_tester_ina226_shunt_setting(manifest, signing_key, tmp_path):
    """INA226 の電流は、人がテスター画面でシャント抵抗を設定するまで出さない（基板で 0.1Ω・5mΩ・80mΩ と違う）。
    設定はデバイスごとにクラウドに残り、INA226 として登録したアドレスにだけ付けられる。"""
    import re

    async def go():
        async with Rig(manifest, signing_key, tmp_path) as r:
            await r.exec("i2c.bind", {"bus": 1, "addr": 0x40, "part": "INA226",
                                      "fingerprint": {"reg": 0xFE, "len": 2, "hex": "5449"},
                                      "write_allow_regs": [0, 5]}, approve=True)
            async with r.browser() as b:
                csrf = re.search(r'name="csrf" value="([^"]+)"', (await b.get("/")).text).group(1)
                ina = (await b.get("/ui/devices/pi-desk-01/tester")).json()["readings"][0]
                assert ina["shunt_ohm"] is None and "current_ma" not in ina["values"] and "シャント抵抗" in ina["note"]
                assert 4 < ina["values"]["bus_v"] < 6 and ina["values"]["shunt_mv"] > 0

                url = "/ui/devices/pi-desk-01/part-params"
                assert (await b.post(url, json={"bus": 1, "addr": 0x40, "shunt_ohm": 0.1})).status_code == 403   # CSRF
                h = {"x-csrf": csrf}
                assert (await b.post(url, json={"bus": 1, "addr": 0x76, "shunt_ohm": 0.1}, headers=h)).status_code == 400
                assert (await b.post(url, json={"bus": 1, "addr": 0x40, "shunt_ohm": 0}, headers=h)).status_code == 400
                assert (await b.post(url, json={"bus": 1, "addr": 0x40, "shunt_ohm": 0.1}, headers=h)).status_code == 200
                ina = (await b.get("/ui/devices/pi-desk-01/tester")).json()["readings"][0]
                assert ina["shunt_ohm"] == 0.1 and 50 < ina["values"]["current_ma"] < 400 and ina["note"] is None
    asyncio.run(go())
