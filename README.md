# mugemuge-agentd

むげむげのPi側デーモン。プロトコル（v1）の仕様、スキーマ、ポリシー判定と承認検証、そしてそれらを使って実際に I2C／GPIO を動かすデーモン本体。

- 仕様: [`docs/PROTOCOL.md`](docs/PROTOCOL.md)
- マニフェストのスキーマ: [`schemas/manifest.schema.json`](schemas/manifest.schema.json)
- メッセージのスキーマ: [`schemas/protocol.schema.json`](schemas/protocol.schema.json)
- マニフェストの例: [`examples/manifest.pi-grove.yaml`](examples/manifest.pi-grove.yaml)（ハッカソン用、素のPi＋Grove）、[`examples/manifest.mugemuge-hat.yaml`](examples/manifest.mugemuge-hat.yaml)（将来のHAT）
- 1セッション分のメッセージ例: [`examples/session.json`](examples/session.json)（スキャン→同定→承認付きバインド→計測→注入の遮断→デッドマン）

## 構成

| ファイル | 中身 | どこで使うか |
|---|---|---|
| `src/mugemuge_agentd/canonical.py` | 正規化JSONと `args_sha256` | デバイス・Web/API |
| `src/mugemuge_agentd/manifest.py` | マニフェストの読み込み・検証・既定値の補完・ハッシュ | デバイス・Gateway（hello の検証） |
| `src/mugemuge_agentd/policy.py` | op とマニフェストからレベル（allow / approval / deny）を決める | デバイス・Agent（`before_tool_callback` での事前チェック） |
| `src/mugemuge_agentd/gate.py` | 非常停止・レート上限・承認の検証まで含めた最終判定 | デバイス |
| `src/mugemuge_agentd/approval.py` | 承認グラントの Ed25519 署名と検証 | 署名は Web/API のみ、検証はデバイス |
| `src/mugemuge_agentd/messages.py` | 受信フレームの読み込み（小数の拒否＋スキーマ検証）、result / event の組み立て | デバイス・Gateway |
| `src/mugemuge_agentd/hw/i2c.py` | `I2CBus`、実機の `SmbusI2C`（`i2c_rdwr`）、模擬の `FakeI2C`（INA226 0x40 / BME280 0x76） | デバイス・仮想デバイス |
| `src/mugemuge_agentd/hw/gpio.py` | `GPIO`、実機の `GpiodGPIO`（libgpiod v2）、模擬の `FakeGPIO` | デバイス・仮想デバイス |
| `src/mugemuge_agentd/executor.py` | `gate.check()` を通った exec の実行。二重実行の防止、バスごとの直列化、指紋の読み直し | デバイス |
| `src/mugemuge_agentd/safety.py` | 出力の自動 OFF、デッドマン、非常停止、起動・終了時の safe_value | デバイス |
| `src/mugemuge_agentd/sampler.py` | 計測ジョブ（`samples` と `job_end`） | デバイス |
| `src/mugemuge_agentd/logger.py` | 長期の記録ジョブ（spool に貯めて送り、ack で消す。通信断の間も続ける） | デバイス |
| `src/mugemuge_agentd/hw/display.py` | 表示デバイス（SH1107 128x128）。`display.draw` の文字の描画（内蔵 5x7 フォント）と書き込みの列 | デバイス |
| `src/mugemuge_agentd/hw/camera.py` | カメラ（CSI は rpicam-still、USB は fswebcam、模擬） | デバイス |
| `src/mugemuge_agentd/audit.py` | 監査ログ（`prev_sha256` の鎖、ローテート） | デバイス |
| `src/mugemuge_agentd/client.py` | Gateway への WebSocket クライアント（再接続、時計補正、切断中のイベント保持） | デバイス |
| `src/mugemuge_agentd/daemon.py` / `__main__.py` | 組み立てとコマンドライン（`run` / `pair` / `safe-outputs`） | デバイス |
| `tools/stub_gateway.py` | 開発用の最小ゲートウェイ（対話 CLI、`--auto-approve`） | 開発時 |
| `tools/gen_examples.py` | `examples/session.json` を参照実装から生成 | 開発時 |
| `tools/github_app_push.py` | GitHub App のインストレーショントークンで `git push`（鍵と ID はリポジトリの外の `~/.config/mugemuge/`） | 開発時 |
| `deploy/mugemuge-agentd.service` | systemd unit | 本番 |
| `services/gateway/` | Device Gateway（Cloud Run）。デバイスの WebSocket、ペアリング、`/internal/exec` | クラウド |
| `services/web/` | Web/API（Cloud Run）。ログイン、承認カード、**署名鍵を持つ唯一のサービス** | クラウド |
| `services/common/` | 保存先（Firestore／メモリ）と、サービス間の ID トークン検証 | クラウド |
| `services/agent/` | Agent サービス（Cloud Run、ADK + Gemini）。ツール、部品カタログ、デコーダ、承認待ちの再開（`docs/AGENT.md`） | クラウド |
| `services/web/log_page.py`, `logdata.py` | 「記録」ページ（時刻・値・写真、CSV） | クラウド |
| `services/common/blobs.py` | 写真の保管（Cloud Storage／メモリ） | クラウド |
| `tools/ui_preview.py`, `tools/log_preview.py` | クラウドなしで画面を再現して撮る（下見・デモ用） | 開発時 |
| `tools/agent_cli.py` | Agent の代役（承認付き exec の確認用） | 開発時 |
| `tools/chat_cli.py` | Agent サービスと話す確認用の CLI（UI ができるまで） | 開発時 |
| `Dockerfile`, `deploy/cloud/` | Cloud Run 用イメージとデプロイ手順（`docs/DEPLOY.md`） | クラウド |

Agent サービスには `approval.sign_grant` を使わせない（署名鍵を渡さない）。これが「エージェントは承認を偽造できない」の実体。

## 開発

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,hw]'               # hw は実機の GPIO（gpiod）。--fake-hw だけなら不要
pytest                                   # ハードなしで全件（FakeI2C / FakeGPIO と、WebSocket 越しの結合テスト）
PYTHONPATH=src python3 tools/gen_examples.py   # 例を作り直す（スキーマ違反があれば止まる）
```

push は `python3 tools/github_app_push.py`（今のブランチを origin へ）。App の秘密鍵は `~/.config/mugemuge/github-app.pem`（0600）、App ID とインストレーション ID は `~/.config/mugemuge/github-app.json` か環境変数 `GITHUB_APP_ID` / `GITHUB_APP_INSTALLATION_ID` で渡す。トークンは保存しない。

### stub_gateway とデーモンを手元で動かす

端末を2つ使う。デーモンは `--gateway-url` で stub に向ける（`ws://` は loopback のときだけ許す。マニフェストは書き換えないので `manifest_sha256` は変わらない）。

```sh
# 端末1: ゲートウェイ（承認は例専用の鍵で署名する。--auto-approve なら承認待ちを自動で再送）
PYTHONPATH=src python3 tools/stub_gateway.py

# 端末2: デーモン（ハードなし）。実機で動かすときは --fake-hw を外す
python3 -m mugemuge_agentd run --manifest examples/manifest.pi-grove.yaml --fake-hw \
    --gateway-url ws://127.0.0.1:8765/v1/device --audit-path /tmp/mugemuge-audit.jsonl
```

端末1で `scan` → `read 1 0x40 0xfe 2` → `bind 1 0x40 INA226 0xfe 2 5449 0,5` → `approve` → `sample 100 10000 bus_v:1:0x40:2:2` のように打つ。`help` で一覧、`mute` で ping を止めるとデッドマンを試せる。

例のマニフェストの公開鍵は `tools/gen_examples.py` の例専用の鍵（シードが公開されている）なので、stub の署名がそのまま通る。**本番のマニフェストでは必ず差し替える。**

## 新しい Pi に入れる（1行インストール）

Web の「デバイスを追加」を押すと、ペアリングコードと一緒に次のコマンドが出る。Pi（Raspberry Pi OS）で実行する:

```sh
curl -fsSL https://<web>/install.sh | sudo bash -s -- <6桁のコード>                 # I2C だけ許すマニフェスト
curl -fsSL https://<web>/install.sh | sudo bash -s -- <6桁のコード> --profile grove # GPIO17 リレー・GPIO27 ボタン・USB カメラも
curl -fsSL https://<web>/install.sh | sudo bash -s -- <6桁のコード> --camera csi     # Pi Camera Module も
```

| オプション | 意味 |
|---|---|
| `--profile i2c\|grove` | `i2c`（既定）は I2C バス 1 だけ。`grove` は GPIO17 リレー・GPIO27 ボタンも |
| `--camera none\|csi\|usb` | カメラ。既定は `i2c` なら `none`、`grove` なら `usb`。`csi` は rpicam-apps、`usb` は fswebcam も apt で入れる |
| `--camera-device /dev/videoN` | USB カメラのデバイス（既定 `/dev/video0`）。Pi 5 で CSI カメラも挿していると番号がずれるので `v4l2-ctl --list-devices` で確かめる |
| `--device-id ID` / `--name NAME` | デバイス ID（既定 `pi-<ホスト名>`）と表示名 |
| `-y` | 確認を省く |

- `install.sh` は [`deploy/install.sh`](deploy/install.sh) に、Web/API がその環境の Gateway の URL・承認の**公開鍵**・ソース一式の sha256 を埋めたもの。秘密鍵は含まない。下の「本番での動かし方」の手順 1〜5 を1本にしている
- `install.sh` に埋めた sha256 は `install.sh` と同じ配信元（Web/API）から来るので、部分的なダウンロードは防げるが、改ざんへの備えは TLS（https）に頼っている
- 実行前に変更点（apt・I2C の有効化・ユーザー agent・`/opt`・`/etc/mugemuge`・unit）と公開鍵を表示し、`y` で進む（`-y` で省略）。中身を先に読むなら `install.sh` を保存してから `sudo bash install.sh <コード>`
- デバイス ID は既定で `pi-<ホスト名>`（`--device-id` で変更）。**同じ ID で別の Pi をペアリングすると、前の Pi のトークンが失効する**ので台ごとに変える
- マニフェストは `mugemuge-agentd init` が作る。**既にあれば上書きしない**（人が直したものを消さない。リモートから変えない: PROTOCOL.md 0章）
- 何度実行してもよい。2回目以降はデーモンの更新になる（コードを省くとペアリングもしない）

### 入れたあとでカメラを足す

`--profile`・`--camera` が効くのはマニフェストを新しく作るときだけ。既にある Pi では `/etc/mugemuge/manifest.yaml` に人が書き足す（マニフェストはリモートから変えられない）:

```yaml
camera:                     # 書かれていないカメラは使えない
  devices:
    - id: cam0
      kind: csi             # Pi Camera Module。USB なら kind: usb と device: /dev/video0
      label: 卓上カメラ
      capture: approval     # 人や部屋が写りうるので、撮る前に人が確認する
      max_width: 1280
      max_height: 960
```

`rpicam-hello --list-cameras` で認識を確かめ（なければ `sudo apt install rpicam-apps`。USB は `fswebcam`）、`sudo systemctl restart mugemuge-agentd`。撮れないときは `sudo -u agent rpicam-still -n -t 1 -o /tmp/t.jpg` で agent の権限を切り分ける。

UART・SPI はプロトコル v1 にない（`uart.*` は v2 の予約、PROTOCOL.md 5章）ので、マニフェストにも書けない。

## 本番での動かし方（systemd、`agent` ユーザー）

`/etc`・systemd・ユーザーを変更するので、**実行前に人が内容を確認する**（CLAUDE.md の安全ルール）。

```sh
# 1. 低権限ユーザー（ログイン不可）。I2C と GPIO のグループに入れる
sudo useradd --system --home-dir /var/lib/mugemuge --no-create-home --shell /usr/sbin/nologin agent
sudo usermod -aG i2c,gpio,video agent      # video は USB カメラ（/dev/videoN）を使うとき

# 2. コードと venv を /opt に置く（root 所有、agent は読むだけ）
sudo install -d -m 0755 /opt/mugemuge-agentd
sudo rsync -a --delete --exclude .venv --exclude .git ./ /opt/mugemuge-agentd/
sudo python3 -m venv /opt/mugemuge-agentd/.venv
sudo /opt/mugemuge-agentd/.venv/bin/pip install -e '/opt/mugemuge-agentd[hw]'

# 3. マニフェスト（root 所有・0644。デーモンは読むだけ。公開鍵・gateway_url を本番用に直す）
sudo install -d -m 0755 /etc/mugemuge
sudo install -m 0644 examples/manifest.pi-grove.yaml /etc/mugemuge/manifest.yaml
sudoedit /etc/mugemuge/manifest.yaml

# 4. ペアリング（M2 の Gateway ができてから）。トークンは agent 所有・0600
sudo /opt/mugemuge-agentd/.venv/bin/python -m mugemuge_agentd pair <6桁のコード> --manifest /etc/mugemuge/manifest.yaml
sudo chown agent:agent /etc/mugemuge/device.token

# 5. unit を入れて起動
sudo install -m 0644 deploy/mugemuge-agentd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mugemuge-agentd
```

- **ログ**: デーモンのログは journald（`journalctl -u mugemuge-agentd -f`）。監査ログは `/var/lib/mugemuge/audit.jsonl`（`StateDirectory` で agent 所有に作られる。20MB でローテート、`.1`〜`.3` を残す）
- **止めたとき**: SIGTERM で計測を止め、出力を safe_value に戻し、`bye` を送ってから終わる。落ちた場合も `ExecStopPost` の `safe-outputs` が出力を戻す
- unit は `WorkingDirectory` と `Environment=HOME=` を明示している。スキーマは `MUGEMUGE_SCHEMA_DIR` で場所を指定している
