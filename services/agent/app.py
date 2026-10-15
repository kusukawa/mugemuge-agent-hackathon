"""Agent サービス（Cloud Run、--no-allow-unauthenticated。呼べるのは IAM の起動元＝Web/API など）。

    POST /api/sessions                 {"device_id"}            → {"session_id"}
    POST /api/sessions/{id}/messages   {"text"}                 → 実行して、増えたイベントと状態を返す
    GET  /api/sessions/{id}/events?after=N                      → イベントの続き（承認後の再開分も含む）
    GET  /api/sessions/{id}/series/{job_id}                     → 換算済みの系列（グラフ用）

承認: wait_for_approval で止まったセッションは ApprovalWatcher が見張る。Web/API で承認されたら、
グラントを付けて同じ op と args をデバイスに送り直し、その結果を wait_for_approval の応答として差し込んで再開する。

    uvicorn --factory services.agent.app:app_from_env
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from ..common.auth import dev_insecure, fetch_id_token
from ..common.tracing import setup_tracing
from .agent import DEFAULT_MODEL, Runtime, build_agent, set_runtime
from .device_tools import CloudClient, PendingApproval

log = logging.getLogger("mugemuge.agent.app")

APP_NAME = "mugemuge"
WATCH_EVERY_S = 2.0
APPROVAL_GIVE_UP_S = 11 * 60


def simplify(ev) -> dict:
    """ADK のイベントを UI と API 向けの小さな形にする。"""
    out: dict[str, Any] = {"id": ev.id, "author": ev.author, "ts": int(ev.timestamp * 1000) if ev.timestamp else None}
    texts, calls, results = [], [], []
    for p in (ev.content.parts if ev.content and ev.content.parts else []):
        if p.text and not getattr(p, "thought", False):
            texts.append(p.text)
        if p.function_call:
            calls.append({"id": p.function_call.id, "name": p.function_call.name, "args": dict(p.function_call.args or {})})
        if p.function_response:
            results.append({"id": p.function_response.id, "name": p.function_response.name,
                            "response": p.function_response.response})
    if texts:
        out["text"] = "".join(texts)
    if calls:
        out["calls"] = calls
    if results:
        out["results"] = results
    if ev.long_running_tool_ids:
        out["waiting"] = sorted(ev.long_running_tool_ids)
    return out


def failure_text(e: Exception) -> str:
    """実行失敗をチャットに出す文。Gemini の混雑（送り直しても 429 のまま）は、待てば続けられると伝える。"""
    if getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e):
        return ("Gemini が混み合っていて、何度か送り直しても応答がありませんでした（429 RESOURCE_EXHAUSTED）。"
                "1〜2分待ってから「続けて」と送ると、ここまでの続きから再開します。")
    return f"エージェントの実行に失敗しました: {e}"[:300]


class SessionLog:
    def __init__(self, device_id: str, user_id: str):
        self.device_id, self.user_id = device_id, user_id
        self.events: list[dict] = []
        self.lock = asyncio.Lock()
        self.status = "idle"            # idle / running / waiting_approval / error


class AgentService:
    def __init__(self, runtime: Runtime, model: str = DEFAULT_MODEL, *, model_obj=None):
        self.rt = runtime
        set_runtime(runtime)
        self.sessions = InMemorySessionService()
        self.agent = build_agent(model_obj if model_obj is not None else model)
        self.runner = Runner(app_name=APP_NAME, agent=self.agent, session_service=self.sessions)
        self.logs: dict[str, SessionLog] = {}
        self._watcher: asyncio.Task | None = None
        # 承認の確認は Web からの通知・画面の問い合わせ・見張り役の3か所から同時に走りうる。
        # 同じ承認を2回処理すると、送り直しが2回（2回目は承認の使い回しで拒否）、再開も2回になり、
        # モデルが混乱して同じ操作をもう一度求める（本番で確認）。1つずつ処理する
        self._check_lock = asyncio.Lock()

    async def create(self, device_id: str, user_id: str) -> str:
        await self.rt.toolkit(device_id, refresh=True)     # デバイスがなければここで 404
        s = await self.sessions.create_session(app_name=APP_NAME, user_id=user_id, state={"device_id": device_id})
        self.logs[s.id] = SessionLog(device_id, user_id)
        return s.id

    async def _drive(self, sid: str, message: types.Content) -> list[dict]:
        lg = self.logs[sid]
        new: list[dict] = []
        async with lg.lock:
            lg.status = "running"
            try:
                async for ev in self.runner.run_async(user_id=lg.user_id, session_id=sid, new_message=message):
                    e = simplify(ev)
                    lg.events.append(e)
                    new.append(e)
            except Exception as e:
                log.exception("agent run failed in session %s", sid)
                err = {"id": f"err-{len(lg.events)}", "author": "system", "text": failure_text(e)}
                lg.events.append(err)
                new.append(err)
                lg.status = "error"
                return new
            waiting = any(p.adk_session_id == sid for p in self.rt.pending.items.values())
            lg.status = "waiting_approval" if waiting else "idle"
        return new

    async def say(self, sid: str, text: str) -> list[dict]:
        lg = self.logs[sid]
        lg.events.append({"id": f"user-{len(lg.events)}", "author": "user", "text": text,
                          "ts": int(time.time() * 1000)})        # 画面を開き直しても発言が残るように
        return await self._drive(sid, types.Content(role="user", parts=[types.Part(text=text)]))

    async def resume(self, p: PendingApproval, response: dict) -> list[dict]:
        # 同じ承認待ちに wait_for_approval が重ねて呼ばれていたら、全部の呼び出しに同じ応答を返す
        parts = [types.Part(function_response=types.FunctionResponse(id=cid, name="wait_for_approval",
                                                                     response=response)) for cid in p.call_ids]
        # 差し込む応答は ADK のイベントとしては返ってこないので、画面のためにここで記録する
        lg = self.logs[p.adk_session_id]
        lg.events.append({"id": f"approval-{p.approval_id}", "author": "approval", "ts": int(time.time() * 1000),
                          "results": [{"id": p.call_ids[0], "name": "wait_for_approval", "response": response}]})
        return await self._drive(p.adk_session_id, types.Content(role="user", parts=parts))

    # ---- 承認の見張り ------------------------------------------------------------------

    async def check_approvals(self) -> int:
        """承認・却下・期限切れになったものを処理する。処理した数を返す。"""
        # 決まった承認待ちを、ロックの中で取り出して自分の担当にする（同じ承認を2か所で処理しない）。
        # 送り直しと再開（LLM の1ターン）はロックの外で行い、ほかの会話の再開や画面の問い合わせを待たせない
        claimed: list[tuple[PendingApproval, dict]] = []
        async with self._check_lock:
            for p in list(self.rt.pending.items.values()):
                if not p.call_ids:
                    if time.time() - p.created > APPROVAL_GIVE_UP_S:
                        self.rt.pending.pop(p.approval_id)       # wait_for_approval が呼ばれないまま放置された
                    continue
                try:
                    st = await self.rt.cloud.get_approval(p.approval_id)
                except Exception as e:
                    log.warning("cannot read approval %s: %s", p.approval_id, e)
                    continue
                if st["status"] == "pending":
                    continue
                self.rt.pending.pop(p.approval_id)
                claimed.append((p, st))
        for p, st in claimed:
            await self._finish(p, st)
        return len(claimed)

    async def _finish(self, p: PendingApproval, st: dict) -> None:
        if st["status"] == "approved":
            tk = await self.rt.toolkit(p.device_id)
            status, r = await tk.send(p.op, p.args, run_id=p.run_id, step_id=f"{p.call_ids[-1]}-approved",
                                      approval=st["grant"])
            if status == 200 and not r["ok"] and r["error"]["code"] == "approval_invalid":
                result = tk.approval_rejected(p.adk_session_id, p.op, p.args, r)
            elif status == 200:
                result = tk.digest(p.op, p.args, r)
            else:
                result = {"status": "error", "code": r.get("error", f"http_{status}"),
                          "message": "承認後の送信がデバイスに届かなかった"}
            response = {"approval": "approved", "approved_by": st["grant"]["approved_by"], "result": result}
        elif st.get("reason") == "session_ended":
            response = {"approval": "expired", "reason": "session_ended",
                        "message": "デバイスとの接続が切れたため、この承認待ちは無効になった（人が却下したのではない）。"
                                   "まだ必要なら、同じ操作をもう一度デバイスに求める（新しい承認カードが出る）"}
        else:
            response = {"approval": st["status"],
                        "message": "人が承認しなかった。この操作はしないで、代わりの方法か中止を伝える"}
        response["approval_id"] = p.approval_id
        log.info("approval %s → %s; resuming session %s", p.approval_id, st["status"], p.adk_session_id)
        await self.resume(p, response)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(WATCH_EVERY_S)
            try:
                await self.check_approvals()
            except Exception:
                log.exception("approval watcher failed")

    def start(self) -> None:
        if self._watcher is None:
            self._watcher = asyncio.create_task(self.watch(), name="approval-watcher")


def create_app(service: AgentService) -> FastAPI:
    import contextlib

    @contextlib.asynccontextmanager
    async def lifespan(app):
        service.start()
        yield

    app = FastAPI(title="mugemuge-agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.service = service

    def user_of(request: Request) -> str:
        # Cloud Run の IAM を通った呼び出し元（Web/API）が、ログイン中の利用者をこのヘッダで伝える
        return request.headers.get("x-mugemuge-user", "anonymous")[:64]

    def log_of(sid: str):
        lg = service.logs.get(sid)
        if lg is None:
            raise HTTPException(404, "unknown session")
        return lg

    @app.post("/api/sessions")
    async def create_session(request: Request):
        body = await request.json()
        try:
            sid = await service.create(str(body["device_id"]), user_of(request))
        except KeyError:
            raise HTTPException(400, "device_id is required") from None
        except Exception as e:
            raise HTTPException(404, f"device is not available: {e}") from None
        return {"session_id": sid}

    @app.post("/api/sessions/{sid}/messages")
    async def post_message(sid: str, request: Request):
        lg = log_of(sid)
        text = str((await request.json()).get("text", "")).strip()
        if not text:
            raise HTTPException(400, "text is required")
        if lg.lock.locked():
            raise HTTPException(409, "the agent is still working on this session")
        new = await service.say(sid, text)
        return {"events": new, "status": lg.status, "next_after": len(lg.events)}

    @app.get("/api/sessions/{sid}/events")
    async def get_events(sid: str, after: int = 0):
        lg = log_of(sid)
        if lg.status == "waiting_approval" and not lg.lock.locked():
            # Cloud Run はリクエストのない間 CPU を絞るので、見張り役だけに頼らず、取りに来たときにも確かめる
            await service.check_approvals()
        return {"events": lg.events[after:], "status": lg.status, "next_after": len(lg.events)}

    @app.post("/api/approvals/notify")
    async def approval_notify():
        """Web/API が承認・却下した直後に呼ぶ。すぐ確かめて、待っているセッションを再開する。"""
        return {"resumed": await service.check_approvals()}

    @app.get("/api/sessions/{sid}/series/{job_id}")
    async def get_series(sid: str, job_id: str):
        """換算済みの系列（列名 → {decoder, column, points: [{t_ms, 物理量…}], complete}）。"""
        lg = log_of(sid)
        tk = service.rt.toolkits.get(lg.device_id)
        if tk is None or job_id not in tk.series:
            raise HTTPException(404, "no decoded series for this job yet")
        return {"job_id": job_id, "columns": tk.series[job_id]}

    @app.get("/api/sessions/{sid}/panel")
    async def get_panel(sid: str):
        """テスター画面に出すもの（エージェントが show_on_tester で決める）。"""
        log_of(sid)
        return service.rt.panels.get(sid, {"version": 0, "widgets": []})

    @app.get("/api/sessions/{sid}/jobs/{job_id}")
    async def get_job(sid: str, job_id: str):
        """計測の途中経過（進み具合と、換算できた列の系列）。画面が数秒ごとに問い合わせる。"""
        lg = log_of(sid)
        tk = service.rt.toolkits.get(lg.device_id)
        try:
            return await tk.live(job_id)
        except (KeyError, AttributeError):
            raise HTTPException(404, "unknown job") from None

    @app.get("/health")
    async def health():
        return {"ok": True, "pending_approvals": len(service.rt.pending.items)}

    return app


def app_from_env() -> FastAPI:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    insecure = dev_insecure()
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")      # 新しい Gemini はグローバルエンドポイントにある
    if os.environ.get("MUGEMUGE_TRACE", "1") == "1" and not insecure:
        setup_tracing()

    tokens: dict[str, tuple[float, str]] = {}

    async def headers_for(audience: str) -> dict:
        if insecure:
            return {}
        t = tokens.get(audience)
        if t is None or t[0] < time.time():
            tok = await asyncio.to_thread(fetch_id_token, audience)
            tokens[audience] = t = (time.time() + 45 * 60, tok)
        return {"Authorization": f"Bearer {t[1]}"}

    cloud = CloudClient(os.environ["GATEWAY_URL"], os.environ["WEB_URL"], headers_for)
    return create_app(AgentService(Runtime(cloud), os.environ.get("MUGEMUGE_MODEL", DEFAULT_MODEL)))
