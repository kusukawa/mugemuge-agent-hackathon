#!/bin/bash
# 秘密の作成。**Cloud Shell で実行する**（承認の署名鍵を Pi 上で生成・保存しないため。CLAUDE.md の安全ルール）。
#   - mugemuge-approval-key   承認の Ed25519 秘密鍵（PEM）。ディスクには残さない
#   - mugemuge-session-secret Web/API のセッション Cookie の HMAC 鍵
#   - mugemuge-users          ログインできる利用者（ID → scrypt ハッシュ）
# どの Secret も読めるのは mugemuge-web@ だけ（プロジェクト全体には付けない）。
set -euo pipefail
cd "$(dirname "$0")"; source ./env.sh

umask 077
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

if ! gcloud secrets describe mugemuge-approval-key >/dev/null 2>&1; then
    openssl genpkey -algorithm ed25519 -out "$TMP/k.pem"
    gcloud secrets create mugemuge-approval-key --replication-policy=automatic --data-file="$TMP/k.pem"
    PUB="$(openssl pkey -in "$TMP/k.pem" -pubout -outform DER | tail -c 32 | base64 | tr '+/' '-_' | tr -d '=')"
    rm -f "$TMP/k.pem"
    echo
    echo "=== デバイスのマニフェスト（/etc/mugemuge/manifest.yaml）の approval.public_keys に書く ==="
    echo "    - kid: ${APPROVAL_KID}"
    echo "      key: ${PUB}"
    echo
else
    echo "mugemuge-approval-key はもうある（作り直さない）"
fi

if ! gcloud secrets describe mugemuge-session-secret >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -d '\n' | gcloud secrets create mugemuge-session-secret \
        --replication-policy=automatic --data-file=-
fi

if ! gcloud secrets describe mugemuge-users >/dev/null 2>&1; then
    echo "ログインできる利用者を登録する（空の ID で終わり）。審査員用のテストアカウントもここで作る"
    # python3 - <<PY にすると input() が標準入力（スクリプト本体）を読んで EOF になるので、ファイルに書いて端末から読む
    cat > "$TMP/mkusers.py" <<'PY'
import base64, getpass, hashlib, json, secrets, sys
users = {}
while True:
    uid = input("ID: ").strip()
    if not uid:
        break
    pw = getpass.getpass("password: ")
    if pw != getpass.getpass("again: "):
        print("一致しない。もう一度"); continue
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    b = lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")
    users[uid] = f"scrypt${2**14}$8$1${b(salt)}${b(dk)}"
if not users:
    sys.exit("利用者が1人もいない")
open(sys.argv[1], "w").write(json.dumps(users))
PY
    python3 "$TMP/mkusers.py" "$TMP/users.json" < /dev/tty
    gcloud secrets create mugemuge-users --replication-policy=automatic --data-file="$TMP/users.json"
fi

for s in mugemuge-approval-key mugemuge-session-secret mugemuge-users; do
    gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$WEB_SA" \
        --role=roles/secretmanager.secretAccessor --condition=None >/dev/null
done
echo "secrets done. 次は 03_deploy.sh"
