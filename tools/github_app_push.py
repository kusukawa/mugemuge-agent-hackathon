"""GitHub App のインストレーショントークンで git push する（開発用）。

    python3 tools/github_app_push.py                 # 今のブランチを origin へ
    python3 tools/github_app_push.py --dry-run main  # オプションと refspec は git push に渡す（送り先は常に origin）

App の秘密鍵で JWT（RS256、有効9分）に署名し、インストレーショントークン（有効1時間）を受け取って push する。
トークンは GIT_ASKPASS 経由で git に渡すだけで、URL・.git/config・ファイルには書かない。

設定（環境変数が優先。なければ ~/.config/mugemuge/github-app.json の app_id / installation_id / key_path）:

    GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_KEY（既定 ~/.config/mugemuge/github-app.pem）

秘密鍵はリポジトリの外に 0600 で置く。リポジトリには入れない。
"""
from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config/mugemuge/github-app.json"
DEFAULT_KEY = Path.home() / ".config/mugemuge/github-app.pem"


def load_config() -> tuple[str, str, Path]:
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    app_id = os.environ.get("GITHUB_APP_ID") or cfg.get("app_id")
    inst = os.environ.get("GITHUB_APP_INSTALLATION_ID") or cfg.get("installation_id")
    key = Path(os.environ.get("GITHUB_APP_KEY") or cfg.get("key_path") or DEFAULT_KEY).expanduser()
    if not app_id or not inst:
        sys.exit(f"GITHUB_APP_ID / GITHUB_APP_INSTALLATION_ID を環境変数か {CONFIG} で指定してください")
    if not key.exists():
        sys.exit(f"秘密鍵がありません: {key}")
    if key.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit(f"秘密鍵のパーミッションが緩すぎます（chmod 600 {key}）")
    return str(app_id), str(inst), key


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def app_jwt(app_id: str, key_path: Path) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    now = int(time.time())
    signing_input = (_b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "."
                     + _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode()))
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(sig)}"


def installation_token(app_id: str, inst: str, key_path: Path) -> str:
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{inst}/access_tokens", method="POST",
        headers={"Authorization": f"Bearer {app_jwt(app_id, key_path)}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mugemuge-github-app-push"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())["token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"トークンを取得できませんでした: HTTP {e.code} {e.read()[:200].decode(errors='replace')}")


def main() -> int:
    app_id, inst, key = load_config()
    token = installation_token(app_id, inst, key)
    # 送り先は常に origin。"-" で始まるものは git push のオプション、それ以外は refspec（省略時は HEAD）
    opts = [a for a in sys.argv[1:] if a.startswith("-")]
    refs = [a for a in sys.argv[1:] if not a.startswith("-")] or ["HEAD"]
    args = [*opts, "origin", *refs]
    with tempfile.TemporaryDirectory() as d:
        askpass = Path(d) / "askpass.sh"
        # ユーザー名を聞かれたら x-access-token、パスワードならトークン（環境変数からだけ読む）
        askpass.write_text('#!/bin/sh\ncase "$1" in Username*) echo x-access-token ;; *) echo "$GH_APP_TOKEN" ;; esac\n')
        askpass.chmod(0o700)
        env = {**os.environ, "GIT_ASKPASS": str(askpass), "GH_APP_TOKEN": token, "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.call(["git", "-c", "credential.helper=", "push", *args], env=env)


if __name__ == "__main__":
    sys.exit(main())
