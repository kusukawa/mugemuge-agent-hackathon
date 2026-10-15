"""デバイスのインストーラを配る（/install.sh と /install/agentd.tar.gz）。

install.sh は deploy/install.sh に、この環境の Gateway の URL・承認の公開鍵・ソース一式の sha256 を埋めたもの。
配るのは公開してよいものだけ（公開鍵とデーモンのソース）。秘密鍵やトークンは含まない。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]

# デーモンを pip で入れて systemd で動かすのに要るものだけ（services/ や tests/ は入れない）
BUNDLE_FILES = ("pyproject.toml", "README.md", "deploy/mugemuge-agentd.service")
BUNDLE_DIRS = ("src/mugemuge_agentd", "schemas")


def build_bundle(root: Path = ROOT) -> bytes:
    """デーモンのソース一式の tar.gz。時刻と所有者を固定し、同じ中身なら同じバイト列にする（sha256 を埋めるため）。"""
    paths = [root / f for f in BUNDLE_FILES]
    for d in BUNDLE_DIRS:
        paths += [p for p in (root / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for p in sorted(paths):
            info = tarfile.TarInfo(p.relative_to(root).as_posix())
            data = p.read_bytes()
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def device_ws_url(gateway_url: str) -> str:
    u = urlparse(gateway_url)
    scheme = {"https": "wss", "http": "ws"}.get(u.scheme, u.scheme)
    return f"{scheme}://{u.netloc}/v1/device"


@dataclass
class Installer:
    gateway_ws_url: str
    kid: str
    public_key: str
    bundle: bytes

    @classmethod
    def create(cls, gateway_url: str, kid: str, public_key: str, root: Path = ROOT) -> "Installer":
        return cls(device_ws_url(gateway_url), kid, public_key, build_bundle(root))

    def script(self, web_url: str, root: Path = ROOT) -> str:
        text = (root / "deploy/install.sh").read_text(encoding="utf-8")
        for k, v in {"WEB_URL": web_url.rstrip("/"), "GATEWAY_WS_URL": self.gateway_ws_url,
                     "APPROVAL_KID": self.kid, "APPROVAL_KEY": self.public_key,
                     "BUNDLE_SHA256": hashlib.sha256(self.bundle).hexdigest()}.items():
            if "'" in v:                      # シェルの '…' に埋めるので、引用符を含む値は受けない
                raise ValueError(f"{k} must not contain a single quote")
            text = text.replace(f"@@{k}@@", v)
        return text
