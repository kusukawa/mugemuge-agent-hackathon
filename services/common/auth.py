"""サービス間の呼び出しの認証（docs/CLOUD.md 1章）。

Cloud Run は両サービスとも --allow-unauthenticated で動かす（デバイスと人のブラウザが来るため）。
そのかわり内部向けの API は、ここで Google の ID トークンを検証する:
audience がこのサービスの URL で、呼び出し元のメールアドレスが許可リストにあること。

開発用の認証なしモード（MUGEMUGE_DEV_INSECURE=1）は、Cloud Run 上（K_SERVICE がある）では起動を拒否する。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field

from fastapi import HTTPException, Request


def dev_insecure() -> bool:
    on = os.environ.get("MUGEMUGE_DEV_INSECURE") == "1"
    if on and os.environ.get("K_SERVICE"):
        raise RuntimeError("MUGEMUGE_DEV_INSECURE must not be set on Cloud Run")
    return on


@dataclass(eq=False)   # FastAPI の依存はハッシュ可能でなければならない（同一性で比べる）
class CallerAuth:
    """内部 API の呼び出し元の検証。allowed は許可するサービスアカウントのメールアドレス。"""

    audience: str | None
    allowed: set[str] = field(default_factory=set)
    insecure: bool = False

    @classmethod
    def from_env(cls, audience_env: str, callers_env: str) -> "CallerAuth":
        insecure = dev_insecure()
        audience = os.environ.get(audience_env)
        allowed = {e.strip() for e in os.environ.get(callers_env, "").split(",") if e.strip()}
        if not insecure and (not audience or not allowed):
            raise RuntimeError(f"{audience_env} and {callers_env} must be set (or MUGEMUGE_DEV_INSECURE=1 locally)")
        return cls(audience, allowed, insecure)

    async def __call__(self, request: Request) -> str:
        """FastAPI の依存として使う。呼び出し元のメールアドレス（開発モードでは "dev"）を返す。"""
        if self.insecure:
            return "dev"
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        try:
            claims = await asyncio.to_thread(_verify, h[7:], self.audience)
        except ValueError as e:
            raise HTTPException(401, f"invalid id token: {e}") from None
        email = claims.get("email")
        if not claims.get("email_verified") or email not in self.allowed:
            raise HTTPException(403, "caller is not allowed")
        return email


_request = None


def _verify(token: str, audience: str) -> dict:
    global _request
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token

    if _request is None:
        _request = g_requests.Request()     # 公開鍵の取得に使う（google-auth 側でキャッシュされる）
    return id_token.verify_oauth2_token(token, _request, audience=audience)


def fetch_id_token(audience: str, impersonate: str | None = None) -> str:
    """呼び出し側。Cloud Run 上ではメタデータサーバ、手元ではサービスアカウントの鍵か gcloud のなりすましで取る。"""
    if impersonate is None:
        try:
            from google.auth.transport import requests as g_requests
            from google.oauth2 import id_token

            return id_token.fetch_id_token(g_requests.Request(), audience)
        except Exception:
            pass
    cmd = ["gcloud", "auth", "print-identity-token", f"--audiences={audience}", "--include-email"]
    if impersonate:
        cmd.append(f"--impersonate-service-account={impersonate}")
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()
