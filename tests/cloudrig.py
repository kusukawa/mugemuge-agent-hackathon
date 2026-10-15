"""クラウド側の結合テスト用の部品: uvicorn を同じイベントループで動かし、実物のデーモン（模擬ハード）をつなぐ。"""
from __future__ import annotations

import asyncio
import socket

import httpx
import uvicorn

from mugemuge_agentd.daemon import Daemon
from mugemuge_agentd.hw.gpio import FakeGPIO
from mugemuge_agentd.hw.i2c import FakeI2C


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, app, port: int | None = None):
        self.port = port or free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    ws_max_size=65536, lifespan="off"))
        self.task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self):
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        return self

    async def stop(self):
        self.server.should_exit = True
        if self.task:
            await self.task


class DeviceProc:
    """デーモン（FakeI2C／FakeGPIO）を同じイベントループで動かす。"""

    def __init__(self, manifest, gateway_port: int, token: str, audit_path, cameras=None):
        self.i2c, self.gpio = FakeI2C(), FakeGPIO()
        self.d = Daemon(manifest, self.i2c, self.gpio, gateway_url=f"ws://127.0.0.1:{gateway_port}/v1/device",
                        token=token, audit_path=audit_path, backoff=(0.1, 0.2, 0.4), cameras=cameras)
        self.task: asyncio.Task | None = None

    async def start(self):
        self.task = asyncio.create_task(self.d.run())
        return self

    async def stop(self):
        self.d.request_stop()
        await asyncio.wait_for(self.task, 10)


async def until(cond, timeout=5.0, step=0.02):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not (await cond() if asyncio.iscoroutinefunction(cond) else cond()):
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


def client(base: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base, timeout=15)
