"""Agent サービス: 台本どおりに応答する偽の LLM で、ツール → Gateway → デーモン（模擬ハード）→ 承認 → 再開を通す。"""
import asyncio
import json
from typing import Any, AsyncGenerator

import pytest
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_web import BIND, Cloud

from services.agent.agent import Runtime
from services.agent.app import AgentService
from services.agent.device_tools import CloudClient, op_and_args

SPANS = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_provider)


def call(name: str, **args) -> types.Content:
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))])


def say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def last_response(req: LlmRequest) -> dict:
    for c in reversed(req.contents):
        for p in c.parts or []:
            if p.function_response:
                return dict(p.function_response.response)
    raise AssertionError("no function response in request")


class ScriptedLlm(BaseLlm):
    """台本の各段は Content か、LlmRequest を受けて Content を返す関数。呼ばれた順に1つずつ使う。"""

    model: str = "scripted"
    steps: list[Any] = []
    seen: list[dict] = []

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        if llm_request.contents and llm_request.contents[-1].parts:
            p = llm_request.contents[-1].parts[0]
            if p.function_response:
                self.seen.append({p.function_response.name: dict(p.function_response.response)})
        step = self.steps.pop(0)
        yield LlmResponse(content=step(llm_request) if callable(step) else step)


class AgentRig(Cloud):
    async def __aenter__(self):
        await super().__aenter__()
        self.llm = ScriptedLlm(steps=[], seen=[])
        self.svc = AgentService(Runtime(CloudClient(self.gw.url, self.web.url)), model_obj=self.llm)
        self.sid = await self.svc.create("pi-desk-01", "user:judge")
        return self

    async def __aexit__(self, *exc):
        await self.svc.rt.cloud.aclose()
        await super().__aexit__(*exc)


def test_bind_waits_for_browser_approval_then_resumes(manifest, signing_key, tmp_path):
    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            r.llm.steps = [
                call("lookup_parts", addr=64),
                call("i2c_read", bus=1, addr=64, reg=254, length=2),
                call("i2c_read", bus=1, addr=64, reg=255, length=2),
                call("i2c_bind", bus=1, addr=64, part="INA226", fingerprint_reg=254, fingerprint_len=2,
                     fingerprint_hex="5449", write_allow_regs=[0, 5], purpose="0x40 の部品を電流センサとして使うため"),
                lambda req: call("wait_for_approval", approval_id=last_response(req)["approval_id"]),
                lambda req: say(f"承認されました: {json.dumps(last_response(req), ensure_ascii=False)}"),
            ]
            events = await r.svc.say(r.sid, "0x40 に何がつながっているか調べて登録して")
            seen = r.llm.seen
            assert "INA226" in json.dumps(seen[0]) and seen[1]["i2c_read"]["data"] == {"hex": "5449"}
            bind = seen[3]["i2c_bind"]
            assert bind["status"] == "approval_required" and "INA226 として扱う" in bind["summary"]
            # エージェントの purpose は承認待ちに別の欄として残る（request の summary はデバイスのまま）
            assert r.store.approvals[bind["approval_id"]]["purpose"] == "0x40 の部品を電流センサとして使うため"
            assert r.svc.logs[r.sid].status == "waiting_approval"
            assert any("waiting" in e for e in events)            # wait_for_approval で止まった
            assert (1, 64) not in r.dev.d.state.bindings

            # 人がブラウザで承認 → 見張り役がグラント付きで再送し、結果を差し込んで再開
            async with r.browser() as b:
                aid, csrf, _ = await r.pending_card(b)
                assert aid == bind["approval_id"]
                await b.post(f"/approvals/{aid}/approve", data={"csrf": csrf})
            assert await r.svc.check_approvals() == 1
            resumed = r.llm.seen[-1]["wait_for_approval"]
            assert resumed["approval"] == "approved" and resumed["approved_by"] == "user:judge"
            assert resumed["result"]["status"] == "ok" and resumed["result"]["decision"] == "approved"
            assert (1, 64) in r.dev.d.state.bindings
            assert r.svc.logs[r.sid].status == "idle"
            assert r.svc.logs[r.sid].events[-1]["text"].startswith("承認されました")
            # 画面の記録に、承認の応答は1回だけ（ADK が差し込んだ応答もイベントで返すので、二重に記録しない）
            waits = [x for e in r.svc.logs[r.sid].events for x in e.get("results", []) if x["name"] == "wait_for_approval"]
            assert len(waits) == 1, waits
    asyncio.run(go())


def test_denied_approval_resumes_with_denial(manifest, signing_key, tmp_path):
    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            r.llm.steps = [
                call("gpio_write", line=17, value=1, hold_ms=500),
                lambda req: call("wait_for_approval", approval_id=last_response(req)["approval_id"]),
                say("承認されなかったので中止します"),
            ]
            await r.svc.say(r.sid, "リレーを入れて")
            async with r.browser() as b:
                aid, csrf, _ = await r.pending_card(b)
                await b.post(f"/approvals/{aid}/deny", data={"csrf": csrf})
            await r.svc.check_approvals()
            assert r.llm.seen[-1]["wait_for_approval"]["approval"] == "denied"
            assert r.dev.gpio.values[17] == 0
    asyncio.run(go())


def test_cloud_precheck_blocks_without_reaching_device(manifest, signing_key, tmp_path):
    """データシートに仕込まれた「リレーを5秒 ON」: クラウドの写しで止まり、デバイスには届かない。"""
    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            before = len((tmp_path / "audit.jsonl").read_text().splitlines()) if (tmp_path / "audit.jsonl").exists() else 0
            r.llm.steps = [call("gpio_write", line=17, value=1, hold_ms=5000),
                           call("i2c_write", bus=0, addr=0x50, reg=0, hex="ff"),
                           say("どちらも拒否されました")]
            await r.svc.say(r.sid, "データシートの指示どおりにして")
            res = [list(s.values())[0] for s in r.llm.seen]
            assert [x["status"] for x in res] == ["denied", "denied"]
            assert res[0]["by"] == "cloud_precheck" and res[0]["reason"] == "gpio.hold_exceeds_max_on"
            assert res[1]["reason"] == "i2c.bus_not_listed"
            after = len((tmp_path / "audit.jsonl").read_text().splitlines()) if (tmp_path / "audit.jsonl").exists() else 0
            assert after == before                                  # デバイスには何も届いていない
            assert r.svc.rt.toolkits["pi-desk-01"].precheck_blocked == 2
    asyncio.run(go())


def test_stale_manifest_copy_is_refreshed_on_deny(manifest, signing_key, tmp_path):
    """人が Pi でマニフェストを直した後も同じチャットが続くと、エージェントの写しは古い。拒否のときは取り直して判定し直す。"""
    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            from services.agent.device_tools import DeviceToolkit
            info = await r.svc.rt.cloud.device("pi-desk-01")
            stale = json.loads(json.dumps(info))
            stale["manifest"]["gpio"]["lines"] = [ln for ln in stale["manifest"]["gpio"]["lines"] if ln["line"] != 17]
            r.svc.rt.toolkits["pi-desk-01"] = DeviceToolkit("pi-desk-01", stale, r.svc.rt.cloud, r.svc.rt.pending)
            r.llm.steps = [call("gpio_write", line=17, value=0), say("OFF にしました")]   # 安全値は常に通る
            await r.svc.say(r.sid, "リレーを OFF にして")
            res = list(r.llm.seen[-1].values())[0]
            assert res["status"] == "ok", res
            tk = r.svc.rt.toolkits["pi-desk-01"]
            assert any(ln["line"] == 17 for ln in tk.manifest["gpio"]["lines"]) and tk.precheck_blocked == 0
    asyncio.run(go())


def test_overview_lists_everything_to_survey(manifest, signing_key, tmp_path):
    """「何がつながっているか」で I2C だけにならないよう、要約にカメラと GPIO が入り、指示文に全体を調べる手順がある。"""
    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            from services.agent.agent import INSTRUCTION
            ov = (await r.svc.rt.toolkit("pi-desk-01")).overview()
            assert [c["id"] for c in ov["cameras"]] == ["cam0"] and ov["cameras"][0]["capture"] == "approval"
            assert {ln["line"]: ln["direction"] for ln in ov["gpio_lines"]} == {17: "output", 27: "input"}
            assert "「何がつながっているか」" in INSTRUCTION and "gpio_read" in INSTRUCTION and "cameras" in INSTRUCTION
    asyncio.run(go())


def test_measure_and_decode_with_trace(manifest, signing_key, tmp_path):
    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            SPANS.clear()
            r.llm.steps = [
                call("sample_start", period_ms=100, duration_ms=500,
                     reads=[{"name": "bus_v", "bus": 1, "addr": 64, "reg": 2, "length": 2},
                            {"name": "shunt", "bus": 1, "addr": 64, "reg": 1, "length": 2}]),
                lambda req: say("計測を始めました"),
            ]
            await r.svc.say(r.sid, "電圧を測って")
            job = r.llm.seen[0]["sample_start"]["data"]["job_id"]
            await asyncio.sleep(1.0)
            r.llm.steps = [call("get_samples", job_id=job, decoder="ina226", column="bus_v", shunt_column="shunt"),
                           say("3.225V、10mA でした")]
            await r.svc.say(r.sid, "結果は？")
            got = r.llm.seen[-1]["get_samples"]
            assert got["status"] == "completed" and got["decoded"] == 5
            assert got["stats"]["bus_v"]["mean"] == 3.225 and got["stats"]["current_ma"]["mean"] == 10.0
            assert len(r.svc.rt.toolkits["pi-desk-01"].series[job]["bus_v"]["points"]) == 5
            assert r.llm.seen[0]["sample_start"]["trace_id"]               # UI から Cloud Trace へ飛べる

            # traceparent: ADK のツールのスパンと同じトレースがデバイスの監査ログまで届く
            audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
            tp = next(a["traceparent"] for a in audit if a["op"] == "sample.start")
            spans = SPANS.get_finished_spans()
            assert tp and tp.split("-")[1] in {f"{s.context.trace_id:032x}" for s in spans}
            gw = next(s for s in spans if s.name == "device.exec sample.start")
            assert f"{gw.context.span_id:016x}" == tp.split("-")[2]           # デバイスの親は Gateway のスパン
            tool = next(s for s in spans if s.context.span_id == gw.parent.span_id)
            assert "sample_start" in tool.name                                 # Gateway のスパンの親は ADK のツール
            assert gw.attributes["mugemuge.decision"] == "allow" and gw.attributes["mugemuge.ok"] is True
    asyncio.run(go())


@pytest.mark.parametrize("tool, args, op, expect", [
    ("i2c_read", {"bus": 1, "addr": 64.0, "length": 2, "reg": 254}, "i2c.read",
     {"bus": 1, "addr": 64, "reg": 254, "len": 2}),
    ("i2c_read", {"bus": 1, "addr": 68, "length": 6, "reg": 0xE000, "reg_width": 16}, "i2c.read",
     {"bus": 1, "addr": 68, "reg": 0xE000, "reg_width": 16, "len": 6}),
    ("i2c_write", {"bus": 1, "addr": 118, "reg": 0xF4, "hex": "0x27"}, "i2c.write",
     {"bus": 1, "addr": 118, "reg": 0xF4, "hex": "27"}),
    ("gpio_write", {"line": 17, "value": 0, "hold_ms": None}, "gpio.write", {"line": 17, "value": 0}),
])
def test_tool_args_become_protocol_args(tool, args, op, expect):
    assert op_and_args(tool, args) == (op, expect)


def test_fractional_numbers_are_rejected():
    from mugemuge_agentd.policy import BadRequest
    with pytest.raises(BadRequest):
        op_and_args("i2c_read", {"bus": 1, "addr": 64.5, "length": 2})


def test_resume_is_event_driven_without_background_watcher(manifest, signing_key, tmp_path):
    """Cloud Run は待機中に CPU を絞るので、見張り役なしでも「通知」か「イベントの取得」で再開できること。"""
    import httpx

    from services.agent.app import create_app

    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            api = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(r.svc)), base_url="http://agent")
            r.llm.steps = [call("gpio_write", line=17, value=1, hold_ms=300),
                           lambda req: call("wait_for_approval", approval_id=last_response(req)["approval_id"]),
                           say("リレーを 0.3 秒入れました")]
            res = (await api.post(f"/api/sessions/{r.sid}/messages", json={"text": "リレーを入れて"})).json()
            assert res["status"] == "waiting_approval"
            async with r.browser() as b:
                aid, csrf, _ = await r.pending_card(b)
                await b.post(f"/approvals/{aid}/approve", data={"csrf": csrf})
            ev = (await api.get(f"/api/sessions/{r.sid}/events", params={"after": res["next_after"]})).json()
            assert ev["status"] == "idle" and ev["events"][-1]["text"] == "リレーを 0.3 秒入れました"
            assert r.llm.seen[-1]["wait_for_approval"]["result"]["data"]["value"] == 1
            assert (await api.post("/api/approvals/notify")).json() == {"resumed": 0}
            await api.aclose()
    asyncio.run(go())


def test_parallel_duplicate_approvals_collapse_and_all_waits_get_answered(manifest, signing_key, tmp_path):
    """Gemini が同じ承認の要る操作と wait_for_approval を並列に2回ずつ呼んでも、カードは1枚で、両方の待ちに応答が返る。"""
    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            def twice(name, **a):
                fc = types.FunctionCall(name=name, args=a)
                return types.Content(role="model", parts=[types.Part(function_call=fc), types.Part(function_call=fc)])
            r.llm.steps = [
                twice("i2c_bind", **{"bus": 1, "addr": 64, "part": "INA226", "fingerprint_reg": 254,
                                     "fingerprint_len": 2, "fingerprint_hex": "5449", "write_allow_regs": [0, 5]}),
                lambda req: twice("wait_for_approval", approval_id=last_response(req)["approval_id"]),
                say("登録しました"),
            ]
            await r.svc.say(r.sid, "0x40 を登録して")
            ids = {list(s.values())[0]["approval_id"] for s in r.llm.seen if "i2c_bind" in s}
            assert len(ids) == 1 and len(await r.store.list_approvals("pending")) == 1
            async with r.browser() as b:
                aid, csrf, _ = await r.pending_card(b)
                await b.post(f"/approvals/{aid}/approve", data={"csrf": csrf})
            await r.svc.check_approvals()
            assert r.svc.logs[r.sid].status == "idle" and r.svc.logs[r.sid].events[-1]["text"] == "登録しました"
    asyncio.run(go())


def test_calibration_is_remembered_and_decoder_aliases(manifest, signing_key, tmp_path):
    from mugemuge_agentd.hw.virtual import Environment, virtual_i2c

    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            r.dev.d.executor.i2c = r.dev.d.sampler.i2c = virtual_i2c(Environment())   # BMP280 のいる模擬バスに替える
            tk = await r.svc.rt.toolkit("pi-desk-01")
            from mugemuge_agentd.policy import Binding
            tk.bindings[(1, 0x76)] = Binding(1, 0x76, "BMP280", {"reg": 0xD0, "len": 1, "hex": "58"}, [0xF4, 0xF5])
            r.dev.d.state.bindings[(1, 0x76)] = Binding(1, 0x76, "BMP280", {"reg": 0xD0, "len": 1, "hex": "58"},
                                                         [0xF4, 0xF5], last_verified_ms=10 ** 13)
            r.llm.steps = [call("i2c_write", bus=1, addr=0x76, reg=0xF4, hex="27"),
                           call("i2c_read", bus=1, addr=0x76, reg=0x88, length=24),
                           call("sample_start", period_ms=100, duration_ms=300,
                                reads=[{"name": "env", "bus": 1, "addr": 0x76, "reg": 0xF7, "length": 6}]),
                           say("測っています")]
            await r.svc.say(r.sid, "測って")
            job = r.llm.seen[2]["sample_start"]["data"]["job_id"]
            await asyncio.sleep(0.6)
            got = await tk.job_samples(job, "BMP280", "env")            # 校正値を省略、デコーダは部品名
            assert got["decoded"] == 3 and 23 < got["stats"]["temperature_c"]["mean"] < 27
    asyncio.run(go())


def test_wait_for_job_and_live_progress(manifest, signing_key, tmp_path):
    """短い計測は wait_for_job で終わりまで待てる。長い計測は待たずに残り時間を返す。途中経過は換算して返せる。"""
    import time as _time

    from mugemuge_agentd.hw.virtual import Environment, virtual_i2c
    from mugemuge_agentd.policy import Binding

    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            r.dev.d.executor.i2c = r.dev.d.sampler.i2c = virtual_i2c(Environment())
            tk = await r.svc.rt.toolkit("pi-desk-01")
            b = Binding(1, 0x76, "BMP280", {"reg": 0xD0, "len": 1, "hex": "58"}, [0xF4, 0xF5], last_verified_ms=10 ** 13)
            tk.bindings[(1, 0x76)] = b
            r.dev.d.state.bindings[(1, 0x76)] = b
            reads = [{"name": "env", "bus": 1, "addr": 0x76, "reg": 0xF7, "length": 6}]
            r.llm.steps = [call("i2c_write", bus=1, addr=0x76, reg=0xF4, hex="27"),
                           call("i2c_read", bus=1, addr=0x76, reg=0x88, length=24),
                           call("sample_start", period_ms=200, duration_ms=1500, reads=reads),
                           lambda req: call("wait_for_job", job_id=last_response(req)["data"]["job_id"]),
                           say("終わりました")]
            t0 = _time.time()
            await r.svc.say(r.sid, "1.5秒測って")
            assert r.llm.seen[3]["wait_for_job"]["status"] == "completed" and _time.time() - t0 < 10
            job = r.llm.seen[2]["sample_start"]["data"]["job_id"]
            live = await tk.live(job)
            assert live["status"] == "completed" and len(live["columns"]["env"]["points"]) >= 7
            assert 20 < live["columns"]["env"]["points"][0]["temperature_c"] < 30

            r.llm.steps = [call("sample_start", period_ms=1000, duration_ms=600_000, reads=reads),
                           lambda req: call("wait_for_job", job_id=last_response(req)["data"]["job_id"]),
                           say("10分後に終わります")]
            t0 = _time.time()
            await r.svc.say(r.sid, "10分測って")
            w = r.llm.seen[-1]["wait_for_job"]
            assert w["status"] == "running" and w["remaining_s"] > 500 and _time.time() - t0 < 5    # 待たずに返る
            job2 = r.llm.seen[-2]["sample_start"]["data"]["job_id"]
            live = await tk.live(job2)
            assert live["status"] == "running" and live["duration_ms"] == 600_000 and live["elapsed_ms"] < 10_000
            await tk.run_op("sample_stop", {"job_id": job2}, adk_session_id=r.sid, user_id="u", run_id="r", step_id="s")
    asyncio.run(go())


def test_show_on_tester_only_when_it_can(manifest, signing_key, tmp_path):
    """テスター画面にグラフを出せるのは show_on_tester が成功したときだけ。出せない理由はそのまま返す。"""
    from mugemuge_agentd.hw.virtual import Environment, virtual_i2c
    from mugemuge_agentd.policy import Binding

    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            r.dev.d.executor.i2c = r.dev.d.sampler.i2c = virtual_i2c(Environment())
            tk = await r.svc.rt.toolkit("pi-desk-01")
            b = Binding(1, 0x76, "BMP280", {"reg": 0xD0, "len": 1, "hex": "58"}, [0xF4, 0xF5], last_verified_ms=10 ** 13)
            tk.bindings[(1, 0x76)] = b
            r.dev.d.state.bindings[(1, 0x76)] = b
            reads = [{"name": "env", "bus": 1, "addr": 0x76, "reg": 0xF7, "length": 6}]
            r.llm.steps = [call("i2c_write", bus=1, addr=0x76, reg=0xF4, hex="27"),
                           call("sample_start", period_ms=200, duration_ms=1000, reads=reads),
                           lambda req: call("show_on_tester", job_id=last_response(req)["data"]["job_id"]),
                           say("校正値がないので出せませんでした")]
            await r.svc.say(r.sid, "測ってテスター画面にグラフを出して")
            res = r.llm.seen[2]["show_on_tester"]
            assert res["status"] == "error" and "校正値" in res["message"]
            assert r.svc.rt.panels.get(r.sid, {"widgets": []})["widgets"] == []      # 画面には何も出ていない

            job = r.llm.seen[1]["sample_start"]["data"]["job_id"]
            r.llm.steps = [call("i2c_read", bus=1, addr=0x76, reg=0x88, length=24),
                           call("show_on_tester", job_id="job_99"),
                           call("show_on_tester", job_id=job, columns=["nope"]),
                           call("show_on_tester", job_id=job, columns=["env"], title="室温"),
                           say("テスター画面に出しました")]
            await r.svc.say(r.sid, "校正値を読んでから出して")
            assert "job_99" in r.llm.seen[-3]["show_on_tester"]["message"]          # 始めていない計測
            assert "nope" in r.llm.seen[-2]["show_on_tester"]["message"]            # ない列
            ok = r.llm.seen[-1]["show_on_tester"]
            assert ok["status"] == "ok" and ok["shown"]["title"] == "室温"
            panel = r.svc.rt.panels[r.sid]
            assert panel["version"] == 1 and panel["widgets"] == [{"type": "chart", "job_id": job, "columns": ["env"],
                                                                    "title": "室温"}]
            await asyncio.sleep(1.2)
            live = await tk.live(job)
            assert len(live["columns"]["env"]["points"]) >= 4
    asyncio.run(go())


def test_toolkit_resends_once_with_same_exec_id_when_offline():
    """device_offline のときは同じ exec.id で1回だけ送り直す（二重実行はデバイスの id 確認が防ぐ: F3）。"""
    from services.agent.device_tools import DeviceToolkit

    class Cloud:
        def __init__(self):
            self.bodies = []

        async def exec(self, body):
            self.bodies.append(dict(body))
            if len(self.bodies) == 1:
                return 503, {"error": "device_offline", "device_id": "d"}
            return 200, {"ok": True}

    async def go():
        tk = DeviceToolkit.__new__(DeviceToolkit)
        tk.cloud, tk.device_id = Cloud(), "d"
        st, r = await tk.send("i2c.scan", {"bus": 1}, run_id="r", step_id="s")
        assert st == 200 and len(tk.cloud.bodies) == 2
        assert tk.cloud.bodies[0]["id"] == tk.cloud.bodies[1]["id"]
    asyncio.run(go())


def test_concurrent_approval_checks_resume_only_once(manifest, signing_key, tmp_path):
    """承認の確認が同時に2か所から走っても（Web からの通知と画面の問い合わせ）、送り直しと再開は1回だけ。"""
    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            r.llm.steps = [
                call("i2c_bind", bus=1, addr=64, part="INA226", fingerprint_reg=254, fingerprint_len=2,
                     fingerprint_hex="5449", write_allow_regs=[0, 5]),
                lambda req: call("wait_for_approval", approval_id=last_response(req)["approval_id"]),
                say("承認されました"),
                say("（2回目の再開が来たらこれが出る）"),
            ]
            await r.svc.say(r.sid, "0x40 を登録して")
            async with r.browser() as b:
                aid, csrf, _ = await r.pending_card(b)
                await b.post(f"/approvals/{aid}/approve", data={"csrf": csrf})
            done = await asyncio.gather(r.svc.check_approvals(), r.svc.check_approvals())
            assert sum(done) == 1
            waits = [x for e in r.svc.logs[r.sid].events for x in e.get("results", []) if x["name"] == "wait_for_approval"]
            assert len(waits) == 1 and waits[0]["response"]["result"]["status"] == "ok"
            assert r.svc.logs[r.sid].events[-1]["text"] == "承認されました"
    asyncio.run(go())


def test_gemini_retry_and_429_message():
    """Gemini の 429 は ADK 側で待って送り直す。それでも尽きたら、待てば続けられると画面に伝える。"""
    from google.genai import errors
    from services.agent.agent import build_agent
    from services.agent.app import failure_text
    a = build_agent("gemini-3.5-flash")
    assert 429 in a.model.retry_options.http_status_codes and a.model.retry_options.attempts > 1
    e = errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted.", "status": "RESOURCE_EXHAUSTED"}})
    assert "続けて" in failure_text(e)
    assert failure_text(ValueError("boom")) == "エージェントの実行に失敗しました: boom"


def test_live_progress_decodes_ina226(manifest, signing_key, tmp_path):
    """テスター画面の途中経過で INA226 を換算する（以前は飛ばしていて、グラフが空だった）。
    バス電圧の列にシャント電圧を足し、電流はシャント抵抗が分かってから（get_samples で使った値）出す。"""
    from mugemuge_agentd.hw.virtual import Environment, virtual_i2c
    from mugemuge_agentd.policy import Binding

    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            r.dev.d.executor.i2c = r.dev.d.sampler.i2c = virtual_i2c(Environment())
            tk = await r.svc.rt.toolkit("pi-desk-01")
            b = Binding(1, 0x40, "INA226", {"reg": 0xFE, "len": 2, "hex": "5449"}, [0, 5], last_verified_ms=10 ** 13)
            tk.bindings[(1, 0x40)] = b
            r.dev.d.state.bindings[(1, 0x40)] = b
            reads = [{"name": "bus", "bus": 1, "addr": 0x40, "reg": 2, "length": 2},
                     {"name": "shunt", "bus": 1, "addr": 0x40, "reg": 1, "length": 2}]
            r.llm.steps = [call("sample_start", period_ms=200, duration_ms=1000, reads=reads),
                           lambda req: call("wait_for_job", job_id=last_response(req)["data"]["job_id"]),
                           say("終わりました")]
            await r.svc.say(r.sid, "1秒測って")
            job = r.llm.seen[0]["sample_start"]["data"]["job_id"]
            p = (await tk.live(job))["columns"]["bus"]["points"][0]
            assert 4 < p["bus_v"] < 6 and p["shunt_mv"] > 0 and "current_ma" not in p    # 抵抗値が分からないうちは電流を出さない
            await tk.job_samples(job, "ina226", "bus", shunt_column="shunt", shunt_ohm=0.1)
            p = (await tk.live(job))["columns"]["bus"]["points"][0]
            assert 50 < p["current_ma"] < 400
    asyncio.run(go())


def test_approval_invalid_is_not_retried_except_wrong_session(manifest, signing_key, tmp_path):
    """承認つきの送り直しをデバイスが受け付けなかったら（10/14 の pi-kusupizaro2 では expired で7回）、
    同じ操作の承認を求め直さない。承認カードを積み上げず、理由を人に伝えて止まる。wrong_session だけ1回求め直してよい。"""
    BIND_CALL = dict(bus=1, addr=64, part="INA226", fingerprint_reg=254, fingerprint_len=2,
                     fingerprint_hex="5449", write_allow_regs=[0, 5])

    async def go():
        async with AgentRig(manifest, signing_key, tmp_path) as r:
            r.llm.steps = [
                call("i2c_bind", **BIND_CALL),
                lambda req: call("wait_for_approval", approval_id=last_response(req)["approval_id"]),
                call("i2c_bind", **BIND_CALL),            # モデルが同じ操作をもう一度求めようとする
                lambda req: say(f"止めます: {last_response(req)['message']}"),
            ]
            await r.svc.say(r.sid, "0x40 を登録して")
            async with r.browser() as b:
                aid, csrf, _ = await r.pending_card(b)
                await b.post(f"/approvals/{aid}/approve", data={"csrf": csrf})
            r.dev.d.clock.offset_ms = 400_000             # デバイスの補正済み時刻が承認の期限を過ぎている
            assert await r.svc.check_approvals() == 1
            resumed = r.llm.seen[1]["wait_for_approval"]["result"]
            assert resumed["code"] == "approval_invalid" and resumed["reason"] == "expired"
            assert "理由: expired" in resumed["message"] and "求めない" in resumed["next"]
            again = r.llm.seen[2]["i2c_bind"]
            assert again["status"] == "error" and again["code"] == "approval_invalid" and "approval_id" not in again
            assert len(r.store.approvals) == 1               # 承認カードは1枚のまま
            assert (1, 64) not in r.dev.d.state.bindings
            assert "デバイスの時計か設定の問題" in r.svc.logs[r.sid].events[-1]["text"]

            # wrong_session は1回だけ求め直してよい（2回目は止める）
            tk = await r.svc.rt.toolkit("pi-desk-01")
            args = {"bus": 1, "addr": 65, "part": "INA226", "fingerprint": {"reg": 254, "len": 2, "hex": "5449"},
                    "write_allow_regs": [0, 5]}
            rej = {"ok": False, "error": {"code": "approval_invalid", "detail": {"reason": "wrong_session"}}}
            first = tk.approval_rejected("s1", "i2c.bind", args, rej)
            assert "1回だけ求めてよい" in first["message"] and "next" not in first
            assert tk._check_rejected("s1", "i2c.bind", args) is None
            second = tk.approval_rejected("s1", "i2c.bind", args, rej)
            assert "求めない" in second["next"]
            assert tk._check_rejected("s1", "i2c.bind", args)["reason"] == "wrong_session"
            assert tk._check_rejected("s2", "i2c.bind", args) is None      # 別の会話には持ち越さない
    asyncio.run(go())
