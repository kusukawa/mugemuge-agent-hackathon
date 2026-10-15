"""インストール時に最初のマニフェストを作る（`mugemuge-agentd init`）。

人が Pi の上で実行するインストーラ（Web/API の /install.sh）から呼ばれる。リモートからマニフェストを
変える経路ではない（PROTOCOL.md 0章の 7）。既にあるマニフェストは上書きしない。

プロファイル（GPIO）:
    i2c    I2C バス 1 だけ（既定）。GPIO は何も許さない。配線が分からない Pi でも安全
    grove  examples/manifest.pi-grove.yaml と同じ（GPIO17 リレー・GPIO27 ボタン。カメラの既定は USB）

カメラ（--camera）:
    none   カメラは使えない（i2c の既定）
    csi    Pi Camera Module（rpicam-still）
    usb    UVC カメラ（fswebcam。--camera-device で /dev/videoN、既定 /dev/video0。grove の既定）
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .manifest import parse_manifest

PROFILES = ("i2c", "grove")
CAMERAS = ("none", "csi", "usb")

DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")      # Gateway の DEVICE_ID_RE と同じ
BOARDS = ("raspberry-pi-zero-2-w", "raspberry-pi-4", "raspberry-pi-5")   # schemas/manifest.schema.json の device.board

_HEAD = """\
# むげむげ デバイスのマニフェスト（mugemuge-agentd init で生成。プロファイル: {profile}、カメラ: {camera}）
# 置き場所: /etc/mugemuge/manifest.yaml（root 所有・0644。デーモンは読むだけ）
# 書かれていないバス・アドレス・ピンはすべて拒否される。配線を変えたらここを直して
#   sudo systemctl restart mugemuge-agentd
# 書き方の例は examples/manifest.pi-grove.yaml。

manifest_version: 1

device:
  id: {device_id}
  name: {name}
  board: {board}
  hat: none

link:
  gateway_url: {gateway_url}
  token_file: /etc/mugemuge/device.token

approval:
  scheme: ed25519
  public_keys:
    - kid: {kid}
      key: {key}
  max_ttl_s: 300

safety:
  deadman_ms: 10000
  estop_clear: approval

limits:
  exec_per_s: 20
  i2c_read_per_s: 200
  i2c_write_per_s: 20
  gpio_write_per_s: 5
  display_per_s: 2
  max_read_len: 32
  max_write_len: 16
  queue_depth: 8

sample:
  min_period_ms: 10
  max_duration_ms: 600000      # 10分
  max_reads_per_tick: 8
  max_concurrent_jobs: 2

i2c:
  buses:
    - bus: 1
      label: grove-i2c
      scan:
        enabled: true
        first: 0x08
        last: 0x77
        exclude: []
      unknown:              # 何がつながっているか分からないアドレス
        read: allow         # 同定のための読み出しは自動
        write: approval     # 未同定の部品への書き込みは人の承認が必要
        bind: approval      # 「この部品は INA226 として扱う」も人が確認する
      bound:                # i2c.bind で同定を確定したアドレス
        other_write: approval
        max_write_allow_regs: 8
        fingerprint_recheck_ms: 1000
        # 仕様（PROTOCOL.md 6.1節）の既定は false。SHT30 と表示デバイス（SH1107）のために true で作る。
        # 提出後に部品名での許可（allow_no_recheck_parts）へ移す予定
        allow_no_recheck: true
        # 表示デバイス（SH1107）として登録したアドレスへの display.draw。allow にすると登録の承認のあとは自動で描ける
        display: approval
      addresses: []
"""

_GPIO_NONE = """
gpio:
  chip: gpiochip0
  lines: []                 # GPIO は何も許さない。リレーなどをつないだら examples/manifest.pi-grove.yaml を見て足す
"""

_GPIO_GROVE = """
gpio:
  chip: gpiochip0
  lines:
    - line: 17
      label: relay-1        # Grove リレー
      direction: output
      write: approval       # 能動側（ON）は承認必須。OFF（safe_value）は常に許可
      safe_value: 0
      max_on_ms: 2000       # 承認されても 2 秒で自動 OFF
    - line: 27
      label: button-1       # Grove ボタン
      direction: input
      read: allow
      bias: pull_up
"""

_CAMERA = """
camera:                     # 書かれていないカメラは使えない
  devices:
    - id: cam0
      kind: {kind}{device}
      label: 卓上カメラ
      capture: approval     # 人や部屋が写りうるので、撮る前に人が確認する
      max_width: 1280
      max_height: 960
"""

_CAMERA_KIND_NOTE = {"csi": "             # Pi Camera Module（rpicam-still で撮る）",
                     "usb": "             # UVC カメラ（fswebcam で撮る）"}

_TAIL = """
log:                        # 長期の記録ジョブ（log.start）
  min_interval_ms: 1000
  max_duration_ms: 604800000      # 7日
  min_photo_interval_ms: 60000
  max_concurrent_logs: 2
  spool_mb: 50
  photo_max_px: 640

logging:
  audit_path: /var/lib/mugemuge/audit.jsonl
  audit_max_mb: 20
"""


def render(*, device_id: str, gateway_url: str, kid: str, key: str, profile: str = "i2c",
           name: str | None = None, board: str = "raspberry-pi-zero-2-w",
           camera: str | None = None, camera_device: str = "/dev/video0") -> str:
    """マニフェストの YAML を作り、スキーマで検証してから返す（通らなければ ManifestError）。

    camera を省くとプロファイルの既定（i2c は none、grove は usb）。
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r} (choose from {', '.join(PROFILES)})")
    camera = camera or ("usb" if profile == "grove" else "none")
    if camera not in CAMERAS:
        raise ValueError(f"unknown camera {camera!r} (choose from {', '.join(CAMERAS)})")
    if not DEVICE_ID_RE.match(device_id) or device_id.startswith("virt-"):
        raise ValueError(f"device id {device_id!r} must match {DEVICE_ID_RE.pattern} and not start with virt-")
    # YAML の値として安全に埋め込む（名前に : や # が入っても壊れない）
    q = lambda s: yaml.safe_dump(s, allow_unicode=True, default_style='"').strip()  # noqa: E731
    text = _HEAD.format(profile=profile, camera=camera, device_id=device_id, name=q(name or device_id), board=q(board),
                        gateway_url=q(gateway_url), kid=q(kid), key=q(key))
    text += _GPIO_GROVE if profile == "grove" else _GPIO_NONE
    if camera != "none":
        device = f"\n      device: {q(camera_device)}" if camera == "usb" else ""
        text += _CAMERA.format(kind=camera + _CAMERA_KIND_NOTE[camera], device=device)
    text += _TAIL
    parse_manifest(yaml.safe_load(text))
    return text


def write_new(path: str | Path, text: str) -> None:
    """新しく作るだけ。既にあれば FileExistsError（人が直したマニフェストを消さない）。"""
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o644)
