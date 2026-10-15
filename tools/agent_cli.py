"""Agent の代役（M2 の確認用。M3 で ADK エージェントに置き換える）。

exec を Gateway に送り、approval_required ならデバイスが返した request を Web/API に登録して、
人がブラウザで承認するのを待ち、グラントを付けて同じ op と args で送り直す（PROTOCOL.md 7.1節）。
署名鍵には触れない。グラントを作れるのは Web/API だけ。

    python3 tools/agent_cli.py --gateway https://mugemuge-gateway-… --web https://mugemuge-web-… \\
        --device pi-desk-01 --impersonate mugemuge-agent@PROJECT.iam.gserviceaccount.com \\
        i2c.bind '{"bus":1,"addr":118,"part":"BMP280","fingerprint":{"reg":208,"len":1,"hex":"58"},"write_allow_regs":[244]}'

ローカル（MUGEMUGE_DEV_INSECURE=1 の Gateway / Web/API）では --no-auth を付ける。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class Agent:
    def __init__(self, gateway: str, web: str, device_id: str, *, gw_headers: dict | None = None,
                 web_headers: dict | None = None, poll_s: float = 2.0, approval_timeout_s: float = 600,
                 run_id: str | None = None, say=print):
        self.gateway, self.web, self.device_id = gateway.rstrip("/"), web.rstrip("/"), device_id
        self.gw_headers, self.web_headers = gw_headers or {}, web_headers or {}
        self.poll_s, self.approval_timeout_s = poll_s, approval_timeout_s
        self.run_id = run_id or "run_cli_" + secrets.token_hex(6)
        self.say = say
        self.step = 0

    async def exec(self, op: str, args: dict, approval: dict | None = None) -> dict:
        self.step += 1
        body = {"device_id": self.device_id, "op": op, "args": args, "run_id": self.run_id,
                "step_id": f"stp_{self.step:04d}", "id": "x-" + secrets.token_hex(12)}
        if approval is not None:
            body["approval"] = approval
        async with httpx.AsyncClient(timeout=40) as c:
            for attempt in range(3):
                r = await c.post(f"{self.gateway}/internal/exec", json=body, headers=self.gw_headers)
                if r.status_code != 504:
                    break
                self.say(f"   (timeout; resending the same id {body['id']})")   # 同じ id なら二重実行されない
        if r.status_code != 200:
            raise RuntimeError(f"gateway {r.status_code}: {r.text[:300]}")
        return r.json()

    async def wait_for_grant(self, request: dict) -> dict | None:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{self.web}/api/approvals", json={**request, "run_id": self.run_id},
                             headers=self.web_headers)
            if r.status_code != 200:
                raise RuntimeError(f"web {r.status_code}: {r.text[:300]}")
            aid = r.json()["approval_id"]
            self.say(f"承認待ち {aid}: 「{request['summary']}」をブラウザで承認してください")
            loop = asyncio.get_running_loop()
            end = loop.time() + self.approval_timeout_s
            while loop.time() < end:
                await asyncio.sleep(self.poll_s)
                st = (await c.get(f"{self.web}/api/approvals/{aid}", headers=self.web_headers)).json()
                if st["status"] == "approved":
                    self.say(f"承認されました（grant {st['grant']['grant_id']}, by {st['grant']['approved_by']}）")
                    return st["grant"]
                if st["status"] in ("denied", "expired"):
                    self.say(f"承認されませんでした: {st['status']}")
                    return None
        self.say("承認を待ちきれませんでした")
        return None

    async def run(self, op: str, args: dict) -> dict:
        r = await self.exec(op, args)
        if not r["ok"] and r["error"]["code"] in ("approval_required", "approval_invalid"):
            grant = await self.wait_for_grant(r["error"]["request"])
            if grant is not None:
                r = await self.exec(op, args, approval=grant)
        return r


def _show(r: dict) -> None:
    pol = r.get("policy", {})
    if r.get("ok"):
        print(f"ok [{pol.get('decision')} {pol.get('rule')}] {json.dumps(r['data'], ensure_ascii=False)}")
    else:
        e = r["error"]
        print(f"{e['code']} [{pol.get('decision')} {pol.get('rule')}] {e['message']}")


def main() -> int:
    p = argparse.ArgumentParser(description="Agent の代役（承認つき exec）")
    p.add_argument("--gateway", required=True)
    p.add_argument("--web", required=True)
    p.add_argument("--device", required=True)
    p.add_argument("--impersonate", help="ID トークンを取るサービスアカウント（mugemuge-agent@…）")
    p.add_argument("--no-auth", action="store_true", help="ローカルの開発モード用")
    p.add_argument("op")
    p.add_argument("args", nargs="?", default="{}")
    a = p.parse_args()

    gw_h, web_h = {}, {}
    if not a.no_auth:
        from services.common.auth import fetch_id_token

        gw_h = {"Authorization": "Bearer " + fetch_id_token(a.gateway, a.impersonate)}
        web_h = {"Authorization": "Bearer " + fetch_id_token(a.web, a.impersonate)}
    agent = Agent(a.gateway, a.web, a.device, gw_headers=gw_h, web_headers=web_h)
    r = asyncio.run(agent.run(a.op, json.loads(a.args)))
    _show(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
