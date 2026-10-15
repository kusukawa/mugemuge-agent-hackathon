"""記録ページの下見（クラウドもデバイスも使わない）。仮想デバイスの模擬 BMP280 と模擬カメラで24時間分の記録を作り、
Chromium で撮る（tools/ui_preview.py と同じく、通信は録画した応答に差し替えて file:// で開く）。

    PYTHONPATH=src:. python3 tools/log_preview.py out/
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

from cloudrig import Server  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from mugemuge_agentd.hw.camera import FakeCamera  # noqa: E402
from mugemuge_agentd.hw.virtual import BMP280, BMP280_CALIB, SHT30, Environment  # noqa: E402
from services.common.auth import CallerAuth  # noqa: E402
from services.common.blobs import MemoryBlobStore  # noqa: E402
from services.common.store import MemoryStore  # noqa: E402
from services.web.app import create_app  # noqa: E402
from services.web.security import Sessions, hash_password  # noqa: E402
from ui_preview import screenshot  # noqa: E402

T0 = 1_791_252_000_000          # 2026-10-06 18:00 JST ごろ


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


async def main(out: Path):
    store, blobs = MemoryStore(), MemoryBlobStore()
    clock = Clock()
    env = Environment(clock, seed=3)
    bmp, sht, cam = BMP280(env), SHT30(env), FakeCamera(clock=lambda: clock.t * 3)
    bmp.write(0xF4, b"\x27")
    sht.write(0x22, b"\x36")
    await store.update_device("pi-1", {"device_id": "pi-1", "online": True,
                                       "manifest": {"device": {"name": "卓上むげむげ"}}})
    rows_per = 30
    seq, photos_b64 = 0, {}
    for start in range(0, 24 * 60, rows_per):
        rows, photos = [], []
        for m in range(start, start + rows_per):
            clock.t = m * 1.6                # 模擬の環境は短いデモ向けの速さなので、24時間にならして使う
            rows.append({"t": m * 60_000, "v": [bmp.read(0xF7, 6).hex(), sht.read(0xE000, 6, 16).hex()]})
            if m % 30 == 0:
                data, ctype = cam.capture(320, 240)
                bid = f"blob_{m:010d}"
                await blobs.put(f"devices/pi-1/{bid}.png", data, ctype)
                await store.put_blob_meta("pi-1", bid, {"path": f"devices/pi-1/{bid}.png", "content_type": ctype})
                photos.append({"t": m * 60_000, "blob_id": bid})
                photos_b64[bid] = "data:image/png;base64," + base64.b64encode(data).decode()
        await store.put_log_batch("pi-1", "log_demo", {"seq": seq, "started_ts": T0, "rows": rows, "photos": photos})
        seq += 1
    await store.put_log("pi-1", "log_demo", {
        "name": "一晩の室温と湿度（写真つき）", "columns": ["t_ms", "env", "rh"], "started_ts": T0, "status": "running",
        "rows": 24 * 60, "photos": 48, "last_row": rows[-1], "last_photo": photos[-1] if photos else None,
        "args": {"interval_ms": 60_000, "duration_ms": 2 * 86_400_000, "reads": [],
                 "photo": {"camera": "cam0", "interval_ms": 1_800_000}},
        "decoders": {"env": {"decoder": "bmp280", "calibration_hex": BMP280_CALIB.hex(), "part": "BMP280"},
                     "rh": {"decoder": "sht3x", "calibration_hex": "", "part": "SHT30"}}})
    app = create_app(store, CallerAuth(None, insecure=True), users={"demo": hash_password("demo-pw", n=2 ** 10)},
                     sessions=Sessions(b"p" * 32), signing_key=Ed25519PrivateKey.generate(), kid="x", gateway=None,
                     cookie_secure=False, blobs=blobs)
    srv = await Server(app).start()
    b = httpx.AsyncClient(base_url=srv.url, timeout=60)
    await b.post("/login", data={"uid": "demo", "password": "demo-pw"})
    html = (await b.get("/logs/pi-1/log_demo")).text
    rec = {"/ui/devices/pi-1/logs/log_demo/data": (await b.get("/ui/devices/pi-1/logs/log_demo/data")).json()}
    stub = ("<script>window.__BLOB=" + json.dumps(photos_b64) + ";window.__REC=" + json.dumps(rec) + ";"
            "window.fetch=async(p)=>{const k=String(p).split('?')[0];const b=window.__REC[k];"
            "return new Response(JSON.stringify(b||{}),{status:b?200:404,headers:{'content-type':'application/json'}})};</script>")
    html = html.replace("<main>", "<main>" + stub, 1)
    # 一覧ページも撮る
    lst = (await b.get("/logs/pi-1")).text
    for bid, uri in photos_b64.items():
        lst = lst.replace(f"/ui/devices/pi-1/blobs/{bid}", uri)
    (out / "logs-list.html").write_text(lst)
    await screenshot(html, out, 9350, prefix="log", views=(("light", 1300, False, None), ("dark", 1300, True, None),
                                                           ("mobile", 400, False, None)))
    await screenshot(lst, out, 9351, prefix="loglist", views=(("light", 1300, False, None),))
    await srv.stop()


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    out.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(out))
