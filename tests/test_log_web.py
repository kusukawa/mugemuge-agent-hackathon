"""記録ページ: 換算・間引き・範囲・CSV・写真の中継と、見られる範囲。エージェントの log_start で換算方法が登録されること。"""
import asyncio
import contextlib
import re
import struct

from cloudrig import Server, client
from test_agent import AgentRig, call, say
from test_web import PW

from google.genai import types


def call_args(tool, args):
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=tool, args=args))])

from services.common.auth import CallerAuth
from services.common.blobs import MemoryBlobStore
from services.common.store import MemoryStore
from services.web.app import create_app as create_web
from services.web.logdata import downsample, series_for_view, to_csv
from services.web.security import Sessions, hash_password

CAL = "e96b8b6632002c939dd7d00bd023c1fef9ff8c3cf8c67017"     # kusupi5 の BMP280
RAW = "4835007f4300"                                         # 24.81℃ / 1004.50hPa
T0 = 1_791_300_000_000


def meta(**kw):
    return {"log_id": "log_abc", "name": "室温", "columns": ["t_ms", "env"], "started_ts": T0, "status": "running",
            "args": {"interval_ms": 60_000, "duration_ms": 86_400_000, "reads": [],
                     "photo": {"camera": "cam0", "interval_ms": 600_000}},
            "decoders": {"env": {"decoder": "bmp280", "calibration_hex": CAL, "part": "BMP280"}}, **kw}


def batches(n=120):
    return [{"seq": i, "started_ts": T0, "rows": [{"t": i * 60_000, "v": [RAW]}],
             "photos": [{"t": i * 60_000, "blob_id": f"blob_{i:010d}"}] if i % 10 == 0 else []} for i in range(n)]


def test_series_decode_downsample_and_csv():
    v = series_for_view(meta(), batches(), None, None, max_points=30)
    temp = next(s for s in v["series"] if s["channel"] == "temperature_c")
    assert len(temp["points"]) <= 31 and temp["points"][0][1] == 24.81
    assert v["latest"]["env"]["pressure_hpa"] == 1004.5 and len(v["photos"]) == 12 and v["rows"] == 120
    last_hour = series_for_view(meta(), batches(), T0 + 60 * 60_000, T0 + 119 * 60_000)
    assert last_hour["rows"] == 60 and len(last_hour["photos"]) == 6
    assert downsample([(i, float(i)) for i in range(100)], 0, 99, 10)[0] == [5, 4.5, 0.0, 9.0]
    csv = to_csv(meta(), batches(3), lambda b: f"https://x/{b}")
    lines = csv.strip().splitlines()
    assert lines[0] == "time_jst,t_ms,env.pressure_hpa,env.temperature_c,env.raw_hex,photo_url"
    assert lines[1].split(",")[1:] == ["0", "1004.5", "24.81", RAW, "https://x/blob_0000000000"]


def test_undecodable_columns_are_reported():
    v = series_for_view(meta(decoders={}), batches(5), None, None)
    assert v["series"] == [] and v["not_decodable"] == ["env"]


class Web:
    def __init__(self, signing_key):
        self.store, self.blobs = MemoryStore(), MemoryBlobStore()
        self.key = signing_key

    async def __aenter__(self):
        self.app = create_web(self.store, CallerAuth(None, insecure=True), users={"judge": hash_password(PW, n=2 ** 10)},
                              sessions=Sessions(b"s" * 32), signing_key=self.key, kid="test-key", gateway=None,
                              cookie_secure=False, blobs=self.blobs)
        self.srv = await Server(self.app).start()
        return self

    async def __aexit__(self, *exc):
        await self.srv.stop()

    @contextlib.asynccontextmanager
    async def browser(self):
        async with client(self.srv.url) as b:
            await b.post("/login", data={"uid": "judge", "password": PW})
            yield b


def png():
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 0)


def test_log_pages_and_photo_proxy(signing_key):
    async def go():
        async with Web(signing_key) as w:
            s = w.store
            await s.update_device("pi-1", {"device_id": "pi-1", "online": True,
                                           "manifest": {"device": {"name": "卓上"}}})
            await s.put_log("pi-1", "log_abc", {**meta(), "rows": 120, "photos": 12,
                                                "last_row": {"t": 0, "v": [RAW]},
                                                "last_photo": {"t": 0, "blob_id": "blob_0000000000"}})
            for b in batches():
                await s.put_log_batch("pi-1", "log_abc", b)
            await w.blobs.put("devices/pi-1/blob_0000000000.png", png(), "image/png")
            await s.put_blob_meta("pi-1", "blob_0000000000", {"path": "devices/pi-1/blob_0000000000.png",
                                                             "content_type": "image/png"})
            # 別のブラウザのセッションの仮想デバイス（見えない）
            await s.update_device("virt-x", {"device_id": "virt-x", "virtual": True, "owner": "vsession:someone"})
            await s.put_log("virt-x", "log_v", meta(log_id="log_v"))
            async with w.browser() as b:
                page = (await b.get("/logs/pi-1")).text
                assert "室温" in page and "記録中" in page and "気温（env）" in page
                assert re.search(r'src="/ui/devices/pi-1/blobs/blob_0000000000"', page)
                det = (await b.get("/logs/pi-1/log_abc")).text
                assert 'id="scrub"' in det and "CSV" in det
                assert "1分ごと" in det and "写真 10分ごと" in det and "欠落" not in det
                await s.put_log("pi-1", "log_abc", {"dropped": [[3, 5], [9, 9]], "photos_paused_ts": T0})
                det = (await b.get("/logs/pi-1/log_abc")).text
                assert "欠落" in det and "バッチ 3〜5、9" in det and "写真の撮影を止めた" in det
                d = (await b.get("/ui/devices/pi-1/logs/log_abc/data", params={"span": 3_600_000})).json()
                assert d["rows"] == 61 and d["range"][1] - d["range"][0] == 3_600_000
                r = await b.get("/ui/devices/pi-1/logs/log_abc.csv")
                assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
                assert r.text.lstrip("﻿").startswith("time_jst,")
                img = await b.get("/ui/devices/pi-1/blobs/blob_0000000000")
                assert img.status_code == 200 and img.content == png() and img.headers["content-type"] == "image/png"
                assert (await b.get("/ui/devices/pi-1/blobs/blob_missing00")).status_code == 404
                assert (await b.get("/logs/virt-x")).status_code == 404
                assert (await b.get("/ui/devices/virt-x/logs/log_v/data")).status_code == 404
            async with client(w.srv.url) as anon:
                assert (await anon.get("/ui/devices/pi-1/blobs/blob_0000000000")).status_code in (303, 401)
    asyncio.run(go())


def test_agent_log_start_registers_decoders(manifest, signing_key, tmp_path):
    from mugemuge_agentd.hw.virtual import Environment, virtual_i2c
    from mugemuge_agentd.policy import Binding

    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            r.dev.d.executor.i2c = r.dev.d.logger.i2c = virtual_i2c(Environment())
            tk = await r.svc.rt.toolkit("pi-desk-01")
            b = Binding(1, 0x76, "BMP280", {"reg": 0xD0, "len": 1, "hex": "58"}, [0xF4, 0xF5], last_verified_ms=10 ** 13)
            tk.bindings[(1, 0x76)] = b
            r.dev.d.state.bindings[(1, 0x76)] = b
            reads = [{"name": "env", "bus": 1, "addr": 0x76, "reg": 0xF7, "length": 6},
                     {"name": "raw40", "bus": 1, "addr": 0x40, "reg": 2, "length": 2}]
            r.llm.steps = [call("i2c_read", bus=1, addr=0x76, reg=0x88, length=24),
                           call_args("log_start", {"name": "一晩の室温", "interval_s": 1, "duration_min": 60, "reads": reads}),
                           say("記録を始めました")]
            await r.svc.say(r.sid, "一晩記録して")
            res = r.llm.seen[-1]["log_start"]
            assert res["status"] == "ok" and res["data"]["decodable"] == ["env"] and res["data"]["not_decodable"] == ["raw40"]
            log_id = res["data"]["log_id"]
            for _ in range(50):
                m = await r.store.get_log("pi-desk-01", log_id)
                if m and m.get("decoders"):
                    break
                await asyncio.sleep(0.05)
            assert m["decoders"]["env"]["decoder"] == "bmp280" and m["decoders"]["env"]["calibration_hex"]
            assert m["name"] == "一晩の室温" and m["status"] == "running"
            await tk.run_op("log_stop", {"log_id": log_id}, adk_session_id=r.sid, user_id="u", run_id="r", step_id="s")
    asyncio.run(go())
