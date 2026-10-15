"""仮想デバイスのサービス（Cloud Run、--no-allow-unauthenticated、起動元は Web/API の SA）。

審査員が「仮想デバイスで試す」を押すと、Web/API がペアリングコードを作ってここに渡す。ここでは本物の Pi と同じく
/v1/pair でトークンを受け取り、M1 のデーモン（Daemon）を模擬ハード（hw/virtual.py）で起動して、公開の Gateway に
wss でつなぐ。Gateway・Agent・Web/API からは本物の Pi と区別がつかない（同じ判定・同じ承認・同じ監査ログ）。

- 1人1台（審査員どうしで干渉しない）。TTL（既定60分）で自動的に止める。同時に動かすのは MAX_DEVICES 台まで
- デーモンは外向きの WebSocket を保つので、Cloud Run は CPU を常時割り当てる（--no-cpu-throttling）。
  リクエストが来なくなると Cloud Run はインスタンスを止めるので、そのときは全デバイスを正しく止める（bye）

    uvicorn --factory services.virtual.app:app_from_env
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request

from mugemuge_agentd.daemon import Daemon
from mugemuge_agentd.hw.camera import make_cameras
from mugemuge_agentd.hw.gpio import FakeGPIO
from mugemuge_agentd.hw.virtual import Environment, virtual_i2c
from mugemuge_agentd.manifest import parse_manifest

log = logging.getLogger("mugemuge.virtual")

DEVICE_ID_RE = re.compile(r"^virt-[a-z0-9]{4,16}$")


def virtual_manifest(device_id: str, gateway_ws_url: str, public_keys: list[dict]) -> dict:
    """examples/manifest.pi-grove.yaml と同じ規則の仮想デバイス用マニフェスト（リレーとボタンも模擬で持つ）。"""
    return parse_manifest({
        "manifest_version": 1,
        "device": {"id": device_id, "name": "仮想むげむげ", "board": "virtual", "hat": "none"},
        "link": {"gateway_url": gateway_ws_url, "token_file": "/tmp/unused.token"},
        "approval": {"scheme": "ed25519", "public_keys": public_keys, "max_ttl_s": 300},
        "safety": {"deadman_ms": 10000, "estop_clear": "approval"},
        "limits": {"exec_per_s": 20, "i2c_read_per_s": 200, "i2c_write_per_s": 20, "gpio_write_per_s": 5,
                   "max_read_len": 32, "max_write_len": 16, "queue_depth": 8},
        "sample": {"min_period_ms": 10, "max_duration_ms": 600000, "max_reads_per_tick": 8, "max_concurrent_jobs": 2},
        "i2c": {"buses": [{
            "bus": 1, "label": "grove-i2c（仮想）",
            "scan": {"enabled": True, "first": 0x08, "last": 0x77, "exclude": []},
            "unknown": {"read": "allow", "write": "approval", "bind": "approval"},
            "bound": {"other_write": "approval", "max_write_allow_regs": 8, "fingerprint_recheck_ms": 1000,
                      "allow_no_recheck": True},     # 模擬の SHT30 のため（examples/manifest.pi-grove.yaml と同じ）
            "addresses": []}]},
        "gpio": {"chip": "gpiochip0", "lines": [
            {"line": 17, "label": "relay-1（仮想）", "direction": "output", "write": "approval", "safe_value": 0,
             "max_on_ms": 2000},
            {"line": 27, "label": "button-1（仮想）", "direction": "input", "read": "allow", "bias": "pull_up"}]},
        "camera": {"devices": [{"id": "cam0", "kind": "fake", "label": "卓上カメラ（仮想）", "capture": "approval",
                                "max_width": 640, "max_height": 480}]},
        "log": {"min_interval_ms": 1000, "max_duration_ms": 3_600_000, "min_photo_interval_ms": 10_000,
                "max_concurrent_logs": 2, "spool_mb": 20},
        "logging": {"audit_path": f"/tmp/{device_id}.audit.jsonl", "audit_max_mb": 5},
    })


def ws_url(gateway_url: str) -> str:
    u = urlparse(gateway_url)
    scheme = {"https": "wss", "http": "ws"}.get(u.scheme, u.scheme)
    return f"{scheme}://{u.netloc}/v1/device"


@dataclass
class Virtual:
    device_id: str
    owner: str
    daemon: Daemon
    task: asyncio.Task
    started: float
    expires: float


class VirtualFleet:
    def __init__(self, gateway_url: str, public_keys: list[dict], *, ttl_s: int = 3600, max_devices: int = 10):
        self.gateway_url = gateway_url.rstrip("/")
        self.public_keys = public_keys
        self.ttl_s, self.max_devices = ttl_s, max_devices
        self.devices: dict[str, Virtual] = {}
        self.http = httpx.AsyncClient(timeout=20)

    async def create(self, device_id: str, code: str, owner: str) -> Virtual:
        self.reap()
        if not DEVICE_ID_RE.match(device_id):
            raise ValueError("device_id must look like virt-xxxx")
        if device_id in self.devices:
            raise ValueError("device already exists")
        if len(self.devices) >= self.max_devices:
            raise OverflowError(f"too many virtual devices (max {self.max_devices}); try again later")
        # 本物の Pi と同じペアリング（1回限りのコード → トークン）
        r = await self.http.post(f"{self.gateway_url}/v1/pair", json={"code": code, "device_id": device_id})
        if r.status_code != 200:
            raise PermissionError(f"pairing failed: {r.status_code} {r.text[:200]}")
        token = r.json()["token"]
        # マニフェストには常に wss:// を書く（スキーマの要求）。実際の接続先は Daemon に別に渡す（ローカルの試験では ws://）
        manifest = virtual_manifest(device_id, "wss://" + urlparse(self.gateway_url).netloc + "/v1/device",
                                    self.public_keys)
        d = Daemon(manifest, virtual_i2c(Environment()), FakeGPIO(), gateway_url=ws_url(self.gateway_url),
                   token=token, audit_path=manifest["logging"]["audit_path"], cameras=make_cameras(manifest, fake=True),
                   spool_dir=f"/tmp/{device_id}-spool")
        task = asyncio.create_task(d.run(), name=device_id)
        now = time.time()
        v = Virtual(device_id, owner, d, task, now, now + self.ttl_s)
        self.devices[device_id] = v
        log.info("virtual device %s started for %s (expires in %ds)", device_id, owner, self.ttl_s)
        return v

    async def stop(self, device_id: str) -> bool:
        v = self.devices.pop(device_id, None)
        if v is None:
            return False
        v.daemon.request_stop()
        try:
            await asyncio.wait_for(v.task, 15)
        except (asyncio.TimeoutError, Exception):
            v.task.cancel()
        log.info("virtual device %s stopped", device_id)
        return True

    def reap(self) -> list[str]:
        """期限切れのデバイスを止める（待たない）。"""
        now = time.time()
        expired = [d for d, v in self.devices.items() if v.expires < now or v.task.done()]
        for d in expired:
            asyncio.create_task(self.stop(d))
        return expired

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(d) for d in list(self.devices)), return_exceptions=True)

    def describe(self, v: Virtual) -> dict:
        return {"device_id": v.device_id, "owner": v.owner, "connected": v.daemon.client.ready,
                "started": int(v.started), "expires": int(v.expires)}


def create_app(fleet: VirtualFleet) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async def reaper():
            while True:
                await asyncio.sleep(30)
                fleet.reap()
        task = asyncio.create_task(reaper())
        yield
        task.cancel()
        await fleet.stop_all()          # Cloud Run がインスタンスを止めるとき（SIGTERM）も、出力を戻して bye を送る

    app = FastAPI(title="mugemuge-virtual", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.fleet = fleet

    @app.post("/devices")
    async def create(request: Request):
        body = await request.json()
        try:
            v = await fleet.create(str(body["device_id"]), str(body["code"]), str(body.get("owner", ""))[:80])
        except KeyError:
            raise HTTPException(400, "device_id and code are required") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except OverflowError as e:
            raise HTTPException(429, str(e)) from None
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
        return fleet.describe(v)

    @app.get("/devices")
    async def list_devices():
        fleet.reap()
        return {"devices": [fleet.describe(v) for v in fleet.devices.values()], "max": fleet.max_devices}

    @app.delete("/devices/{device_id}")
    async def delete(device_id: str):
        return {"stopped": await fleet.stop(device_id)}

    @app.get("/health")
    async def health():
        return {"ok": True, "devices": len(fleet.devices)}

    return app


def app_from_env() -> FastAPI:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    keys = [{"kid": os.environ["APPROVAL_KID"], "key": os.environ["APPROVAL_PUBLIC_KEY"]}]
    return create_app(VirtualFleet(os.environ["GATEWAY_URL"], keys,
                                   ttl_s=int(os.environ.get("VIRTUAL_TTL_S", "3600")),
                                   max_devices=int(os.environ.get("VIRTUAL_MAX_DEVICES", "10"))))
