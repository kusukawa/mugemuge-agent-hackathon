#!/usr/bin/env bash
# むげむげ Pi 側デーモンのインストーラ。
#
# Web/API の /install.sh から配る。@@…@@ は配るときに Web/API がその環境の値で埋める
# （Gateway の URL・承認の公開鍵・デーモンのソース一式の sha256）。秘密鍵は含まない。
#
#   curl -fsSL https://<web>/install.sh | sudo bash -s -- <6桁のコード> [--profile grove] [--camera csi]
#
# やること（PROTOCOL.md・README の「本番での動かし方」と同じ手順を1本にしたもの）:
#   1. apt で python3-venv と i2c-tools（カメラを使うなら rpicam-apps か fswebcam も）を入れ、I2C を有効にする
#   2. 低権限ユーザー agent を作り、i2c・gpio・video グループに入れる
#   3. デーモンを /opt/mugemuge-agentd に置き、venv に入れる
#   4. /etc/mugemuge/manifest.yaml を作る（既にあれば触らない）
#   5. ペアリングコードでトークンを受け取る（agent 所有・0600）
#   6. systemd の unit を入れて起動する
#
# 何度実行してもよい（2回目以降はデーモンの更新になる。マニフェストは上書きしない）。
set -euo pipefail

WEB_URL='@@WEB_URL@@'
GATEWAY_WS_URL='@@GATEWAY_WS_URL@@'
APPROVAL_KID='@@APPROVAL_KID@@'
APPROVAL_KEY='@@APPROVAL_KEY@@'
BUNDLE_SHA256='@@BUNDLE_SHA256@@'

PREFIX=/opt/mugemuge-agentd
ETC=/etc/mugemuge
MANIFEST=$ETC/manifest.yaml
TOKEN=$ETC/device.token
UNIT=/etc/systemd/system/mugemuge-agentd.service

usage() {
    cat <<EOF
使い方: sudo bash install.sh [<6桁のペアリングコード>] [オプション]

  --profile i2c|grove   作るマニフェストの中身（既定 i2c）
                          i2c   I2C バス 1 だけ。GPIO・カメラは何も許さない
                          grove GPIO17 リレー・GPIO27 ボタンも許す（カメラの既定は usb）
  --camera none|csi|usb カメラ（既定: i2c は none、grove は usb）
                          csi   Pi Camera Module（rpicam-still）
                          usb   UVC カメラ（fswebcam）
  --camera-device DEV   USB カメラのデバイス（既定 /dev/video0）
  --device-id ID        デバイス ID（既定: pi-<ホスト名>）。Web の画面に出る名前
  --name NAME           表示名（既定: デバイス ID）
  -y, --yes             確認せずに進める

ペアリングコードは Web の「デバイスを追加」で出す（10分・1回だけ有効）。
既にペアリング済みでデーモンだけ更新するときは省略できる。
EOF
}

# apt で入れるもの。カメラのコマンドは、マニフェストを新しく作るときに選んだものだけ
packages() {
    echo python3-venv; echo i2c-tools
    if [ ! -f "$MANIFEST" ]; then
        case "$CAMERA" in csi) echo rpicam-apps ;; usb) echo fswebcam ;; esac
    fi
}

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31mエラー: %s\033[0m\n' "$*" >&2; exit 1; }

CODE="" PROFILE=i2c CAMERA="" CAMERA_GIVEN=0 CAMERA_DEVICE=/dev/video0 DEVICE_ID="" NAME="" YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --profile)   PROFILE=${2:?}; shift 2 ;;
        --camera)    CAMERA=${2:?}; CAMERA_GIVEN=1; shift 2 ;;
        --camera-device) CAMERA_DEVICE=${2:?}; shift 2 ;;
        --device-id) DEVICE_ID=${2:?}; shift 2 ;;
        --name)      NAME=${2:?}; shift 2 ;;
        -y|--yes)    YES=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        [0-9][0-9][0-9][0-9][0-9][0-9]) CODE=$1; shift ;;
        *) usage >&2; die "分からない引数: $1" ;;
    esac
done

case "$PROFILE" in i2c|grove) ;; *) die "--profile は i2c か grove" ;; esac
if [ -z "$CAMERA" ]; then
    if [ "$PROFILE" = grove ]; then CAMERA=usb; else CAMERA=none; fi
fi
case "$CAMERA" in none|csi|usb) ;; *) die "--camera は none・csi・usb のどれか" ;; esac
echo "$CAMERA_DEVICE" | grep -Eq '^/dev/video[0-9]{1,2}$' || die "--camera-device は /dev/videoN"
[ "$(id -u)" -eq 0 ] || die "root で実行してください（sudo bash install.sh …）"
[ -n "$CODE" ] || [ -s "$TOKEN" ] || die "初回はペアリングコードが要ります（Web の「デバイスを追加」）"

# ボード（マニフェストの device.board。スキーマの列挙に合わせる）
MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)
case "$MODEL" in
    *"Pi 5"*)      BOARD=raspberry-pi-5 ;;
    *"Pi 4"*)      BOARD=raspberry-pi-4 ;;
    *"Zero 2 W"*)  BOARD=raspberry-pi-zero-2-w ;;
    *)             BOARD=raspberry-pi-zero-2-w
                   echo "注意: ボード「$MODEL」は一覧にないので raspberry-pi-zero-2-w として登録します" ;;
esac

if [ -z "$DEVICE_ID" ]; then
    DEVICE_ID="pi-$(hostname | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9\n-' '-' | head -c 59)"
fi
echo "$DEVICE_ID" | grep -Eq '^[a-z0-9][a-z0-9-]{1,62}$' || die "デバイス ID「$DEVICE_ID」は英小文字・数字・- で（--device-id で指定）"
case "$DEVICE_ID" in virt-*) die "virt- で始まる ID は仮想デバイス用です" ;; esac

cat <<EOF

むげむげのデーモンを入れます。
  ボード         $MODEL
  接続先         $GATEWAY_WS_URL
  承認の公開鍵   $APPROVAL_KID  $APPROVAL_KEY
EOF
if [ -f "$MANIFEST" ]; then
    echo "  マニフェスト   $MANIFEST は既にあるのでそのまま使う（--profile・--camera は効かない）"
else
    cam_desc=$CAMERA; [ "$CAMERA" = usb ] && cam_desc="usb $CAMERA_DEVICE"
    echo "  マニフェスト   $MANIFEST を新しく作る（デバイス ID $DEVICE_ID、プロファイル $PROFILE、カメラ $cam_desc）"
fi
cat <<EOF
  変更するもの   apt（$(packages | tr '\n' ' ')）、I2C の有効化、ユーザー agent、
                 $PREFIX、$ETC、$UNIT
  ペアリング     ${CODE:-しない（既存のトークンを使う）}

EOF
if [ "$YES" -ne 1 ]; then
    if [ -r /dev/tty ]; then
        read -r -p "続けますか？ [y/N] " ans </dev/tty || ans=""
    else
        ans=""
    fi
    case "$ans" in y|Y|yes) ;; *) die "やめました（-y で確認を省けます）" ;; esac
fi

say "1/6 必要なパッケージと I2C"
missing=""
for p in $(packages); do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
if [ -n "$missing" ]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $missing
fi
if [ ! -e /dev/i2c-1 ]; then
    if command -v raspi-config >/dev/null; then
        raspi-config nonint do_i2c 0
        echo "I2C を有効にしました"
    else
        echo "注意: /dev/i2c-1 がありません。I2C を有効にしてください（raspi-config）"
    fi
fi

say "2/6 ユーザー agent"
if ! id agent >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/mugemuge --no-create-home --shell /usr/sbin/nologin agent
fi
for g in i2c gpio video; do
    getent group "$g" >/dev/null && usermod -aG "$g" agent
done

say "3/6 デーモン本体（$PREFIX）"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$WEB_URL/install/agentd.tar.gz" -o "$tmp/agentd.tar.gz"
echo "$BUNDLE_SHA256  $tmp/agentd.tar.gz" | sha256sum -c --quiet - || die "ダウンロードしたソースの sha256 が合いません"
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/src" "$PREFIX/schemas" "$PREFIX/deploy"
tar -xzf "$tmp/agentd.tar.gz" -C "$PREFIX" --no-same-owner
[ -x "$PREFIX/.venv/bin/python" ] || python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -q -e "$PREFIX[hw]"
export MUGEMUGE_SCHEMA_DIR=$PREFIX/schemas      # unit と同じ

say "4/6 マニフェスト（$MANIFEST）"
install -d -m 0755 "$ETC"
init_args=(--manifest "$MANIFEST" --device-id "$DEVICE_ID" --board "$BOARD" --profile "$PROFILE"
           --gateway-url "$GATEWAY_WS_URL" --kid "$APPROVAL_KID" --key "$APPROVAL_KEY")
[ -n "$NAME" ] && init_args+=(--name "$NAME")
[ "$CAMERA" != none ] && init_args+=(--camera "$CAMERA" --camera-device "$CAMERA_DEVICE")
if [ ! -f "$MANIFEST" ]; then
    "$PREFIX/.venv/bin/python" -m mugemuge_agentd init "${init_args[@]}"
elif [ "$CAMERA_GIVEN" -eq 1 ] && [ "$CAMERA" != none ] && ! grep -q '^camera:' "$MANIFEST"; then
    echo "注意: 既存のマニフェストにカメラがありません。--camera は既存のマニフェストを変えません。"
    echo "      $MANIFEST に camera: を書き足して sudo systemctl restart mugemuge-agentd（README の「カメラを足す」）"
fi
if [ -f "$MANIFEST" ] && ! grep -qF "$APPROVAL_KEY" "$MANIFEST"; then
    echo "注意: 既存のマニフェストにこの環境の承認の公開鍵がありません。承認が通らないので $MANIFEST を確かめてください"
fi

say "5/6 ペアリング"
if [ -n "$CODE" ]; then
    "$PREFIX/.venv/bin/python" -m mugemuge_agentd pair "$CODE" --manifest "$MANIFEST" --token-file "$TOKEN"
fi
chown agent:agent "$TOKEN"
chmod 0600 "$TOKEN"

say "6/6 systemd"
install -m 0644 "$PREFIX/deploy/mugemuge-agentd.service" "$UNIT"
systemctl daemon-reload
systemctl enable -q mugemuge-agentd
systemctl restart mugemuge-agentd
sleep 3
if systemctl is-active -q mugemuge-agentd; then
    say "完了。$WEB_URL を開くと、このデバイスがオンラインで出ます"
else
    journalctl -u mugemuge-agentd -n 20 --no-pager || true
    die "デーモンが起動していません（journalctl -u mugemuge-agentd）"
fi
cat <<EOF
  ログ          journalctl -u mugemuge-agentd -f
  監査ログ      /var/lib/mugemuge/audit.jsonl
  マニフェスト  $MANIFEST（センサやリレーをつないだら直して sudo systemctl restart mugemuge-agentd）
EOF
