"""コマンドライン。

    python -m mugemuge_agentd run --manifest /etc/mugemuge/manifest.yaml
    python -m mugemuge_agentd run --manifest examples/manifest.pi-grove.yaml --fake-hw \\
        --gateway-url ws://127.0.0.1:8765/v1/device --audit-path /tmp/audit.jsonl   # 開発用
    python -m mugemuge_agentd pair 123456 --manifest /etc/mugemuge/manifest.yaml
    python -m mugemuge_agentd safe-outputs --manifest /etc/mugemuge/manifest.yaml  # systemd の ExecStopPost 用
    python -m mugemuge_agentd init --device-id pi-desk-02 --gateway-url wss://… --kid … --key …  # インストーラ用
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from . import __version__
from .manifest import ManifestError, load_manifest

log = logging.getLogger("mugemuge_agentd")

DEFAULT_MANIFEST = "/etc/mugemuge/manifest.yaml"


def _make_hw(manifest: dict, fake: bool):
    if fake:
        from .hw.gpio import FakeGPIO
        from .hw.i2c import FakeI2C

        return FakeI2C(), FakeGPIO()
    from .hw.gpio import GpiodGPIO
    from .hw.i2c import SmbusI2C

    return SmbusI2C(), GpiodGPIO(manifest["gpio"]["chip"])


def _read_token(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def cmd_run(args) -> int:
    from .client import check_gateway_url
    from .daemon import Daemon

    manifest = load_manifest(args.manifest)
    url = args.gateway_url or manifest["link"]["gateway_url"]
    check_gateway_url(url)
    token_file = args.token_file or manifest["link"]["token_file"]
    token = _read_token(token_file)
    if token is None and urlparse(url).scheme == "wss":
        log.error("token file %s is missing; run `mugemuge-agentd pair <code>` first", token_file)
        return 2
    audit_path = args.audit_path or manifest["logging"]["audit_path"]
    i2c, gpio = _make_hw(manifest, args.fake_hw)
    from .hw.camera import make_cameras

    daemon = Daemon(manifest, i2c, gpio, gateway_url=url, token=token, audit_path=audit_path,
                    cameras=make_cameras(manifest, fake=args.fake_hw))
    log.info("mugemuge-agentd %s starting: device %s, gateway %s, %s hardware, audit %s",
             __version__, manifest["device"]["id"], url, "FAKE" if args.fake_hw else "real", audit_path)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, daemon.request_stop)
        await daemon.run()

    try:
        asyncio.run(main())
    except Exception:
        log.exception("fatal error; reverting outputs")
        daemon.safety.revert_all_sync()
        return 1
    return 0


def pair_url(gateway_url: str) -> str:
    u = urlparse(gateway_url)
    scheme = {"wss": "https", "ws": "http"}[u.scheme]
    return urlunparse((scheme, u.netloc, "/v1/pair", "", "", ""))


def cmd_pair(args) -> int:
    from .client import check_gateway_url

    manifest = load_manifest(args.manifest)
    url = args.gateway_url or manifest["link"]["gateway_url"]
    check_gateway_url(url)
    if not (args.code.isdigit() and len(args.code) == 6):
        log.error("pairing code must be 6 digits")
        return 2
    body = json.dumps({"code": args.code, "device_id": manifest["device"]["id"]}).encode()
    req = urllib.request.Request(pair_url(url), data=body, headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            token = json.loads(resp.read())["token"]
    except (urllib.error.URLError, KeyError, ValueError) as e:
        log.error("pairing failed: %s", e)
        return 1
    if not (isinstance(token, str) and token.startswith("mgd_")):
        log.error("gateway returned an unexpected token format")
        return 1
    path = Path(args.token_file or manifest["link"]["token_file"])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)
    log.info("paired as %s; token written to %s", manifest["device"]["id"], path)
    return 0


def cmd_safe_outputs(args) -> int:
    """全出力ピンを safe_value にする（デーモンが落ちた後に systemd が呼ぶ）。"""
    from .hw.gpio import line_configs

    manifest = load_manifest(args.manifest)
    _, gpio = _make_hw(manifest, args.fake_hw)
    gpio.configure(line_configs(manifest))
    gpio.close()
    outs = [ln["line"] for ln in manifest["gpio"]["lines"] if ln["direction"] == "output"]
    log.info("outputs set to safe values: %s", outs)
    return 0


def cmd_init(args) -> int:
    """最初のマニフェストを作る（インストーラから）。既にあれば何もしない。"""
    from .init_manifest import render, write_new

    text = render(device_id=args.device_id, gateway_url=args.gateway_url, kid=args.kid, key=args.key,
                  profile=args.profile, name=args.name, board=args.board, camera=args.camera,
                  camera_device=args.camera_device)
    try:
        write_new(args.manifest, text)
    except FileExistsError:
        log.warning("%s already exists; leaving it as is", args.manifest)
        return 0
    log.info("wrote %s (device %s, profile %s, camera %s)", args.manifest, args.device_id, args.profile,
             args.camera or "default")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mugemuge-agentd", description="むげむげ Pi 側デーモン")
    p.add_argument("--log-level", default=os.environ.get("MUGEMUGE_LOG_LEVEL", "INFO"))
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--manifest", default=DEFAULT_MANIFEST)
        sp.add_argument("--fake-hw", action="store_true", help="I2C／GPIO を模擬にする（仮想デバイス・開発用）")
        sp.add_argument("--gateway-url", help="接続先の上書き。ws:// は loopback のみ（開発用）")
        sp.add_argument("--token-file", help="トークンファイルの上書き")

    r = sub.add_parser("run", help="デーモンを動かす")
    common(r)
    r.add_argument("--audit-path", help="監査ログの置き場所の上書き（開発用）")
    r.set_defaults(func=cmd_run)

    pr = sub.add_parser("pair", help="ペアリングコードでトークンを受け取る")
    pr.add_argument("code")
    common(pr)
    pr.set_defaults(func=cmd_pair)

    so = sub.add_parser("safe-outputs", help="全出力を safe_value にする")
    common(so)
    so.set_defaults(func=cmd_safe_outputs)

    from .init_manifest import BOARDS, CAMERAS, PROFILES

    it = sub.add_parser("init", help="最初のマニフェストを作る（既にあれば上書きしない）")
    it.add_argument("--manifest", default=DEFAULT_MANIFEST)
    it.add_argument("--device-id", required=True)
    it.add_argument("--name")
    it.add_argument("--board", choices=BOARDS, default=BOARDS[0])
    it.add_argument("--gateway-url", required=True, help="wss://…/v1/device")
    it.add_argument("--kid", required=True, help="承認の公開鍵の kid")
    it.add_argument("--key", required=True, help="承認の公開鍵（base64url）")
    it.add_argument("--profile", choices=PROFILES, default="i2c",
                    help="i2c: I2C だけ（既定）／ grove: GPIO17 リレー・GPIO27 ボタンも")
    it.add_argument("--camera", choices=CAMERAS,
                    help="none / csi（Pi Camera Module）/ usb。省くと i2c は none、grove は usb")
    it.add_argument("--camera-device", default="/dev/video0", help="USB カメラのデバイス（--camera usb のとき）")
    it.set_defaults(func=cmd_init)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except ManifestError as e:
        log.error("%s", e)
        return 2
    except ValueError as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
