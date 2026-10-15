"""Agent サービスと話す確認用の CLI（M5 の UI ができるまで）。

    python3 tools/chat_cli.py --agent https://mugemuge-agent-….run.app --device kusupi5

自分のアカウントに Agent サービスの roles/run.invoker が要る（ID トークンは gcloud から取る）。
承認待ちになったら Web/API の画面で承認する。ここでは結果が届くまで待って表示する。
ローカル（MUGEMUGE_DEV_INSECURE=1）では --no-auth。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import httpx


def show(ev: dict) -> None:
    for c in ev.get("calls", []):
        if c["name"] != "wait_for_approval":
            print(f"  → {c['name']}({json.dumps(c['args'], ensure_ascii=False)})")
    for r in ev.get("results", []):
        resp = r["response"]
        st = resp.get("status") or resp.get("approval")
        brief = json.dumps(resp.get("data") or resp.get("result") or resp.get("message") or resp.get("summary") or "",
                           ensure_ascii=False)
        print(f"  ← {r['name']}: {st} {brief[:160]}")
    if ev.get("text") and ev["author"] != "user":
        print(f"\nむげむげ> {ev['text']}\n")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agent", required=True)
    p.add_argument("--device", required=True)
    p.add_argument("--no-auth", action="store_true")
    p.add_argument("--user", default="cli")
    a = p.parse_args()
    base = a.agent.rstrip("/")

    def headers() -> dict:
        h = {"x-mugemuge-user": a.user}
        if not a.no_auth:
            tok = subprocess.run(["gcloud", "auth", "print-identity-token"], check=True, capture_output=True,
                                 text=True).stdout.strip()
            h["Authorization"] = f"Bearer {tok}"
        return h

    with httpx.Client(timeout=900) as c:
        r = c.post(f"{base}/api/sessions", json={"device_id": a.device}, headers=headers())
        r.raise_for_status()
        sid = r.json()["session_id"]
        print(f"session {sid}（終わるには Ctrl-D）")
        after = 0
        while True:
            try:
                text = input("あなた> ").strip()
            except EOFError:
                return 0
            if not text:
                continue
            res = c.post(f"{base}/api/sessions/{sid}/messages", json={"text": text}, headers=headers()).json()
            for ev in res["events"]:
                show(ev)
            after = res["next_after"]
            status = res["status"]
            if status == "waiting_approval":
                print("（承認待ち: Web/API の画面で承認してください。結果が届くまで待ちます）")
            while status == "waiting_approval":
                time.sleep(2)
                res = c.get(f"{base}/api/sessions/{sid}/events", params={"after": after}, headers=headers()).json()
                for ev in res["events"]:
                    show(ev)
                after, status = res["next_after"], res["status"]


if __name__ == "__main__":
    sys.exit(main())
