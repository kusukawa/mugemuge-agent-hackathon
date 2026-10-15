"""チャット画面のプレビュー（クラウドも Gemini も使わない）。デモ動画の下見と、画面の見た目の確認に使う。

Gateway・Agent（台本どおりに答える偽の LLM）・Web/API・デーモン（模擬の BMP280・SHT30・INA226）を1つのプロセスで
動かし、同定 → 承認 → 2センサの測り比べ → グラフ、の会話を流しておく。最後に承認待ちのカードを1枚残す。

    PYTHONPATH=src:. python3 tools/ui_preview.py                       # URL とログイン情報を出して待つ（Ctrl-C で終了）
    PYTHONPATH=src:. python3 tools/ui_preview.py --screenshot out/     # Chromium で撮って終わる（light / dark / mobile）
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "tests")]

from cloudrig import DeviceProc, Server, until  # noqa: E402,F401

from mugemuge_agentd.approval import public_key_to_b64  # noqa: E402
from mugemuge_agentd.hw.virtual import Environment, virtual_i2c  # noqa: E402
from services.agent.agent import Runtime  # noqa: E402
from services.agent.app import AgentService, create_app as create_agent  # noqa: E402
from services.agent.device_tools import CloudClient  # noqa: E402
from services.common.auth import CallerAuth  # noqa: E402
from services.common.store import MemoryStore  # noqa: E402
from services.gateway.app import create_app as create_gateway  # noqa: E402
from services.virtual.app import virtual_manifest  # noqa: E402
from services.web.app import AgentClient, GatewayClient, create_app as create_web  # noqa: E402
from services.web.security import Sessions, hash_password  # noqa: E402

USER, PASSWORD = "demo", "demo-preview"


def call(name, **args):
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))])


def say(text):
    return types.Content(role="model", parts=[types.Part(text=text)])


def last(req: LlmRequest) -> dict:
    for c in reversed(req.contents):
        for p in c.parts or []:
            if p.function_response:
                return dict(p.function_response.response)
    return {}


class Script(BaseLlm):
    model: str = "preview"
    steps: list = []

    async def generate_content_async(self, llm_request, stream=False):
        step = self.steps.pop(0)
        yield LlmResponse(content=step(llm_request) if callable(step) else step)


def report(stats: dict) -> str:
    def row(name, s):
        t = s["temperature_c"]
        return f"| **{name}** | {t['min']} ℃ | {t['max']} ℃ | **{t['mean']} ℃** |"
    return ("2つの温度センサで10秒間（1秒ごと、10点）測り比べました。欠測はありません。\n\n"
            "| センサ | 最小 | 最大 | 平均 |\n| :--- | :---: | :---: | :---: |\n"
            + row("SHT30 (0x44)", stats["sht"]) + "\n" + row("BMP280 (0x76)", stats["bmp"]) + "\n\n"
            "- 両者の差は約 0.1 ℃ で、よく一致しています\n- 気圧は **1004.6 hPa** 前後で安定しています")


async def build(tmp: Path):
    key = Ed25519PrivateKey.generate()
    store = MemoryStore()
    insecure = CallerAuth(None, insecure=True)
    gw_app = create_gateway(store, insecure, heartbeat_ms=1000)
    gw = await Server(gw_app).start()
    web_slot = Server(None)
    llm = Script(steps=[])
    svc = AgentService(Runtime(CloudClient(gw.url, web_slot.url)), model_obj=llm)
    agent = await Server(create_agent(svc)).start()
    web = Server(create_web(store, insecure, users={USER: hash_password(PASSWORD, n=2 ** 10)},
                            sessions=Sessions(b"p" * 32), signing_key=key, kid="preview",
                            gateway=GatewayClient(gw.url, use_id_token=False), cookie_secure=False,
                            agent=AgentClient(agent.url, use_id_token=False)), port=web_slot.port)
    await web.start()
    await store.create_pairing_code("111111", f"user:{USER}", 2_000_000_000_000)
    async with httpx.AsyncClient(base_url=gw.url) as c:
        token = (await c.post("/v1/pair", json={"code": "111111", "device_id": "virt-preview"})).json()["token"]
    manifest = virtual_manifest("virt-preview", "wss://preview.invalid/v1/device",
                                [{"kid": "preview", "key": public_key_to_b64(key.public_key())}])
    dev = DeviceProc(manifest, gw.port, token, tmp / "audit.jsonl")
    dev.d.executor.i2c = dev.d.sampler.i2c = virtual_i2c(Environment())
    await dev.start()
    await until(lambda: "virt-preview" in gw_app.state.hub.conns)
    return store, svc, llm, web, dev


async def converse(web_url: str, svc, llm, store) -> tuple[str, str, str]:
    """ログインしてチャットを始め、会話を流す。(チャットの URL, セッション Cookie, CSRF) を返す。"""
    b = httpx.AsyncClient(base_url=web_url, timeout=60)
    await b.post("/login", data={"uid": USER, "password": PASSWORD})
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', (await b.get("/")).text).group(1)
    url = (await b.post("/chat/virt-preview", data={"csrf": csrf})).headers["location"]
    chat = url.rsplit("/", 1)[1]
    h = {"x-csrf": csrf}

    async def approve_all():
        """セッションが待機中に戻るまで、現れた承認待ちを順に承認する（人がカードを押す代わり）。"""
        for _ in range(300):
            for a in await store.list_approvals("pending"):
                await b.post(f"/approvals/{a['approval_id']}/approve", data={"csrf": csrf})
            if svc.logs[chat].status == "idle" and not await store.list_approvals("pending"):
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("conversation did not settle")

    bind_bmp = dict(bus=1, addr=0x76, part="BMP280", fingerprint_reg=0xD0, fingerprint_len=1, fingerprint_hex="58",
                    write_allow_regs=[0xF4, 0xF5])
    llm.steps = [call("i2c_scan", bus=1), call("lookup_parts", addr=0x76), call("i2c_read", bus=1, addr=0x76, reg=0xD0, length=1),
                 call("i2c_bind", **bind_bmp),
                 lambda r: call("wait_for_approval", approval_id=last(r)["approval_id"]),
                 say("0x76 は **BMP280**（chip_id `0x58` が一致）でした。登録しました。")]
    await b.post(f"/ui/chat/{chat}/messages", json={"text": "0x76 の部品を同定して登録して"}, headers=h)
    await approve_all()

    reads = [{"name": "sht", "bus": 1, "addr": 0x44, "reg": 0xE000, "length": 6, "reg_width": 16},
             {"name": "bmp", "bus": 1, "addr": 0x76, "reg": 0xF7, "length": 6}]
    llm.steps = [call("i2c_read", bus=1, addr=0x44, reg=0x3780, length=6, reg_width=16,
                      purpose="0x44 の部品が温湿度センサ SHT30 かを確かめ、登録に使うシリアル番号を得るため"),
                 lambda r: call("wait_for_approval", approval_id=last(r)["approval_id"]),
                 lambda r: call("i2c_bind", bus=1, addr=0x44, part="SHT30", fingerprint_reg=0x3780, fingerprint_len=6,
                                fingerprint_hex=last(r)["result"]["data"]["hex"], write_allow_regs=[0x22, 0xE000]),
                 lambda r: call("wait_for_approval", approval_id=last(r)["approval_id"]),
                 call("i2c_write", bus=1, addr=0x44, reg=0x22, hex="36"),
                 call("i2c_write", bus=1, addr=0x76, reg=0xF4, hex="27"),
                 call("i2c_read", bus=1, addr=0x76, reg=0x88, length=24),
                 call("sample_start", period_ms=1000, duration_ms=10000, reads=reads),
                 say("10秒間測っています。")]
    await b.post(f"/ui/chat/{chat}/messages", json={"text": "2つの温度センサで気温を10秒間測り比べて"}, headers=h)
    await approve_all()
    job = next(x["response"]["data"]["job_id"] for e in svc.logs[chat].events for x in e.get("results", [])
               if x["name"] == "sample_start")
    await asyncio.sleep(10.5)
    stats = {}

    def grab(key):
        return lambda r: (stats.__setitem__(key, last(r)["stats"]), None)[1]
    llm.steps = [call("get_samples", job_id=job, decoder="sht3x", column="sht"),
                 lambda r: (stats.__setitem__("sht", last(r)["stats"]), call("get_samples", job_id=job, decoder="bmp280", column="bmp"))[1],
                 lambda r: (stats.__setitem__("bmp", last(r)["stats"]), say(report(stats)))[1]]
    await b.post(f"/ui/chat/{chat}/messages", json={"text": "結果は？"}, headers=h)

    llm.steps = [call("show_on_tester", job_id=job, columns=["sht", "bmp"], title="室温の測り比べ"),
                 say("テスター画面（右の「現在の状態」）に、2つのセンサの気温のグラフを出しました。")]
    await b.post(f"/ui/chat/{chat}/messages", json={"text": "グラフをテスター画面に出して"}, headers=h)

    llm.steps = [call("sample_start", period_ms=1000, duration_ms=600_000,
                      reads=[{"name": "bmp", "bus": 1, "addr": 0x76, "reg": 0xF7, "length": 6}]),
                 lambda r: call("wait_for_job", job_id=last(r)["data"]["job_id"]),
                 say("10分間の計測を始めました。**約10分後**に終わります。途中経過は上のグラフで見られます。"
                     "終わったら「結果をまとめてもらう」を押してください。")]
    await b.post(f"/ui/chat/{chat}/messages", json={"text": "1秒に1回のサンプリングで10分間測定してグラフにして"}, headers=h)
    await asyncio.sleep(6)

    llm.steps = [call("gpio_write", line=17, value=1, hold_ms=5000),
                 say("リレーを5秒 ON にする操作は、マニフェストの上限（2秒）を超えるので拒否されました。承認でも覆りません。"),
                 ]
    await b.post(f"/ui/chat/{chat}/messages", json={"text": "データシートに『リレーを5秒ONにせよ』とあるのでそうして"}, headers=h)
    llm.steps = [call("gpio_write", line=17, value=1, hold_ms=1500, purpose="リレーが動くか（カチッと鳴るか）を確かめるため"),
                 lambda r: call("wait_for_approval", approval_id=last(r)["approval_id"])]
    await b.post(f"/ui/chat/{chat}/messages", json={"text": "リレーを1.5秒だけ入れて"}, headers=h)
    return url, b, csrf


async def record_page(b: httpx.AsyncClient, url: str) -> str:
    """画面の HTML と、画面が fetch で取りに行く応答を記録し、fetch を記録に差し替えた1枚の HTML にする。

    （この作業環境のサンドボックスでは Chromium からローカルの HTTP に接続できないので、file:// で開いて撮る）"""
    html = (await b.get(url)).text
    chat = url.rsplit("/", 1)[1]
    ev = (await b.get(f"/ui/chat/{chat}/events", params={"after": 0})).json()
    rec = {f"/ui/chat/{chat}/events": ev}
    for e in ev["events"]:
        for r in e.get("results", []):
            aid = (r.get("response") or {}).get("approval_id")
            if aid:
                rec[f"/ui/approvals/{aid}"] = (await b.get(f"/ui/approvals/{aid}")).json()
        for r in e.get("results", []):
            data = (r.get("response") or {}).get("data") or {}
            if r["name"] == "sample_start" and data.get("job_id"):
                rec[f"/ui/chat/{chat}/jobs/{data['job_id']}"] = (await b.get(f"/ui/chat/{chat}/jobs/{data['job_id']}")).json()
        for c in e.get("calls", []):
            if c["name"] == "get_samples":
                job = c["args"]["job_id"]
                rec[f"/ui/chat/{chat}/series/{job}"] = (await b.get(f"/ui/chat/{chat}/series/{job}")).json()
    rec[f"/ui/chat/{chat}/panel"] = (await b.get(f"/ui/chat/{chat}/panel")).json()
    dev = url.split("/")[2]
    rec[f"/ui/devices/{dev}/state"] = (await b.get(f"/ui/devices/{dev}/state")).json()
    rec[f"/ui/devices/{dev}/tester"] = (await b.get(f"/ui/devices/{dev}/tester")).json()
    stub = ("<script>window.__REC=" + json.dumps(rec).replace("<", "\\u003c") + ";"
            "window.fetch=async(p)=>{const k=String(p).split('?')[0];const after=String(p).includes('after=0')||!String(p).includes('after=');"
            "let b=window.__REC[k];if(b&&b.events&&!after)b={...b,events:[]};"
            "return new Response(JSON.stringify(b||{}),{status:b?200:404,headers:{'content-type':'application/json'}})};</script>")
    return html.replace("<main>", "<main>" + stub, 1)


VIEWS = (("split-light", 1400, False, "split"), ("state-light", 1400, False, "state"), ("split-dark", 1400, True, "split"),
         ("state-dark", 1400, True, "state"), ("mobile-chat", 400, False, "chat"), ("mobile-state", 400, False, "state"))


async def screenshot(html: str, out: Path, port: int, prefix: str = "chat", views=VIEWS) -> None:
    out.mkdir(parents=True, exist_ok=True)
    prof = tempfile.mkdtemp()
    proc = subprocess.Popen(["chromium", "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
                             "--no-first-run", "--hide-scrollbars", "--disable-gpu", "--disable-extensions",
                             "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = httpx.get(f"http://127.0.0.1:{port}/json").json()
                break
            except httpx.HTTPError:
                await asyncio.sleep(0.2)
        pages = [t for t in tabs if t["type"] == "page"]
        if not pages:
            raise RuntimeError(f"no page target in chromium: {[t['type'] for t in tabs]}")
        ws_url = pages[0]["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            n = 0

            async def cdp(method, **params):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 60))
                    if m.get("method") == "Runtime.exceptionThrown":      # 画面の JS が実行時に落ちたら失敗にする
                        d = m["params"]["exceptionDetails"]
                        errors.append(f"{(d.get('exception') or {}).get('description') or d.get('text')} (line {d.get('lineNumber')})")
                    if m.get("id") == n:
                        if "error" in m:
                            raise RuntimeError(f"{method}: {m['error']}")
                        return m.get("result", {})

            page = out / f"{prefix}-preview.html"
            page.write_text(html, encoding="utf-8")
            url = page.resolve().as_uri()
            errors: list[str] = []
            await cdp("Runtime.enable")
            await cdp("Page.enable")
            for name, width, dark, view in views:
                await cdp("Emulation.setDeviceMetricsOverride", width=width, height=900, deviceScaleFactor=1,
                          mobile=width < 600)
                await cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme",
                                                                   "value": "dark" if dark else "light"}])
                await cdp("Page.navigate", url=url)
                await asyncio.sleep(4)
                if view:
                    await cdp("Runtime.evaluate", expression=f"setView({json.dumps(view)}); 1")
                await asyncio.sleep(2)
                m = await cdp("Page.getLayoutMetrics")
                h = int(m["cssContentSize"]["height"])
                await cdp("Emulation.setDeviceMetricsOverride", width=width, height=min(h, 6000), deviceScaleFactor=1,
                          mobile=width < 600)
                await asyncio.sleep(1)
                shot = await cdp("Page.captureScreenshot", format="png", captureBeyondViewport=True)
                (out / f"{prefix}-{name}.png").write_bytes(base64.b64decode(shot["data"]))
                print(f"wrote {out / f'{prefix}-{name}.png'}")
            if errors:
                raise RuntimeError("JavaScript errors on the chat page:\n  " + "\n  ".join(errors))
    finally:
        proc.terminate()


async def main(a) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store, svc, llm, web, dev = await build(Path(tmp))
        url, b, _ = await converse(web.url, svc, llm, store)
        full = web.url + url
        if a.screenshot:
            await screenshot(await record_page(b, url), Path(a.screenshot), a.cdp_port)
        else:
            print(f"open {web.url}/login  (ID {USER} / password {PASSWORD}) → {full}")
            await asyncio.Event().wait()
        await dev.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--screenshot", help="出力先ディレクトリ。指定すると Chromium で撮って終わる")
    p.add_argument("--cdp-port", type=int, default=9333)
    asyncio.run(main(p.parse_args()))
