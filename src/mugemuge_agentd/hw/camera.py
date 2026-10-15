"""カメラ（camera.capture と記録ジョブの写真）。

- CSI: rpicam-still（Raspberry Pi OS 標準）
- USB: fswebcam（UVC カメラ）
- 模擬: 標準ライブラリだけで PNG を作る（テストと仮想デバイス用）

どれも外部コマンドを呼ぶだけで、重いライブラリ（OpenCV など）は入れない（Pi Zero 2 W でも動かすため）。
撮影はブロッキングなので、スレッドから呼ぶ。失敗は CameraError。
"""
from __future__ import annotations

import math
import struct
import subprocess
import threading
import time
import zlib
from abc import ABC, abstractmethod


class CameraError(OSError):
    pass


class Camera(ABC):
    @abstractmethod
    def capture(self, width: int, height: int) -> tuple[bytes, str]:
        """(画像のバイト列, content_type) を返す。"""


def _run(cmd: list[str], timeout: float = 20.0) -> bytes:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise CameraError(2, f"{cmd[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise CameraError(62, f"{cmd[0]} timed out") from None
    if r.returncode != 0 or not r.stdout:
        msg = (r.stderr or b"").decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise CameraError(5, f"{cmd[0]} failed: {msg[0][:200]}")
    return r.stdout


class RpicamCamera(Camera):
    """CSI カメラ（Pi Camera Module）。rpicam-still の標準出力から JPEG を受け取る。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def capture(self, width, height):
        with self._lock:
            data = _run(["rpicam-still", "-n", "-t", "1", "--width", str(width), "--height", str(height),
                         "-e", "jpg", "-q", "85", "-o", "-"])
        return data, "image/jpeg"


class UsbCamera(Camera):
    """USB（UVC）カメラ。fswebcam の標準出力から JPEG を受け取る。最初の数フレームは露出が合わないので捨てる。"""

    def __init__(self, device: str):
        self.device = device
        self._lock = threading.Lock()

    def capture(self, width, height):
        with self._lock:
            data = _run(["fswebcam", "-q", "-d", self.device, "-r", f"{width}x{height}", "-S", "5",
                         "--no-banner", "--jpeg", "85", "-"])
        return data, "image/jpeg"


def _png(width: int, height: int, rgb_at) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(rgb_at(x, y))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


class FakeCamera(Camera):
    """模擬カメラ。卓上を思わせる絵（台の上の基板とセンサ）を、時刻で明るさと色味を少し変えて描く。"""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.count = 0

    def capture(self, width, height):
        self.count += 1
        w, h = min(width, 320), min(height, 240)
        t = self.clock()
        light = 0.75 + 0.25 * math.sin(t / 600.0)          # 部屋の明るさがゆっくり変わる
        board = (int(w * 0.25), int(h * 0.35), int(w * 0.75), int(h * 0.75))
        sensor = (int(w * 0.55), int(h * 0.42), int(w * 0.68), int(h * 0.55))

        def rgb(x, y):
            if sensor[0] <= x < sensor[2] and sensor[1] <= y < sensor[3]:
                c = (40, 90, 200)
            elif board[0] <= x < board[2] and board[1] <= y < board[3]:
                c = (30, 110, 70)
            else:
                g = 170 + int(40 * y / h)
                c = (g, g - 8, g - 20)
            return bytes(max(0, min(255, int(v * light))) for v in c)

        return _png(w, h, rgb), "image/png"


def make_cameras(manifest: dict, fake: bool = False) -> dict[str, Camera]:
    out: dict[str, Camera] = {}
    for c in manifest.get("camera", {}).get("devices", []):
        if fake or c["kind"] == "fake":
            out[c["id"]] = FakeCamera()
        elif c["kind"] == "csi":
            out[c["id"]] = RpicamCamera()
        else:
            out[c["id"]] = UsbCamera(c["device"])
    return out
