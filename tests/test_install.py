"""デバイスのインストーラ（mugemuge-agentd init、Web/API の /install.sh と /install/agentd.tar.gz）。"""
import asyncio
import hashlib
import io
import re
import subprocess
import sys
import tarfile

import pytest
import yaml

from mugemuge_agentd.init_manifest import render, write_new
from mugemuge_agentd.manifest import ManifestError, parse_manifest
from services.web.install import Installer, build_bundle

from test_web import Cloud, client

KEY = "z2wDYLxJeyBVs9gKahiOeEis3kEBAPTIqmPHI6mV4m8"
GW = "wss://mugemuge-gateway-123.asia-northeast1.run.app/v1/device"


def test_render_profiles():
    i2c = parse_manifest(yaml.safe_load(render(device_id="pi-lab-02", gateway_url=GW, kid="api-2026-10", key=KEY)))
    assert i2c["device"]["id"] == "pi-lab-02" and i2c["link"]["gateway_url"] == GW
    assert i2c["approval"]["public_keys"] == [{"kid": "api-2026-10", "key": KEY}]
    assert i2c["gpio"]["lines"] == [] and not i2c.get("camera", {}).get("devices")   # 既定は GPIO もカメラも許さない
    grove = parse_manifest(yaml.safe_load(render(device_id="pi-lab-02", gateway_url=GW, kid="api-2026-10", key=KEY,
                                                 profile="grove", board="raspberry-pi-5")))
    assert [ln["line"] for ln in grove["gpio"]["lines"]] == [17, 27]
    assert grove["device"]["board"] == "raspberry-pi-5"


def test_render_camera():
    base = {"device_id": "pi-lab-02", "gateway_url": GW, "kid": "api-2026-10", "key": KEY}
    cams = lambda **kw: parse_manifest(yaml.safe_load(render(**base, **kw)))["camera"]["devices"]  # noqa: E731
    assert cams() == [] and cams(camera="none") == []
    [csi] = cams(camera="csi")
    assert csi["kind"] == "csi" and "device" not in csi and csi["capture"] == "approval"
    [usb] = cams(camera="usb", camera_device="/dev/video8")
    assert usb["kind"] == "usb" and usb["device"] == "/dev/video8"
    assert cams(profile="grove")[0]["kind"] == "usb"            # grove の既定は USB（前と同じ）
    assert cams(profile="grove", camera="csi")[0]["kind"] == "csi"
    assert cams(profile="grove", camera="none") == []


def test_render_quotes_name():
    m = yaml.safe_load(render(device_id="pi-a1", gateway_url=GW, kid="k1", key=KEY, name='卓上: 2号 # "x"'))
    assert m["device"]["name"] == '卓上: 2号 # "x"'


@pytest.mark.parametrize("kw", [{"device_id": "virt-abcd"}, {"device_id": "Pi_Lab"}, {"profile": "hat"},
                                {"board": "virtual-ish"}, {"gateway_url": "ws://example.com/v1/device"},
                                {"camera": "ip"}, {"camera": "usb", "camera_device": "/dev/sda"}])
def test_render_rejects(kw):
    args = {"device_id": "pi-lab-02", "gateway_url": GW, "kid": "api-2026-10", "key": KEY, **kw}
    with pytest.raises((ValueError, ManifestError)):
        render(**args)


def test_write_new_never_overwrites(tmp_path):
    p = tmp_path / "manifest.yaml"
    write_new(p, "a: 1\n")
    with pytest.raises(FileExistsError):
        write_new(p, "b: 2\n")
    assert p.read_text() == "a: 1\n" and oct(p.stat().st_mode & 0o777) == "0o644"


def test_cli_init_keeps_existing(tmp_path):
    p = tmp_path / "m.yaml"
    cmd = [sys.executable, "-m", "mugemuge_agentd", "init", "--manifest", str(p), "--device-id", "pi-lab-02",
           "--gateway-url", GW, "--kid", "api-2026-10", "--key", KEY]
    assert subprocess.run(cmd).returncode == 0
    first = p.read_text()
    assert subprocess.run(cmd + ["--profile", "grove"]).returncode == 0
    assert p.read_text() == first


def test_bundle_is_deterministic_and_minimal():
    b = build_bundle()
    assert b == build_bundle()
    names = tarfile.open(fileobj=io.BytesIO(b)).getnames()
    assert "pyproject.toml" in names and "deploy/mugemuge-agentd.service" in names
    assert "src/mugemuge_agentd/__main__.py" in names and "schemas/manifest.schema.json" in names
    assert not [n for n in names if "__pycache__" in n or n.startswith(("services/", "tests/", "tools/"))]


def test_bundle_runs_init(tmp_path):
    """配ったソースだけで init が動く（スキーマも入っている）。"""
    opt = tmp_path / "opt"
    tarfile.open(fileobj=io.BytesIO(build_bundle())).extractall(opt, filter="data")
    r = subprocess.run([sys.executable, "-m", "mugemuge_agentd", "init", "--manifest", str(tmp_path / "m.yaml"),
                        "--device-id", "pi-lab-02", "--gateway-url", GW, "--kid", "api-2026-10", "--key", KEY],
                       env={"PYTHONPATH": str(opt / "src"), "MUGEMUGE_SCHEMA_DIR": str(opt / "schemas")},
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert yaml.safe_load((tmp_path / "m.yaml").read_text())["device"]["id"] == "pi-lab-02"


def test_script_is_filled_and_valid_bash(tmp_path):
    inst = Installer.create("https://mugemuge-gateway-123.asia-northeast1.run.app", "api-2026-10", KEY)
    s = inst.script("https://mugemuge-web-123.asia-northeast1.run.app/")
    assert not re.search(r"@@[A-Z_]+@@", s)
    assert f"GATEWAY_WS_URL='{GW}'" in s and f"APPROVAL_KEY='{KEY}'" in s
    assert f"BUNDLE_SHA256='{hashlib.sha256(inst.bundle).hexdigest()}'" in s
    assert "WEB_URL='https://mugemuge-web-123.asia-northeast1.run.app'" in s
    (tmp_path / "install.sh").write_text(s)
    assert subprocess.run(["bash", "-n", str(tmp_path / "install.sh")]).returncode == 0
    with pytest.raises(ValueError):
        inst.script("https://x/'; rm -rf /; '")


def test_script_refuses_without_root_or_code(tmp_path):
    inst = Installer.create("https://gw.example", "api-2026-10", KEY)
    (tmp_path / "install.sh").write_text(inst.script("https://web.example"))
    r = subprocess.run(["bash", str(tmp_path / "install.sh"), "--profile", "nope"], capture_output=True, text=True)
    assert r.returncode != 0 and "--profile" in r.stderr
    r = subprocess.run(["bash", str(tmp_path / "install.sh"), "--camera", "ip"], capture_output=True, text=True)
    assert r.returncode != 0 and "--camera" in r.stderr
    r = subprocess.run(["bash", str(tmp_path / "install.sh"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--profile" in r.stdout and "--camera" in r.stdout


def test_web_serves_installer(manifest, signing_key, tmp_path):
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            async with client(c.web.url) as anon:          # curl から取るのでログイン不要
                r = await anon.get("/install.sh")
                assert r.status_code == 200 and "shellscript" in r.headers["content-type"]
                assert f"WEB_URL='{c.web.url.rstrip('/')}'" in r.text
                sha = re.search(r"BUNDLE_SHA256='([0-9a-f]{64})'", r.text).group(1)
                b = await anon.get("/install/agentd.tar.gz")
                assert b.status_code == 200 and hashlib.sha256(b.content).hexdigest() == sha
            async with c.browser() as br:
                csrf = re.search(r'name="csrf" value="([^"]+)"', (await br.get("/")).text).group(1)
                r = await br.post("/devices/pair-code", data={"csrf": csrf})
                code = re.search(r">(\d{6})<", r.text).group(1)
                assert f"/install.sh | sudo bash -s -- {code}" in r.text
                assert "オプションはない" in r.text and "--camera csi" in r.text
    asyncio.run(go())
