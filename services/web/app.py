"""Web/API（docs/CLOUD.md 4章）。署名鍵を持つのはこのサービスだけ。

    uvicorn --factory services.web.app:app_from_env

環境変数（本番は Secret Manager から注入）:
    APPROVAL_SIGNING_KEY   Ed25519 秘密鍵（PKCS8 PEM）。Secret mugemuge-approval-key
    APPROVAL_KID           その鍵の kid（デバイスのマニフェストの approval.public_keys と一致させる）
    MUGEMUGE_USERS         {"id": "scrypt$…"} の JSON。Secret mugemuge-users
    SESSION_SECRET         セッション Cookie の HMAC 鍵（32 バイト以上）。Secret mugemuge-session-secret
    GATEWAY_URL            Gateway の URL（非常停止ボタンが /internal/exec を呼ぶ。audience にもなる）
    WEB_AUDIENCE, API_CALLERS   Agent 向け /api/* の ID トークン検証
    AGENT_URL              承認・却下をすぐ Agent に知らせる（なくても Agent は問い合わせ時に確かめる）
    VIRTUAL_URL            仮想デバイスのサービス（「仮想デバイスで試す」）
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import secrets
import time

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mugemuge_agentd.approval import public_key_to_b64, sign_grant
from mugemuge_agentd.canonical import args_sha256

from ..common.auth import CallerAuth, dev_insecure, fetch_id_token
from ..common.store import Store, make_store
from ..agent.parts import PARTS
from ..agent.parts import decode as PARTS_DECODE
from ..common.blobs import BlobStore, make_blob_store
from .chat_page import chat_page
from .install import Installer
from .explain import explain, pins_svg
from .log_page import detail_page, list_page
from .logdata import CHANNELS as LOG_CHANNELS
from .logdata import series_for_view, to_csv
from .tester import Tester
from .security import Sessions, check_login

log = logging.getLogger("mugemuge.web")

COOKIE = "mg_session"
APPROVAL_TTL_MS = 10 * 60_000
GRANT_TTL_S = 300
PAIR_CODE_TTL_MS = 10 * 60_000
LOGIN_FAILURES_PER_MIN = 10
REQUEST_FIELDS = ("device_id", "session_id", "op", "args", "args_sha256", "rule", "summary")
NOTICES = {  # 画面上部の通知。クエリには決まったコードしか受け付けない（任意の文言を表示させない）
    "decided": "この承認待ちはもう決まっています",
    "session_ended": "デバイスとの接続が切れたため、この承認待ちは無効になりました。エージェントが必要なら再度求めます",
    "estop_ok": "非常停止をかけました",
    "estop_fail": "非常停止を送れませんでした（デバイスがオフラインかもしれません）",
    "estop_clear_requested": "非常停止の解除を求めました。上の「承認待ち」のカードで、人が承認すると解除されます",
    "estop_cleared": "非常停止を解除しました",
    "estop_clear_failed": "非常停止を解除できませんでした（デバイスがオフラインか、承認が無効になったかもしれません）",
    "virtual_ok": "あなた専用の仮想デバイスを作りました。数秒でオンラインになります",
    "virtual_exists": "このブラウザにはもう仮想デバイスがあります",
    "virtual_full": "仮想デバイスがいっぱいです。少し待ってからもう一度試してください",
    "virtual_fail": "仮想デバイスを作れませんでした",
    "virtual_deleted": "仮想デバイスを削除しました。「仮想デバイスで試す」で新しく作り直せます",
}


def now_ms() -> int:
    return int(time.time() * 1000)


class GatewayClient:
    """Gateway の /internal/exec を呼ぶ（Web/API のサービスアカウントの ID トークンで）。"""

    def __init__(self, base_url: str, use_id_token: bool = True):
        self.base_url = base_url.rstrip("/")
        self.use_id_token = use_id_token
        self._token: tuple[float, str] | None = None

    async def _headers(self) -> dict:
        if not self.use_id_token:
            return {}
        if self._token is None or self._token[0] < time.time():
            tok = await asyncio.to_thread(fetch_id_token, self.base_url)
            self._token = (time.time() + 45 * 60, tok)
        return {"Authorization": f"Bearer {self._token[1]}"}

    async def exec(self, body: dict) -> tuple[int, dict]:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base_url}/internal/exec", json=body, headers=await self._headers())
        return r.status_code, r.json()


class VirtualClient(GatewayClient):
    """仮想デバイスのサービス（--no-allow-unauthenticated。Web/API の SA に起動元の権限を付ける）。"""

    async def create(self, device_id: str, code: str, owner: str) -> tuple[int, dict]:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{self.base_url}/devices", json={"device_id": device_id, "code": code, "owner": owner},
                             headers=await self._headers())
        return r.status_code, r.json()

    async def list(self) -> dict:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{self.base_url}/devices", headers=await self._headers())
            r.raise_for_status()
            return r.json()

    async def delete(self, device_id: str) -> bool:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.delete(f"{self.base_url}/devices/{device_id}", headers=await self._headers())
            r.raise_for_status()
            return bool(r.json().get("stopped"))


class AgentClient(GatewayClient):
    """Agent サービス（--no-allow-unauthenticated。Web/API の SA に起動元の権限を付ける）。"""

    async def notify(self) -> None:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base_url}/api/approvals/notify", headers=await self._headers())
            r.raise_for_status()

    async def call(self, method: str, path: str, user: str, *, json_body: dict | None = None,
                   params: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
        headers = {**await self._headers(), "x-mugemuge-user": user}
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, f"{self.base_url}{path}", json=json_body, params=params, headers=headers)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"detail": r.text[:200]}


# ---- HTML -----------------------------------------------------------------------------

CSS = """
[hidden]{display:none!important}
a{color:var(--accent)}
:root{--bg:#f6f5f2;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e3e1dc;--accent:#2f6f4f;--warn:#b3261e;--chip:#eef3ef}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#ececec;--muted:#9a9a9f;--card:#1f1f1f;--line:#333;--accent:#6fbf93;--warn:#ff8a80;--chip:#24302a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:20px;margin:0}h2{font-size:16px;margin:32px 0 12px;color:var(--muted);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 12px}
.summary{font-size:17px;font-weight:600;margin:0 0 8px}.meta{color:var(--muted);font-size:13px}
.facts,.claim{border-radius:10px;padding:10px 12px;margin:0 0 8px}.facts{background:var(--chip)}
.claim{border:1px dashed var(--muted)}.claim p{margin:2px 0}
.facts .hd,.claim .hd{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:4px}
.facts b{display:inline-block;min-width:3em;color:var(--muted);font-weight:600}.meaning{color:var(--accent);font-weight:600}
.pins40{margin-top:8px}.pins40 svg{display:block;width:100%;max-width:520px;height:auto}
details summary{cursor:pointer;color:var(--muted);font-size:13px}
pre{background:var(--chip);border-radius:8px;padding:10px;overflow-x:auto;font-size:13px;margin:8px 0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}button,input{font:inherit}
button{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:8px 14px;cursor:pointer}
button.ok{background:var(--accent);border-color:var(--accent);color:#fff}button.stop{background:var(--warn);border-color:var(--warn);color:#fff}
input{padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);width:100%}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:2px 10px;font-size:12px}
.on{color:var(--accent)}.off{color:var(--muted)}.err{color:var(--warn)}form{margin:0}
button.busy::before{content:"";display:inline-block;width:12px;height:12px;margin-right:8px;vertical-align:-1px;
 border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){button.busy::before{animation:none}}
.guide p{margin:0 0 8px}.guide ol{margin:0;padding-left:22px}.guide li{margin:0 0 10px}
.login{max-width:360px;margin:12vh auto}.login label{display:block;margin:12px 0 4px;font-size:13px;color:var(--muted)}
"""


# 時間のかかるボタン（data-busy を持つもの）は、押した瞬間に「準備中」の表示に変え、二度押しと自動再読み込みを止める
BUSY_JS = """<script>
document.addEventListener('submit', e => {      // data-confirm のフォームは確かめてから送る（busy の表示より先に）
  const q = e.target.dataset.confirm;
  if (q && !confirm(q)) { e.preventDefault(); e.stopImmediatePropagation(); }
}, true);
document.querySelectorAll('button[data-busy]').forEach(b => b.form.addEventListener('submit', () => {
  document.body.dataset.busy = '1';
  document.querySelectorAll('button[data-busy]').forEach(x => x.disabled = true);
  b.textContent = b.dataset.busy; b.classList.add('busy');
}));
</script>"""


def page(title: str, body: str, refresh: bool = False) -> HTMLResponse:
    js = BUSY_JS + ("""<script>setTimeout(()=>{if(!document.querySelector('form:focus-within')&&!document.body.dataset.busy)"""
                    """location.reload()},4000)</script>""" if refresh else "")
    return HTMLResponse(f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>{CSS}</style></head><body><main>{body}</main>{js}</body></html>""",
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                            "script-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; "
                                                            "form-action 'self'"})


def esc(x) -> str:
    return html.escape(str(x))


def create_app(store: Store | None = None, caller_auth: CallerAuth | None = None, *,
               users: dict[str, str], sessions: Sessions, signing_key: Ed25519PrivateKey, kid: str,
               gateway: GatewayClient | None, cookie_secure: bool = True,
               agent: "AgentClient | None" = None, virtual: "VirtualClient | None" = None,
               virtual_ttl_s: int = 3600, blobs: BlobStore | None = None) -> FastAPI:
    app = FastAPI(title="mugemuge-web", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.notify_tasks = set()
    blobs = blobs or make_blob_store()
    store = store or make_store()
    caller_auth = caller_auth or CallerAuth.from_env("WEB_AUDIENCE", "API_CALLERS")
    login_failures: list[int] = []
    app.state.store = store
    log.info("approval signing key kid=%s public=%s", kid, public_key_to_b64(signing_key.public_key()))
    installer = (Installer.create(gateway.base_url, kid, public_key_to_b64(signing_key.public_key()))
                 if gateway is not None else None)

    # ---- セッション ------------------------------------------------------------------

    def current(request: Request) -> dict | None:
        return sessions.read(request.cookies.get(COOKIE))

    def require_user(request: Request) -> dict:
        s = current(request)
        if s is None:
            raise HTTPException(303, headers={"Location": "/login"})
        return s

    def viewer(s: dict) -> str:
        """仮想デバイスの持ち主の単位＝ブラウザのセッション（同じ test アカウントを複数の審査員が使っても分かれる）。"""
        return "vsession:" + hashlib.sha256(s["csrf"].encode()).hexdigest()[:16]

    def visible(dev: dict | None, s: dict) -> bool:
        if dev is None:
            return False
        if not dev.get("virtual"):
            return True                       # 実機はログインした人なら誰でも（人の判断で今のまま）
        return dev.get("owner") == viewer(s)  # 仮想デバイスは作ったブラウザのセッションだけ

    async def visible_device_ids(s: dict) -> set[str]:
        now = now_ms()
        return {d["device_id"] for d in await store.list_devices()
                if visible(d, s) and not (d.get("virtual") and d.get("expires_ms", 0) < now)}

    def check_csrf(s: dict, token: str) -> None:
        if not secrets.compare_digest(s["csrf"], token or ""):
            raise HTTPException(403, "bad csrf token")

    @app.get("/login")
    async def login_form(request: Request, error: str = ""):
        msg = '<p class="err">ID かパスワードが違います</p>' if error else ""
        return page("ログイン | むげむげ", f"""<div class="login card"><h1>むげむげ</h1>{msg}
<form method="post" action="/login"><label for="id">ID</label><input id="id" name="uid" autocomplete="username" required>
<label for="pw">パスワード</label><input id="pw" type="password" name="password" autocomplete="current-password" required>
<p><button class="ok" type="submit">ログイン</button></p></form></div>""")

    @app.post("/login")
    async def login(uid: str = Form(...), password: str = Form(...)):
        t = now_ms()
        login_failures[:] = [x for x in login_failures if x > t - 60_000]
        if len(login_failures) >= LOGIN_FAILURES_PER_MIN:
            raise HTTPException(429, "too many failed logins; wait a minute")
        if not await asyncio.to_thread(check_login, users, uid, password):
            login_failures.append(t)
            log.warning("login failed for %r", uid[:64])
            return RedirectResponse("/login?error=1", 303)
        log.info("login %s", uid)
        r = RedirectResponse("/", 303)
        r.set_cookie(COOKIE, sessions.issue(uid), httponly=True, secure=cookie_secure, samesite="lax",
                     max_age=12 * 3600)
        return r

    @app.post("/logout")
    async def logout():
        r = RedirectResponse("/login", 303)
        r.delete_cookie(COOKIE)
        return r

    # ---- Agent への通知 -------------------------------------------------------------------

    def notify_agent() -> None:
        """承認・却下をすぐ Agent に知らせる（待たない。届かなくても Agent は画面の問い合わせ時に確かめる）。"""
        if agent is None:
            return

        async def _send():
            try:
                await agent.notify()
            except Exception as e:
                log.warning("could not notify agent: %s", e)

        app.state.notify_tasks.add(t := asyncio.create_task(_send()))
        t.add_done_callback(app.state.notify_tasks.discard)

    # ---- 承認カードの説明 ---------------------------------------------------------------

    async def card_explain(a: dict) -> dict:
        """デバイスの request とマニフェスト・登録済みの部品・部品カタログから「どこに・何を」を作る（LLM の言葉は使わない）。"""
        if a.get("explain"):
            return a["explain"]
        req = a["request"]
        dev = await store.get_device(req["device_id"]) or {}
        try:
            return explain(req, dev.get("manifest") or {}, (dev.get("state") or {}).get("bindings") or [])
        except Exception:
            log.exception("could not explain approval %s", a.get("approval_id"))
            return {"where": [], "pins": [], "items": []}

    def card_html(a: dict, ex: dict) -> str:
        """承認カードの上の段（確認済みの内容）と下の段（エージェントの説明）。トップページ用。"""
        req = a["request"]
        rows = "".join(f"<div><b>場所</b> {esc(w)}</div>" for w in ex["where"])
        for it in ex["items"]:
            rows += f"<div><b>相手</b> {esc(it['target'])}</div><div><b>操作</b> {esc(it['what'])}" + \
                    (f"<br><span class=\"meaning\">{esc(it['meaning'])}</span>" if it.get("meaning") else "") + "</div>"
        purpose = a.get("purpose") or ""
        return f"""<div class="facts"><div class="hd">デバイスの要求とマニフェスト・部品カタログから</div>
<p class="summary">{esc(req['summary'])}</p>{rows}{pins_svg(ex['pins'])}</div>
<div class="claim"><div class="hd">エージェントの説明（確認されていません）</div>
<p>{esc(purpose) if purpose else '（説明なし）'}</p></div>"""

    # ---- 承認待ちの後始末 ---------------------------------------------------------------

    async def expire_old() -> None:
        cutoff = now_ms() - APPROVAL_TTL_MS
        for a in await store.list_approvals("pending"):
            if a["created_ms"] < cutoff:
                await store.decide_approval(a["approval_id"], {"status": "expired", "decided_ms": now_ms()})

    # ---- 画面 -------------------------------------------------------------------------

    warm = {"t": 0.0}

    def warm_agent() -> None:
        """Agent は最小0台なので、トップを開いた時点で裏で起こしておく（「エージェントと話す」を押す頃には温まっている）。"""
        if agent is None or time.time() - warm["t"] < 300:
            return
        warm["t"] = time.time()

        async def _ping():
            try:
                await agent.call("GET", "/health", "warmup", timeout=60)
            except Exception as e:
                log.info("agent warmup failed: %s", e)

        app.state.notify_tasks.add(t := asyncio.create_task(_ping()))
        t.add_done_callback(app.state.notify_tasks.discard)

    @app.get("/")
    async def index(request: Request, notice: str = ""):
        s = current(request)
        if s is None:
            return RedirectResponse("/login", 303)
        warm_agent()
        await expire_old()
        csrf = esc(s["csrf"])
        cards = []
        mine = await visible_device_ids(s)
        for a in await store.list_approvals("pending"):
            req = a["request"]
            if req["device_id"] not in mine:
                continue
            left = max(0, (a["created_ms"] + APPROVAL_TTL_MS - now_ms()) // 1000)
            cards.append(f"""<div class="card approval-card">{card_html(a, await card_explain(a))}
<div class="meta">デバイス <b>{esc(req['device_id'])}</b> ・ ルール <span class="chip">{esc(req['rule'])}</span>
 ・ 実行 {esc(a.get('run_id') or '—')} ・ 残り {left // 60}分{left % 60:02d}秒</div>
<details><summary>詳細（生の引数）</summary><pre>{esc(req['op'])} {esc(json.dumps(req['args'], ensure_ascii=False, indent=1))}</pre>
<div class="meta">args_sha256 {esc(req['args_sha256'][:16])}… ・ セッション {esc(req['session_id'])}</div></details>
<div class="row" style="margin-top:10px">
<form method="post" action="/approvals/{esc(a['approval_id'])}/approve"><input type="hidden" name="csrf" value="{csrf}">
<button class="ok" type="submit">承認する</button></form>
<form method="post" action="/approvals/{esc(a['approval_id'])}/deny"><input type="hidden" name="csrf" value="{csrf}">
<button type="submit">却下する</button></form></div></div>""")
        devs = []
        has_virtual = False
        for d in await store.list_devices():
            if d["device_id"] not in mine:
                continue
            on = d.get("online")
            if d.get("virtual"):
                has_virtual = True
            st = d.get("state") or {}
            devs.append(f"""<div class="card"><div class="row" style="justify-content:space-between">
<div><b>{esc(d['device_id'])}</b> <span class="{'on' if on else 'off'}">● {'オンライン' if on else 'オフライン'}</span>
<div class="meta">{esc((d.get('manifest') or {}).get('device', {}).get('name', ''))} ・ {
'あなた専用の仮想デバイス（残り ' + str(max(0, (d.get('expires_ms', 0) - now_ms()) // 60000)) + '分）' if d.get('virtual')
else '持ち主 ' + esc(d.get('owner', '—'))}
 ・ 非常停止 {'中' if st.get('estop') else 'なし'}</div></div>
<div class="row"><a class="chip" href="/logs/{esc(d['device_id'])}">記録</a><form method="post" action="/chat/{esc(d['device_id'])}"><input type="hidden" name="csrf" value="{csrf}">
<button class="ok" type="submit" data-busy="エージェントを準備しています…（初回は10秒ほど）" {'disabled' if not on or agent is None else ''}>エージェントと話す</button></form>
<form method="post" action="/devices/{esc(d['device_id'])}/estop"><input type="hidden" name="csrf" value="{csrf}">
<button class="stop" type="submit" {'disabled' if not on else ''}>非常停止</button></form>{f"""
<form method="post" action="/devices/{esc(d['device_id'])}/estop-clear"><input type="hidden" name="csrf" value="{csrf}">
<button type="submit" {'disabled' if not on else ''}>非常停止の解除を求める</button></form>""" if st.get('estop') else ''}{f"""
<form method="post" action="/devices/{esc(d['device_id'])}/delete" data-confirm="この仮想デバイスを削除しますか？（登録した部品や会話は消え、作り直すと初めからになります）">
<input type="hidden" name="csrf" value="{csrf}"><button type="submit" data-busy="削除しています…">削除</button></form>""" if d.get('virtual') else ''}</div></div></div>""")
        note = f'<div class="card">{esc(NOTICES[notice])}</div>' if notice in NOTICES else ""
        # はじめての人向けの案内（自分の仮想デバイスがまだないとき）。何をすればよいかを2手で示す
        guide = "" if has_virtual or virtual is None else f"""<h2>はじめかた</h2><div class="card guide">
<p>むげむげは、Raspberry Pi につないだ部品を AI エージェントが自分で調べ、計測・記録・グラフ化までする卓上の実験助手です。</p>
<ol><li><b>デバイスを用意する</b><div class="row" style="margin:6px 0 4px">
<form method="post" action="/devices/virtual"><input type="hidden" name="csrf" value="{csrf}">
<button class="ok" type="submit" data-busy="仮想デバイスを用意しています…">仮想デバイスで試す</button></form>
<span class="meta">Pi がなくても、温度センサ・リレー・カメラの模擬つきで1時間使えます（あなた専用）</span></div>
<div class="meta">自分の Pi をつなぐときは、下の「デバイスを追加」から。</div></li>
<li><b>デバイスの「エージェントと話す」を押す</b><div class="meta">最初の画面のボタンを押すだけで、部品の同定から始まります。文章を打つ必要はありません。</div></li>
<li><b>承認カードで許可する</b><div class="meta">部品の登録や書き込みは、あなたが承認したときだけデバイスが実行します。</div></li></ol></div>"""
        body = f"""<header><h1>むげむげ</h1><div class="row"><span class="meta">{esc(s['uid'])}</span>
<form method="post" action="/logout"><button type="submit">ログアウト</button></form></div></header>{note}
<h2>承認待ち（{len(cards)}）</h2>{''.join(cards) or '<p class="meta">ありません</p>'}
{guide}<h2>デバイス</h2>{''.join(devs) or '<p class="meta">まだありません</p>'}
<div class="row"><form method="post" action="/devices/pair-code"><input type="hidden" name="csrf" value="{csrf}">
<button type="submit">デバイスを追加（ペアリングコードを発行）</button></form></div>"""
        if has_virtual and virtual is not None:
            try:
                await virtual.list()          # 画面を開いている間は仮想デバイスのインスタンスを止めさせない
            except Exception as e:
                log.warning("virtual service is not reachable: %s", e)
        return page("むげむげ", body, refresh=True)

    @app.post("/approvals/{approval_id}/{action}")
    async def decide(approval_id: str, action: str, request: Request, csrf: str = Form("")):
        s = require_user(request)
        check_csrf(s, csrf)
        if action not in ("approve", "deny"):
            raise HTTPException(404)
        await expire_old()
        a = await store.get_approval(approval_id)
        if a is None or a["status"] != "pending":
            return RedirectResponse("/?notice=decided", 303)
        req = a["request"]
        if not visible(await store.get_device(req["device_id"]), s):
            raise HTTPException(403, "this approval belongs to another browser session's virtual device")
        if action == "deny":
            await store.decide_approval(approval_id, {"status": "denied", "decided_by": f"user:{s['uid']}",
                                                      "decided_ms": now_ms()})
            log.info("approval %s denied by %s", approval_id, s["uid"])
            notify_agent()
            return RedirectResponse("/", 303)
        # デバイスのセッションが変わっていたら署名しない（署名してもデバイスは wrong_session で拒否する: F7）
        dev = await store.get_device(req["device_id"])
        if not dev or dev.get("session_id") != req["session_id"] or not dev.get("online"):
            await store.decide_approval(approval_id, {"status": "expired", "reason": "session_ended",
                                                      "decided_ms": now_ms()})
            log.info("approval %s expired at approval time: session %s has ended", approval_id, req["session_id"])
            notify_agent()
            return RedirectResponse("/?notice=session_ended", 303)
        # 署名するのはデバイスが返した args そのもの。ハッシュを計算し直して一致を確かめる
        if args_sha256(req["op"], req["args"]) != req["args_sha256"]:
            log.error("approval %s: args_sha256 mismatch; refusing to sign", approval_id)
            raise HTTPException(409, "args_sha256 mismatch")
        grant = sign_grant(signing_key, kid=kid, device_id=req["device_id"], session_id=req["session_id"],
                           op=req["op"], args=req["args"], approved_by=f"user:{s['uid']}",
                           iat=now_ms() // 1000, ttl_s=GRANT_TTL_S)
        if not await store.decide_approval(approval_id, {"status": "approved", "grant": grant,
                                                         "decided_by": f"user:{s['uid']}", "decided_ms": now_ms()}):
            return RedirectResponse("/?notice=decided", 303)
        log.info("approval %s approved by %s: %s %s (grant %s)", approval_id, s["uid"], req["op"],
                 req["args_sha256"][:12], grant["grant_id"])
        if a.get("send_by") == "web":
            # 画面のボタンから求めた操作（非常停止の解除）。エージェントはいないので Web/API がグラント付きで送る
            ok = await send_with_grant(req, grant, s["uid"])
            return RedirectResponse("/?notice=" + ("estop_cleared" if ok else "estop_clear_failed"), 303)
        notify_agent()
        return RedirectResponse("/", 303)

    @app.post("/devices/pair-code")
    async def pair_code(request: Request, csrf: str = Form("")):
        s = require_user(request)
        check_csrf(s, csrf)
        for _ in range(10):
            code = f"{secrets.randbelow(1_000_000):06d}"
            if await store.create_pairing_code(code, f"user:{s['uid']}", now_ms() + PAIR_CODE_TTL_MS):
                break
        else:
            raise HTTPException(503, "could not allocate a pairing code")
        web = str(request.base_url).rstrip("/")
        install = "" if installer is None else f"""
<h2>A. まだデーモンを入れていない Pi</h2>
<p class="meta">Raspberry Pi OS で次の1行を実行する（デーモンの導入・マニフェストの作成・ペアリング・起動まで）:</p>
<pre>curl -fsSL {esc(web)}/install.sh | sudo bash -s -- {code}</pre>
<p class="meta"><b>この1行の末尾</b>に付けられるオプション（マニフェストを新しく作るときだけ効く）:</p>
<ul class="meta">
<li><code>--profile grove</code> … GPIO17 リレー・GPIO27 ボタンも許す（カメラの既定は USB）</li>
<li><code>--camera csi</code> … Pi Camera Module ／ <code>--camera usb</code> … USB カメラ</li>
</ul>
<p class="meta">中身を先に読むなら <a href="/install.sh">install.sh</a> を保存して <code>sudo bash install.sh {code}</code>。
承認の公開鍵 {esc(installer.kid)} <code>{esc(installer.public_key)}</code></p>"""
        body = f"""<header><h1>ペアリングコード</h1></header><div class="card">
<p class="summary" style="font-size:32px;letter-spacing:4px">{code}</p>
<p class="meta">10分間・1回だけ有効。</p>{install}
<h2>B. デーモンを入れ済みの Pi（ペアリングし直すだけ）</h2>
<pre>sudo /opt/mugemuge-agentd/.venv/bin/python -m mugemuge_agentd pair {code} --manifest /etc/mugemuge/manifest.yaml</pre>
<p class="meta">トークンを受け取り直すだけで、<b>オプションはない</b>（<code>--camera</code> などは付けられない）。
既に画面でオンラインなら不要。カメラやピンを足すときは、その Pi で <code>/etc/mugemuge/manifest.yaml</code> を直して
<code>sudo systemctl restart mugemuge-agentd</code>（マニフェストはリモートからは変えられない。書き方は README の「入れたあとでカメラを足す」）。</p>
<p><a href="/">戻る</a></p></div>"""
        return page("ペアリングコード | むげむげ", body)

    # ---- デバイスのインストーラ（ログイン不要。公開鍵とデーモンのソースだけを配る） ------------------

    @app.get("/install.sh")
    async def install_sh(request: Request):
        if installer is None:
            raise HTTPException(404, "GATEWAY_URL is not configured")
        return Response(installer.script(str(request.base_url)), media_type="text/x-shellscript; charset=utf-8")

    @app.get("/install/agentd.tar.gz")
    async def install_bundle():
        if installer is None:
            raise HTTPException(404, "GATEWAY_URL is not configured")
        return Response(installer.bundle, media_type="application/gzip")

    @app.post("/devices/virtual")
    async def create_virtual(request: Request, csrf: str = Form("")):
        s = require_user(request)
        check_csrf(s, csrf)
        if virtual is None:
            raise HTTPException(503, "virtual devices are not configured")
        owner = viewer(s)
        for d in await store.list_devices():          # 1つのブラウザのセッションに1台
            if d.get("virtual") and d.get("owner") == owner and d.get("expires_ms", 0) > now_ms():
                return RedirectResponse("/?notice=virtual_exists", 303)
        device_id = "virt-" + secrets.token_hex(4)
        code = f"{secrets.randbelow(1_000_000):06d}"
        if not await store.create_pairing_code(code, owner, now_ms() + 60_000):
            raise HTTPException(503, "could not allocate a pairing code")
        status, res = await virtual.create(device_id, code, f"{s['uid']} ({owner})")
        if status != 200:
            log.warning("virtual device creation failed: %s %s", status, res)
            return RedirectResponse("/?notice=" + ("virtual_full" if status == 429 else "virtual_fail"), 303)
        await store.update_device(device_id, {"virtual": True, "owner": owner,
                                              "expires_ms": now_ms() + virtual_ttl_s * 1000})
        log.info("virtual device %s created for %s", device_id, s["uid"])
        return RedirectResponse("/?notice=virtual_ok", 303)

    @app.post("/devices/{device_id}/delete")
    async def delete_virtual(device_id: str, request: Request, csrf: str = Form("")):
        """仮想デバイスを削除する（作ったブラウザのセッションだけ）。実機は画面からは消さない。"""
        s = require_user(request)
        check_csrf(s, csrf)
        dev = await store.get_device(device_id)
        if not dev or not dev.get("virtual") or not visible(dev, s):
            raise HTTPException(403, "only your own virtual device can be deleted")
        if virtual is not None:
            try:
                await virtual.delete(device_id)        # デーモンが出力を戻し bye を送って終わる
            except Exception as e:                     # 既に止まっていても一覧からは消す
                log.warning("could not stop virtual device %s: %s", device_id, e)
        await store.update_device(device_id, {"expires_ms": 0, "deleted_ms": now_ms()})
        for a in await store.list_approvals("pending"):
            if a["request"]["device_id"] == device_id:
                await store.decide_approval(a["approval_id"], {"status": "expired", "reason": "device_deleted",
                                                               "decided_ms": now_ms()})
        log.info("virtual device %s deleted by %s", device_id, s["uid"])
        return RedirectResponse("/?notice=virtual_deleted", 303)

    @app.post("/devices/{device_id}/estop")
    async def estop(device_id: str, request: Request, csrf: str = Form("")):
        s = require_user(request)
        check_csrf(s, csrf)
        if gateway is None:
            raise HTTPException(503, "gateway is not configured")
        if not visible(await store.get_device(device_id), s):
            raise HTTPException(403, "not your device")
        status, res = await gateway.exec({"device_id": device_id, "op": "sys.estop", "args": {},
                                          "run_id": f"ui:{s['uid']}", "step_id": "estop"})
        ok = status == 200 and res.get("ok")
        log.warning("estop on %s by %s: %s", device_id, s["uid"], "ok" if ok else res)
        return RedirectResponse("/?notice=" + ("estop_ok" if ok else "estop_fail"), 303)

    async def send_with_grant(req: dict, grant: dict, uid: str) -> bool:
        if gateway is None:
            return False
        status, res = await gateway.exec({"device_id": req["device_id"], "op": req["op"], "args": req["args"],
                                          "approval": grant, "run_id": f"ui:{uid}", "step_id": "estop_clear"})
        ok = status == 200 and bool(res.get("ok"))
        log.warning("%s on %s with grant %s by %s: %s", req["op"], req["device_id"], grant["grant_id"], uid,
                    "ok" if ok else res)
        return ok

    @app.post("/devices/{device_id}/estop-clear")
    async def estop_clear(device_id: str, request: Request, csrf: str = Form("")):
        """非常停止の解除を求める。デバイスが承認を求めたら、ふつうの承認カードを出す（解除は人の承認で）。"""
        s = require_user(request)
        check_csrf(s, csrf)
        if gateway is None:
            raise HTTPException(503, "gateway is not configured")
        if not visible(await store.get_device(device_id), s):
            raise HTTPException(403, "not your device")
        for a in await store.list_approvals("pending"):            # 同じ解除の承認待ちを2枚出さない
            if a["request"]["device_id"] == device_id and a["request"]["op"] == "sys.estop_clear":
                return RedirectResponse("/?notice=estop_clear_requested", 303)
        status, res = await gateway.exec({"device_id": device_id, "op": "sys.estop_clear", "args": {},
                                          "run_id": f"ui:{s['uid']}", "step_id": "estop_clear"})
        if status == 200 and res.get("ok"):                         # マニフェストが estop_clear: allow のとき
            log.warning("estop cleared on %s by %s (no approval required)", device_id, s["uid"])
            return RedirectResponse("/?notice=estop_cleared", 303)
        err = (res.get("error") or {}) if status == 200 else {}
        if err.get("code") != "approval_required":
            log.warning("estop clear on %s by %s failed: %s", device_id, s["uid"], res)
            return RedirectResponse("/?notice=estop_clear_failed", 303)
        req = err["request"]
        issued = await store.get_device_request(device_id, req["session_id"], req["args_sha256"])
        if issued is None:
            return RedirectResponse("/?notice=estop_clear_failed", 303)
        approval_id = "apr_" + secrets.token_hex(12)
        doc = {"status": "pending", "request": issued["request"], "run_id": f"ui:{s['uid']}",
               "requested_by": f"user:{s['uid']}", "created_ms": now_ms(), "send_by": "web",
               "purpose": f"画面の「解除を求める」ボタンから（{s['uid']}）"}
        doc["explain"] = await card_explain(doc)
        await store.create_approval(approval_id, doc)
        log.warning("estop clear on %s requested by %s: approval %s", device_id, s["uid"], approval_id)
        return RedirectResponse("/?notice=estop_clear_requested", 303)

    # ---- チャット（Agent への中継。チャットは始めたブラウザのセッションだけが読み書きできる） ----------

    async def my_chat(request: Request, chat_id: str) -> tuple[dict, dict]:
        s = require_user(request)
        chat = await store.get_chat(chat_id)
        if chat is None or chat["owner"] != viewer(s):
            raise HTTPException(404, "unknown chat")
        return s, chat

    def need_agent() -> AgentClient:
        if agent is None:
            raise HTTPException(503, "agent is not configured")
        return agent

    @app.post("/chat/{device_id}")
    async def start_chat(device_id: str, request: Request, csrf: str = Form("")):
        s = require_user(request)
        check_csrf(s, csrf)
        if not visible(await store.get_device(device_id), s):
            raise HTTPException(403, "not your device")
        status, res = await need_agent().call("POST", "/api/sessions", s["uid"], json_body={"device_id": device_id})
        if status != 200:
            raise HTTPException(502, f"agent: {res.get('detail', status)}")
        chat_id = res["session_id"]
        await store.put_chat(chat_id, {"owner": viewer(s), "uid": s["uid"], "device_id": device_id,
                                       "created_ms": now_ms()})
        return RedirectResponse(f"/chat/{device_id}/{chat_id}", 303)

    @app.get("/chat/{device_id}/{chat_id}")
    async def chat_page_view(device_id: str, chat_id: str, request: Request):
        s, chat = await my_chat(request, chat_id)
        if chat["device_id"] != device_id:
            raise HTTPException(404, "unknown chat")
        dev = await store.get_device(device_id) or {}
        name = (dev.get("manifest") or {}).get("device", {}).get("name", device_id)
        return page(f"{name} | むげむげ", chat_page(device_id=device_id, chat_id=chat_id, device_name=name,
                                                   virtual=bool(dev.get("virtual")), csrf=s["csrf"], uid=s["uid"],
                                                   trace_project=os.environ.get("GOOGLE_CLOUD_PROJECT", "")))

    @app.post("/ui/chat/{chat_id}/messages")
    async def ui_post_message(chat_id: str, request: Request):
        s, chat = await my_chat(request, chat_id)
        check_csrf(s, request.headers.get("x-csrf", ""))
        text = str((await request.json()).get("text", "")).strip()[:2000]
        if not text:
            raise HTTPException(400, "text is required")
        status, res = await need_agent().call("POST", f"/api/sessions/{chat_id}/messages", s["uid"],
                                              json_body={"text": text}, timeout=900)
        return JSONResponse(res, status)

    keepalive = {"t": 0.0}

    async def keep_virtual_alive(device_id: str) -> None:
        """チャット中は仮想デバイスのサービスに数分おきに問い合わせ、Cloud Run にインスタンスを止めさせない。"""
        if virtual is None or time.time() - keepalive["t"] < 240:
            return
        dev = await store.get_device(device_id)
        if dev and dev.get("virtual"):
            keepalive["t"] = time.time()
            try:
                await virtual.list()
            except Exception as e:
                log.warning("virtual keepalive failed: %s", e)

    @app.get("/ui/chat/{chat_id}/events")
    async def ui_events(chat_id: str, request: Request, after: int = 0):
        s, chat = await my_chat(request, chat_id)
        await keep_virtual_alive(chat["device_id"])
        status, res = await need_agent().call("GET", f"/api/sessions/{chat_id}/events", s["uid"],
                                              params={"after": after}, timeout=60)
        return JSONResponse(res, status)

    @app.get("/ui/chat/{chat_id}/series/{job_id}")
    async def ui_series(chat_id: str, job_id: str, request: Request):
        s, chat = await my_chat(request, chat_id)
        status, res = await need_agent().call("GET", f"/api/sessions/{chat_id}/series/{job_id}", s["uid"])
        return JSONResponse(res, status)

    @app.get("/ui/chat/{chat_id}/panel")
    async def ui_panel(chat_id: str, request: Request):
        s, chat = await my_chat(request, chat_id)
        status, res = await need_agent().call("GET", f"/api/sessions/{chat_id}/panel", s["uid"])
        return JSONResponse(res, status)

    @app.get("/ui/chat/{chat_id}/jobs/{job_id}")
    async def ui_job(chat_id: str, job_id: str, request: Request):
        s, chat = await my_chat(request, chat_id)
        await keep_virtual_alive(chat["device_id"])
        status, res = await need_agent().call("GET", f"/api/sessions/{chat_id}/jobs/{job_id}", s["uid"])
        return JSONResponse(res, status)

    tester = Tester(gateway) if gateway is not None else None

    @app.get("/ui/devices/{device_id}/state")
    async def ui_device_state(device_id: str, request: Request):
        """「現在の状態」: マニフェストのピンとバス、Gateway が追っている出力・バインド・ジョブ・スキャン結果。"""
        s = require_user(request)
        d = await store.get_device(device_id)
        if not visible(d, s):
            raise HTTPException(404, "unknown device")
        m = d.get("manifest") or {}
        return {"device_id": device_id, "name": m.get("device", {}).get("name", device_id),
                "board": m.get("device", {}).get("board"), "virtual": bool(d.get("virtual")),
                "online": bool(d.get("online")), "state": d.get("state") or {}, "i2c_seen": d.get("i2c_seen") or {},
                "gpio_lines": m.get("gpio", {}).get("lines", []),
                "i2c_buses": [{"bus": b["bus"], "label": b.get("label")} for b in m.get("i2c", {}).get("buses", [])],
                "state_ts": d.get("state_ts"), "parts": {name: {"kind": p.kind, "channels": list(p.channels)}
                                                         for name, p in PARTS.items()}}

    @app.get("/ui/devices/{device_id}/tester")
    async def ui_device_tester(device_id: str, request: Request):
        """テスター表示: 同定済みの部品をその場で読んで換算する（自動で許可される読み出しだけ）。"""
        s = require_user(request)
        d = await store.get_device(device_id)
        if not visible(d, s):
            raise HTTPException(404, "unknown device")
        if tester is None or not d.get("online"):
            raise HTTPException(503, "device is offline")
        await keep_virtual_alive(device_id)
        return {"ts": now_ms(), **await tester.read_all(d, s["uid"])}

    @app.post("/ui/devices/{device_id}/part-params")
    async def ui_part_params(device_id: str, request: Request):
        """部品の換算の設定を保存する（今は INA226 のシャント抵抗だけ）。クラウドでの換算にだけ使い、デバイスには送らない。"""
        s = require_user(request)
        check_csrf(s, request.headers.get("x-csrf", ""))
        d = await store.get_device(device_id)
        if not visible(d, s):
            raise HTTPException(404, "unknown device")
        body = await request.json()
        try:
            bus, addr, ohm = int(body["bus"]), int(body["addr"]), float(body["shunt_ohm"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "bus, addr, shunt_ohm are required") from None
        if not 1e-4 <= ohm <= 100:
            raise HTTPException(400, "shunt_ohm must be between 0.0001 and 100")
        bound = {(b["bus"], b["addr"]): b["part"] for b in (d.get("state") or {}).get("bindings", [])}
        if bound.get((bus, addr)) != "INA226":
            raise HTTPException(400, "not an INA226 binding")
        pp = dict(d.get("part_params") or {})
        pp[f"{bus}_{addr}"] = {"shunt_ohm": ohm}
        await store.update_device(device_id, {"part_params": pp})
        return {"ok": True, "part_params": pp}

    # ---- 記録（長期の記録: 時刻・値・写真） --------------------------------------------------

    async def visible_device(request: Request, device_id: str) -> tuple[dict, dict]:
        s = require_user(request)
        d = await store.get_device(device_id)
        if not visible(d, s):
            raise HTTPException(404, "unknown device")
        return s, d

    def latest_values(lg: dict) -> dict:
        out = {}
        last, decs = lg.get("last_row"), lg.get("decoders") or {}
        if not last:
            return out
        for i, c in enumerate(lg.get("columns", ["t_ms"])[1:]):
            d, v = decs.get(c), last["v"][i] if i < len(last["v"]) else None
            if not d or v is None:
                continue
            try:
                for k, val in PARTS_DECODE(d["decoder"], v, d.get("calibration_hex", "")).items():
                    label, unit = LOG_CHANNELS.get(k, (k, ""))
                    out[f"{label}（{c}）"] = f"{val} {unit}"
            except Exception:
                pass
        return out

    @app.get("/logs/{device_id}")
    async def logs_page(device_id: str, request: Request):
        s, d = await visible_device(request, device_id)
        logs = await store.list_logs(device_id)
        name = (d.get("manifest") or {}).get("device", {}).get("name", device_id)
        return page(f"記録 | {name}", list_page(device_id, name, logs, {lg["log_id"]: latest_values(lg) for lg in logs},
                                                s["csrf"]), refresh=True)

    @app.get("/logs/{device_id}/{log_id}")
    async def log_detail_page(device_id: str, log_id: str, request: Request):
        s, d = await visible_device(request, device_id)
        lg = await store.get_log(device_id, log_id)
        if lg is None:
            raise HTTPException(404, "unknown log")
        return page(f"{lg.get('name', log_id)} | 記録", detail_page(device_id=device_id, log=lg, csrf=s["csrf"]))

    @app.get("/ui/devices/{device_id}/logs")
    async def ui_logs(device_id: str, request: Request):
        await visible_device(request, device_id)
        return {"logs": [{k: lg.get(k) for k in ("log_id", "name", "status", "rows", "photos", "started_ts", "ended_ts",
                                                   "last_photo")} for lg in await store.list_logs(device_id)]}

    @app.get("/ui/devices/{device_id}/logs/{log_id}/data")
    async def ui_log_data(device_id: str, log_id: str, request: Request, span: int = 0, max_points: int = 600):
        await visible_device(request, device_id)
        lg = await store.get_log(device_id, log_id)
        if lg is None:
            raise HTTPException(404, "unknown log")
        batches = await store.list_log_batches(device_id, log_id)
        full = series_for_view(lg, batches, None, None, 1)
        hi = full["extent"][1]
        lo = hi - span if span > 0 else None
        return series_for_view(lg, batches, lo, hi, max(50, min(2000, max_points)))

    @app.get("/ui/devices/{device_id}/logs/{log_id}.csv")
    async def ui_log_csv(device_id: str, log_id: str, request: Request):
        await visible_device(request, device_id)
        lg = await store.get_log(device_id, log_id)
        if lg is None:
            raise HTTPException(404, "unknown log")
        base = str(request.base_url).rstrip("/")
        text = to_csv(lg, await store.list_log_batches(device_id, log_id),
                      lambda bid: f"{base}/ui/devices/{device_id}/blobs/{bid}")
        fname = f"{device_id}-{log_id}.csv"
        return Response("\ufeff" + text, media_type="text/csv; charset=utf-8",          # Excel 向けに BOM を付ける
                        headers={"Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})

    @app.get("/ui/devices/{device_id}/blobs/{blob_id}")
    async def ui_blob(device_id: str, blob_id: str, request: Request):
        """写真の中継（バケットは公開しない）。見られるデバイスの写真だけ。"""
        await visible_device(request, device_id)
        meta = await store.get_blob_meta(device_id, blob_id)
        data = await blobs.get(meta["path"]) if meta else None
        if data is None:
            raise HTTPException(404, "no such photo (it may have expired after 90 days)")
        return Response(data, media_type=meta["content_type"],
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})

    @app.post("/ui/devices/{device_id}/logs/{log_id}/stop")
    async def ui_log_stop(device_id: str, log_id: str, request: Request, csrf: str = Form("")):
        s, d = await visible_device(request, device_id)
        check_csrf(s, csrf)
        if gateway is None:
            raise HTTPException(503, "gateway is not configured")
        status, res = await gateway.exec({"device_id": device_id, "op": "log.stop", "args": {"log_id": log_id},
                                          "run_id": f"ui:{s['uid']}", "step_id": "log-stop"})
        log.info("log %s stop by %s: %s", log_id, s["uid"], res)
        return RedirectResponse(f"/logs/{device_id}/{log_id}", 303)

    @app.get("/ui/approvals/{approval_id}")
    async def ui_approval(approval_id: str, request: Request):
        """チャットの中に出す承認カードの中身（デバイスが返した request）。"""
        s = require_user(request)
        await expire_old()
        a = await store.get_approval(approval_id)
        if a is None or not visible(await store.get_device(a["request"]["device_id"]), s):
            raise HTTPException(404, "unknown approval")
        ex = await card_explain(a)
        return {"approval_id": approval_id, "status": a["status"], "request": a["request"],
                "decided_by": a.get("decided_by"), "reason": a.get("reason"),
                "expires_ms": a["created_ms"] + APPROVAL_TTL_MS, "explain": ex,
                "pins_svg": pins_svg(ex["pins"]), "purpose": a.get("purpose") or ""}

    # ---- Agent 向け API ----------------------------------------------------------------

    @app.post("/api/approvals")
    async def api_create_approval(request: Request, caller: str = Depends(caller_auth)):
        try:
            body = json.loads(await request.body())
            req = {k: body[k] for k in REQUEST_FIELDS}
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, f"body must contain {', '.join(REQUEST_FIELDS)}") from None
        # デバイス自身が返した request でなければ受け付けない（Agent が summary を偽れないように）
        issued = await store.get_device_request(req["device_id"], req["session_id"], req["args_sha256"])
        if issued is None:
            raise HTTPException(400, "this request was not issued by the device")
        dev = await store.get_device(req["device_id"])
        if not dev or dev.get("session_id") != req["session_id"]:
            raise HTTPException(409, "the device session has changed; ask the device again")
        approval_id = "apr_" + secrets.token_hex(12)
        # purpose はエージェント（LLM）の言葉。カードには「確認されていない説明」として request とは分けて出す
        purpose = " ".join(str(body.get("purpose") or "").split())[:200]
        doc = {"status": "pending", "request": issued["request"], "run_id": str(body.get("run_id") or "")[:64],
               "requested_by": caller, "created_ms": now_ms(), "purpose": purpose}
        # 「どこに・何を」は求められた時点の状態で作って残す（あとで見ても、登録済みかどうかが変わらない）
        doc["explain"] = await card_explain(doc)
        await store.create_approval(approval_id, doc)
        log.info("approval %s requested by %s: %s", approval_id, caller, issued["request"]["summary"])
        return {"approval_id": approval_id, "status": "pending"}

    @app.get("/api/approvals/{approval_id}")
    async def api_get_approval(approval_id: str, caller: str = Depends(caller_auth)):
        await expire_old()
        a = await store.get_approval(approval_id)
        if a is None:
            raise HTTPException(404, "unknown approval")
        out = {"approval_id": approval_id, "status": a["status"]}
        if a.get("reason"):
            out["reason"] = a["reason"]
        if a["status"] == "approved":
            out["grant"] = a["grant"]
        return out

    @app.get("/health")
    async def health():
        return JSONResponse({"ok": True})

    return app


def app_from_env() -> FastAPI:
    """uvicorn --factory から呼ぶ。足りない設定があれば起動を止める。"""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    insecure = dev_insecure()
    key = serialization.load_pem_private_key(os.environ["APPROVAL_SIGNING_KEY"].encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("APPROVAL_SIGNING_KEY must be an Ed25519 private key")
    gw_url = os.environ.get("GATEWAY_URL")
    agent_url = os.environ.get("AGENT_URL")
    virtual_url = os.environ.get("VIRTUAL_URL")
    return create_app(
        users=json.loads(os.environ["MUGEMUGE_USERS"]),
        sessions=Sessions(os.environ["SESSION_SECRET"].encode()),
        signing_key=key,
        kid=os.environ["APPROVAL_KID"],
        gateway=GatewayClient(gw_url, use_id_token=not insecure) if gw_url else None,
        cookie_secure=not insecure,
        agent=AgentClient(agent_url, use_id_token=not insecure) if agent_url else None,
        virtual=VirtualClient(virtual_url, use_id_token=not insecure) if virtual_url else None,
        virtual_ttl_s=int(os.environ.get("VIRTUAL_TTL_S", "3600")),
    )
