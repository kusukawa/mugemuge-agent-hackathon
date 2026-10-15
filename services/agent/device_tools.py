"""エージェントのツール（docs/AGENT.md）。

- op ごとのツール: 引数を宣言的な exec にして Gateway に送る。デバイスが最終判断者（PROTOCOL.md 0章の1）
- 事前チェック（before_tool_callback）: policy.py をクラウドでも走らせ、拒否が分かっている操作は送らない。写しにすぎない
- 承認: デバイスが approval_required を返したら、その request を Web/API に登録して approval_id を返す。
  エージェントは wait_for_approval（長時間実行ツール）を呼んで止まり、承認されたら Agent サービスが
  グラント付きで送り直した結果を差し込んで再開させる（app.py の ApprovalWatcher）
- このサービスは署名鍵を持たない。グラントを作れるのは Web/API だけ（PROTOCOL.md 0章の4）
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from mugemuge_agentd.canonical import args_sha256
from mugemuge_agentd.manifest import ManifestError, parse_manifest
from mugemuge_agentd.messages import new_id
from mugemuge_agentd.policy import BadRequest, Binding, DeviceState, Level, Policy

from ..common.tracing import traceparent_of_current
from . import parts as parts_mod

log = logging.getLogger("mugemuge.agent.tools")

HeadersFor = Callable[[str], Awaitable[dict]]


async def no_auth(_audience: str) -> dict:
    return {}


class CloudClient:
    """Gateway と Web/API の内部 API。ID トークンは headers_for(audience) が用意する。"""

    def __init__(self, gateway_url: str, web_url: str, headers_for: HeadersFor = no_auth):
        self.gateway_url = gateway_url.rstrip("/")
        self.web_url = web_url.rstrip("/")
        self.headers_for = headers_for
        self.http = httpx.AsyncClient(timeout=40)

    async def _get(self, base: str, path: str) -> httpx.Response:
        return await self.http.get(base + path, headers=await self.headers_for(base))

    async def _post(self, base: str, path: str, body: dict) -> httpx.Response:
        return await self.http.post(base + path, json=body, headers=await self.headers_for(base))

    async def exec(self, body: dict) -> tuple[int, dict]:
        r = await self._post(self.gateway_url, "/internal/exec", body)
        return r.status_code, r.json()

    async def device(self, device_id: str) -> dict:
        r = await self._get(self.gateway_url, f"/internal/devices/{device_id}")
        r.raise_for_status()
        return r.json()

    async def samples(self, device_id: str, job_id: str) -> dict:
        r = await self._get(self.gateway_url, f"/internal/devices/{device_id}/samples/{job_id}")
        r.raise_for_status()
        return r.json()

    async def events(self, device_id: str, limit: int = 50) -> list[dict]:
        r = await self._get(self.gateway_url, f"/internal/devices/{device_id}/events?limit={limit}")
        r.raise_for_status()
        return r.json()["events"]

    async def logs(self, device_id: str) -> list[dict]:
        r = await self._get(self.gateway_url, f"/internal/devices/{device_id}/logs")
        r.raise_for_status()
        return r.json()["logs"]

    async def put_log_decoders(self, device_id: str, log_id: str, decoders: dict) -> None:
        r = await self._post(self.gateway_url, f"/internal/devices/{device_id}/logs/{log_id}/decoders",
                             {"decoders": decoders})
        r.raise_for_status()

    async def create_approval(self, request: dict, run_id: str, purpose: str = "") -> dict:
        # purpose はエージェントの言葉。承認カードには「確認されていない説明」として別の欄に出る
        r = await self._post(self.web_url, "/api/approvals", {**request, "run_id": run_id, "purpose": purpose})
        if r.status_code != 200:
            raise RuntimeError(f"web {r.status_code}: {r.text[:200]}")
        return r.json()

    async def get_approval(self, approval_id: str) -> dict:
        r = await self._get(self.web_url, f"/api/approvals/{approval_id}")
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        await self.http.aclose()


def rejected_message(reason: str) -> str:
    return (f"デバイスが承認を受け付けませんでした（理由: {reason}）。"
            "デバイスの時計か設定の問題の可能性があります")


@dataclass
class PendingApproval:
    approval_id: str
    device_id: str
    op: str
    args: dict
    summary: str
    adk_session_id: str
    user_id: str
    run_id: str
    created: float = field(default_factory=time.time)
    call_ids: list[str] = field(default_factory=list)   # wait_for_approval の function_call_id（重ねて呼ばれたら全部）


class Pending:
    """承認待ちの台帳（プロセス内。Agent サービスは max-instances=1）。"""

    def __init__(self) -> None:
        self.items: dict[str, PendingApproval] = {}

    def add(self, p: PendingApproval) -> None:
        self.items[p.approval_id] = p

    def pop(self, approval_id: str) -> PendingApproval | None:
        return self.items.pop(approval_id, None)


# ---- ツール名 → op と引数 ---------------------------------------------------------------

def _i(x: Any) -> int:
    """モデルが 2.0 のような値を渡しても、整数として送る（Gateway は小数表記を拒否する）。"""
    if isinstance(x, bool):
        raise BadRequest("boolean is not an integer")
    if isinstance(x, float) and not x.is_integer():
        raise BadRequest(f"{x} is not an integer")
    return int(x)


def _reg_args(bus, addr, reg, reg_width) -> dict:
    a = {"bus": _i(bus), "addr": _i(addr)}
    if reg is not None:
        a["reg"] = _i(reg)
    if reg_width is not None and _i(reg_width) != 8:
        a["reg_width"] = _i(reg_width)
    return a


def op_and_args(tool: str, a: dict) -> tuple[str, dict]:
    """ツールの引数から PROTOCOL.md 5章の op と args を作る。事前チェックと実行の両方が使う。"""
    if tool == "device_info":
        return "sys.info", {}
    if tool == "emergency_stop":
        return "sys.estop", {}
    if tool == "i2c_scan":
        return "i2c.scan", {"bus": _i(a.get("bus", 1))}
    if tool == "i2c_read":
        return "i2c.read", {**_reg_args(a["bus"], a["addr"], a.get("reg"), a.get("reg_width")), "len": _i(a["length"])}
    if tool == "i2c_write":
        return "i2c.write", {**_reg_args(a["bus"], a["addr"], a["reg"], a.get("reg_width")),
                             "hex": str(a["hex"]).lower().removeprefix("0x")}
    if tool == "i2c_bind":
        return "i2c.bind", {"bus": _i(a["bus"]), "addr": _i(a["addr"]), "part": str(a["part"]),
                            "fingerprint": {"reg": _i(a["fingerprint_reg"]), "len": _i(a["fingerprint_len"]),
                                            "hex": str(a["fingerprint_hex"]).lower(),
                                            **({"reg_width": _i(a["fingerprint_reg_width"])}
                                               if a.get("fingerprint_reg_width") in (8, 16, 8.0, 16.0) else {}),
                                            **({"recheck": False} if a.get("fingerprint_recheck") is False else {})},
                            "write_allow_regs": sorted({_i(r) for r in a.get("write_allow_regs") or []})}
    if tool == "i2c_unbind":
        return "i2c.unbind", {"bus": _i(a["bus"]), "addr": _i(a["addr"])}
    if tool == "display_draw":
        items = []
        for it in a["items"]:
            item = {"text": str(it["text"])[:32]}
            for k in ("x", "y", "size"):
                if it.get(k) is not None:
                    item[k] = _i(it[k])
            items.append(item)
        args = {"bus": _i(a["bus"]), "addr": _i(a["addr"]), "items": items}
        if a.get("clear") is False:
            args["clear"] = False
        return "display.draw", args
    if tool == "gpio_read":
        return "gpio.read", {"line": _i(a["line"])}
    if tool == "gpio_write":
        args = {"line": _i(a["line"]), "value": _i(a["value"])}
        if a.get("hold_ms") is not None:
            args["hold_ms"] = _i(a["hold_ms"])
        return "gpio.write", args
    if tool == "sample_start":
        reads = []
        for r in a["reads"]:
            rr = {"name": str(r["name"]), **_reg_args(r["bus"], r["addr"], r.get("reg"), r.get("reg_width")),
                  "len": _i(r["length"] if "length" in r else r["len"])}
            reads.append(rr)
        args = {"period_ms": _i(a["period_ms"]), "duration_ms": _i(a["duration_ms"]), "reads": reads}
        if a.get("batch_ms") is not None:
            args["batch_ms"] = _i(a["batch_ms"])
        return "sample.start", args
    if tool == "sample_stop":
        return "sample.stop", {"job_id": str(a["job_id"])}
    if tool == "camera_capture":
        args = {"camera": str(a.get("camera") or "cam0")}
        for k in ("width", "height"):
            if a.get(k):
                args[k] = _i(a[k])
        return "camera.capture", args
    if tool == "log_start":
        reads = [{"name": str(r["name"]), **_reg_args(r["bus"], r["addr"], r.get("reg"), r.get("reg_width")),
                  "len": _i(r["length"] if "length" in r else r["len"])} for r in a.get("reads") or []]
        args = {"name": str(a["name"])[:64], "interval_ms": _i(round(float(a["interval_s"]) * 1000)),
                "duration_ms": _i(round(float(a["duration_min"]) * 60_000)), "reads": reads}
        if a.get("photo_camera"):
            args["photo"] = {"camera": str(a["photo_camera"]),
                             "interval_ms": _i(round(float(a.get("photo_interval_s") or 300) * 1000))}
        return "log.start", args
    if tool == "log_stop":
        return "log.stop", {"log_id": str(a["log_id"])}
    raise KeyError(tool)


DEVICE_TOOLS = {"device_info", "emergency_stop", "i2c_scan", "i2c_read", "i2c_write", "i2c_bind", "i2c_unbind",
                "display_draw", "gpio_read", "gpio_write", "sample_start", "sample_stop", "camera_capture", "log_start", "log_stop"}


# ---- デバイスごとの状態と道具箱 -------------------------------------------------------------

class DeviceToolkit:
    """1台のデバイス用のツール一式。マニフェストはデバイスが hello で申告したもの（Gateway 経由）。"""

    def __init__(self, device_id: str, info: dict, cloud: CloudClient, pending: Pending):
        self.device_id = device_id
        self.cloud = cloud
        self.pending = pending
        try:
            self.manifest = parse_manifest(info["manifest"])
        except (ManifestError, KeyError) as e:
            raise ValueError(f"device {device_id} has no valid manifest: {e}") from None
        self.policy = Policy(self.manifest)
        self.bindings: dict[tuple[int, int], Binding] = {}
        for b in (info.get("state") or {}).get("bindings", []):
            self.bindings[(b["bus"], b["addr"])] = Binding(**b)
        self.jobs: dict[str, list[str]] = {}          # job_id → 列名（t_ms を除く）
        self.job_reads: dict[str, list[dict]] = {}    # job_id → reads（列ごとの bus・addr）
        self.job_meta: dict[str, dict] = {}           # job_id → period_ms・duration_ms・started_ts
        self.calibration: dict[tuple[int, int], str] = {}   # バインド済みの部品から読んだ校正値
        self.series: dict[str, dict[str, dict]] = {}  # job_id → 列名 → 換算済みの系列（UI のグラフ用）
        self.shunt_ohm: dict[tuple[int, int], float] = {}  # INA226 のシャント抵抗（エージェントが get_samples で使った値）
        self.precheck_blocked = 0
        self._approval_lock = asyncio.Lock()
        # 承認つきで送り直したのにデバイスが受け付けなかった操作（会話・op・args ごと）。
        # wrong_session（つなぎ直しの直後）だけ1回求め直してよい。それ以外は何度求めても通らないので求め直さない
        self.rejected: dict[tuple[str, str, str], dict] = {}

    # ---- 事前チェック（写し） -----------------------------------------------------------

    def precheck(self, tool: str, args: dict, *, count: bool = True) -> dict | None:
        """拒否されると分かっている操作なら、送らずに返す dict。通すなら None。count=False なら数えない（判定し直す前の1回目）。"""
        if tool not in DEVICE_TOOLS:
            return None
        try:
            op, a = op_and_args(tool, args)
            d = self.policy.evaluate(op, a, DeviceState(self.device_id, bindings=dict(self.bindings)),
                                     int(time.time() * 1000))
        except (BadRequest, KeyError, TypeError, ValueError) as e:
            return {"status": "error", "code": "bad_request", "message": f"invalid arguments: {e}"}
        if d.level is Level.DENY:
            if count:
                self.precheck_blocked += 1
            log.info("cloud precheck blocked %s %s: %s", op, a, d.reason or d.rule)
            return {"status": "denied", "by": "cloud_precheck", "rule": d.rule, "reason": d.reason,
                    "message": "マニフェストで拒否される操作なのでデバイスに送らなかった。承認でも覆らない"}
        return None

    # ---- 実行 ---------------------------------------------------------------------------

    async def run_op(self, tool: str, args: dict, *, adk_session_id: str, user_id: str, run_id: str,
                     step_id: str, purpose: str = "") -> dict:
        try:
            op, a = op_and_args(tool, args)
        except (BadRequest, KeyError, TypeError, ValueError) as e:
            return {"status": "error", "code": "bad_request", "message": f"invalid arguments: {e}"}
        status, r = await self.send(op, a, run_id=run_id, step_id=step_id)
        if status != 200:
            return {"status": "error", "code": r.get("error", f"http_{status}"),
                    "message": "デバイスに届かなかった（オフラインか時間切れ）。少し待ってから同じ操作をやり直してよい"}
        if not r["ok"] and r["error"]["code"] in ("approval_required", "approval_invalid"):
            stop = self._check_rejected(adk_session_id, op, a)
            if stop is not None:
                return stop
            req = r["error"]["request"]
            # モデルが同じ操作を並列に呼んでも、承認待ちは1つにまとめる（人に同じカードを2回見せない）。
            # 確かめてから登録するまでを1つのロックで囲む（並列の呼び出しが同時に「まだない」と判断しないように）
            async with self._approval_lock:
                for p in self.pending.items.values():
                    if (p.adk_session_id, p.device_id, p.op, p.args) == (adk_session_id, self.device_id, op, a):
                        return {"status": "approval_required", "approval_id": p.approval_id, "summary": p.summary,
                                "next": "同じ操作がすでに承認待ち。wait_for_approval(approval_id) で待つこと"}
                try:
                    created = await self.cloud.create_approval(req, run_id, purpose)
                except Exception as e:
                    return {"status": "error", "code": "approval_unavailable", "message": str(e)[:200]}
                self.pending.add(PendingApproval(created["approval_id"], self.device_id, op, a, req["summary"],
                                                 adk_session_id, user_id, run_id))
            log.info("approval %s requested for %s %s", created["approval_id"], op, req["args_sha256"][:12])
            return {"status": "approval_required", "approval_id": created["approval_id"], "summary": req["summary"],
                    "rule": req["rule"], **({"trace_id": r["traceparent"].split("-")[1]} if r.get("traceparent") else {}),
                    "next": "人の承認が要る。wait_for_approval(approval_id) を呼んで待つこと。自分で同じ操作を再送しない"}
        return self.digest(op, a, r)

    def approval_rejected(self, adk_session_id: str, op: str, args: dict, r: dict) -> dict:
        """承認つきの送り直しをデバイスが approval_invalid で拒否したとき、エージェントに返す結果。理由を覚えておく。"""
        reason = (r["error"].get("detail") or {}).get("reason", "unknown")
        key = (adk_session_id, op, args_sha256(op, args))
        prev = self.rejected.get(key)
        self.rejected[key] = {"reason": reason, "retried": bool(prev and prev["retried"])}
        log.warning("device rejected the approved %s: %s", op, reason)
        if reason == "wrong_session" and not (prev and prev["retried"]):
            return {"status": "error", "code": "approval_invalid", "reason": reason,
                    "message": "承認のあとでデバイスとの接続がつなぎ直されたため、承認が使えなかった。"
                               "同じ操作をもう一度1回だけ求めてよい（新しい承認カードが出る）"}
        return {"status": "error", "code": "approval_invalid", "reason": reason,
                "message": rejected_message(reason),
                "next": "同じ操作の承認をもう一度求めない。この message をそのまま人に伝えて止まる"}

    def _check_rejected(self, adk_session_id: str, op: str, args: dict) -> dict | None:
        """前に承認を受け付けなかった操作を、また求めようとしていれば止める dict。求めてよければ None。"""
        rec = self.rejected.get((adk_session_id, op, args_sha256(op, args)))
        if rec is None:
            return None
        if rec["reason"] == "wrong_session" and not rec["retried"]:
            rec["retried"] = True
            return None
        return {"status": "error", "code": "approval_invalid", "reason": rec["reason"],
                "message": rejected_message(rec["reason"]),
                "next": "承認カードは出していない。同じ操作の承認をもう一度求めない。この message をそのまま人に伝えて止まる"}

    async def send(self, op: str, args: dict, *, run_id: str, step_id: str, approval: dict | None = None):
        body = {"device_id": self.device_id, "op": op, "args": args, "run_id": run_id[:64], "step_id": step_id[:64]}
        tp = traceparent_of_current()
        if tp:
            body["traceparent"] = tp
        if approval is not None:
            body["approval"] = approval
        # exec.id はここで決め、device_offline のときは同じ id で1回だけ送り直す（Gateway の世代交代の窓: F3）。
        # 前の exec がデバイスに届いて実行されていても、デバイスは同じ id に前の結果を返す（二重実行しない）
        body["id"] = new_id("x-")
        st, r = await self.cloud.exec(body)
        if st == 503 and r.get("error") == "device_offline":
            st, r = await self.cloud.exec(body)
        return st, r

    def digest(self, op: str, args: dict, r: dict) -> dict:
        """デバイスの result をエージェント向けにまとめ、バインドの写しなどを更新する。"""
        pol = r.get("policy", {})
        trace = {}
        if r.get("traceparent"):
            trace["trace_id"] = r["traceparent"].split("-")[1]       # UI から Cloud Trace へのリンクに使う
        if r.get("duration_ms") is not None:
            trace["device_ms"] = r["duration_ms"]
        if r["ok"]:
            data = r["data"]
            if op == "i2c.bind":
                self.bindings[(args["bus"], args["addr"])] = Binding(**args)
            elif op == "i2c.unbind":
                self.bindings.pop((args["bus"], args["addr"]), None)
            elif op == "sample.start":
                self.jobs[data["job_id"]] = data["columns"][1:]
                self.job_reads[data["job_id"]] = args["reads"]
                self.job_meta[data["job_id"]] = {"period_ms": args["period_ms"], "duration_ms": args["duration_ms"],
                                                 "started_ts": data["started_ts"]}
            elif op == "i2c.read":
                self._remember_calibration(args, data["hex"])
            elif op == "log.start":
                # 画面（記録ページ）が換算できるよう、列ごとの換算方法を登録しておく（承認後に始まった場合も同じ）
                decs, missing = self.log_decoders(args)
                self._bg = asyncio.ensure_future(self.cloud.put_log_decoders(self.device_id, data["log_id"], decs))
                data = {**data, "decodable": sorted(decs), "not_decodable": missing}
            return {"status": "ok", "decision": pol.get("decision"), "rule": pol.get("rule"), "data": data, **trace}
        e = r["error"]
        if e["code"] == "fingerprint_mismatch":
            self.bindings.pop((args.get("bus"), args.get("addr")), None)
        out = {"status": "error", "code": e["code"], "message": e["message"], "decision": pol.get("decision"),
               "rule": pol.get("rule"), **trace}
        if e.get("detail"):
            out["detail"] = e["detail"]
        if e.get("retry_after_ms") is not None:
            out["retry_after_ms"] = e["retry_after_ms"]
        return out

    def log_decoders(self, args: dict) -> tuple[dict, list[str]]:
        """記録の列ごとの換算方法（バインド済みの部品のデコーダと覚えている校正値）。換算できない列の名前も返す。"""
        decs, missing = {}, []
        for r in args["reads"]:
            b = self.bindings.get((r["bus"], r["addr"]))
            part = parts_mod.PARTS.get(b.part) if b else None
            cal = self.calibration.get((r["bus"], r["addr"]), "")
            if part is None or part.decoder is None or (part.calibration is not None and not cal):
                missing.append(r["name"])
                continue
            decs[r["name"]] = {"decoder": part.decoder, "calibration_hex": cal, "part": part.name,
                               "channels": list(part.channels)}
        return decs, missing

    def _remember_calibration(self, args: dict, hex_: str) -> None:
        b = self.bindings.get((args["bus"], args["addr"]))
        part = parts_mod.PARTS.get(b.part) if b else None
        cal = part.calibration if part else None
        if cal and args.get("reg") == cal.reg and args["len"] >= cal.len:
            self.calibration[(args["bus"], args["addr"])] = hex_

    # ---- 換算 ---------------------------------------------------------------------------

    async def job_samples(self, job_id: str, decoder: str, column: str, calibration_hex: str = "",
                          shunt_column: str = "", shunt_ohm: float = 0.1) -> dict:
        cols = self.jobs.get(job_id)
        raw = await self.cloud.samples(self.device_id, job_id)
        if cols is None:
            return {"status": "error", "message": f"job {job_id} はこのセッションで始めたものではない"}
        if column not in cols:
            return {"status": "error", "message": f"column は {cols} のどれか"}
        events = await self.cloud.events(self.device_id)
        end = next((e["detail"] for e in events if e["event"] == "job_end" and e["detail"]["job_id"] == job_id), None)
        ci = cols.index(column)
        si = cols.index(shunt_column) if shunt_column in cols else None
        if si is not None and self.job_reads.get(job_id):
            r = self.job_reads[job_id][ci]
            self.shunt_ohm[(r["bus"], r["addr"])] = shunt_ohm      # 次の計測の途中経過でも電流を出せるように
        if not calibration_hex and self.job_reads.get(job_id):
            r = self.job_reads[job_id][ci]
            calibration_hex = self.calibration.get((r["bus"], r["addr"]), "")    # 省略されたら覚えている値を使う
        points, nulls, errors = [], 0, []
        for row in raw["rows"]:
            v = row["v"][ci]
            if v is None:
                nulls += 1
                continue
            try:
                extra = {"shunt_raw": row["v"][si], "shunt_ohm": shunt_ohm} if si is not None and row["v"][si] else {}
                points.append({"t_ms": row["t"], **parts_mod.decode(decoder, v, calibration_hex, **extra)})
            except parts_mod.DecodeError as e:
                errors.append(str(e))
        self.series.setdefault(job_id, {})[column] = {"decoder": parts_mod.ALIASES.get(decoder.lower(), decoder.lower()),
                                                      "column": column, "points": points, "complete": end is not None}
        stats = {}
        for ch in (points[0].keys() - {"t_ms"} if points else []):
            vals = [p[ch] for p in points]
            stats[ch] = {"min": min(vals), "max": max(vals), "mean": round(sum(vals) / len(vals), 3),
                         "first": vals[0], "last": vals[-1]}
        step = max(1, len(points) // 30)
        return {"status": "completed" if end else "running", "job_end": end, "rows": len(raw["rows"]),
                "decoded": len(points), "null_values": nulls, "decode_errors": errors[:3],
                "missing_batches": raw["missing_seq"], "stats": stats, "preview": points[::step][:30]}

    async def job_end(self, job_id: str) -> dict | None:
        events = await self.cloud.events(self.device_id)
        return next((e["detail"] for e in events if e["event"] == "job_end" and e["detail"]["job_id"] == job_id), None)

    async def wait_for_job(self, job_id: str, max_wait_s: float = 45.0) -> dict:
        """計測ジョブの終わりを待つ（短い計測用）。待てる時間を超えるなら待たずに残り時間を返す。"""
        meta = self.job_meta.get(job_id)
        if meta is None:
            return {"status": "error", "message": f"job {job_id} はこのセッションで始めたものではない"}
        remaining = (meta["started_ts"] + meta["duration_ms"]) / 1000 - time.time()
        if remaining > max_wait_s:
            return {"status": "running", "remaining_s": round(remaining),
                    "next": "長い計測なので待たない。終了予定を人に伝えてターンを終える（画面に途中経過が出る）"}
        deadline = time.time() + max(0.0, remaining) + 10
        while time.time() < deadline:
            end = await self.job_end(job_id)
            if end is not None:
                return {"status": "completed", "job_end": end}
            await asyncio.sleep(1.0)
        return {"status": "running", "remaining_s": 0, "message": "終了予定を過ぎたが job_end が届いていない"}

    async def live(self, job_id: str) -> dict:
        """計測中・計測後の途中経過（UI 用）。列ごとに、バインド済みの部品のデコーダと覚えている校正値で換算する。"""
        meta = self.job_meta.get(job_id)
        if meta is None:
            raise KeyError(job_id)
        raw = await self.cloud.samples(self.device_id, job_id)
        end = await self.job_end(job_id)
        cols = self.jobs.get(job_id, [])
        reads = self.job_reads[job_id]
        out: dict[str, dict] = {}
        for ci, col in enumerate(cols):
            if col in self.series.get(job_id, {}):
                out[col] = self.series[job_id][col]     # エージェントが get_samples で換算した系列（シャント抵抗も指定済み）
                continue
            r = reads[ci]
            b = self.bindings.get((r["bus"], r["addr"]))
            part = parts_mod.PARTS.get(b.part) if b else None
            if part is None or part.decoder is None:
                continue
            if part.decoder == "ina226":
                ina = self._live_ina226(raw["rows"], reads, ci, end is not None)
                if ina:
                    out[col] = ina
                continue
            cal = self.calibration.get((r["bus"], r["addr"]), "")
            if part.calibration is not None and not cal:
                continue
            pts = []
            for row in raw["rows"]:
                v = row["v"][ci]
                if v is None:
                    continue
                try:
                    pts.append({"t_ms": row["t"], **parts_mod.decode(part.decoder, v, cal)})
                except parts_mod.DecodeError:
                    pass
            out[col] = {"decoder": part.decoder, "column": col, "points": pts, "complete": end is not None}
        elapsed = min(meta["duration_ms"], max(0, int(time.time() * 1000) - meta["started_ts"]))
        return {"job_id": job_id, "status": "completed" if end else "running", "rows": len(raw["rows"]),
                "elapsed_ms": meta["duration_ms"] if end else elapsed, **meta, "job_end": end, "columns": out}

    def _live_ina226(self, rows: list[dict], reads: list[dict], ci: int, complete: bool) -> dict | None:
        """INA226 のバス電圧の列（0x02, 2バイト）を換算する。同じ部品のシャント電圧の列（0x01）があれば
        シャント電圧も足し、シャント抵抗が分かっていれば（get_samples で使った値）電流も足す。推測の抵抗値では電流を出さない。"""
        r = reads[ci]
        if r.get("reg") != 0x02 or r.get("length", r.get("len")) != 2:
            return None
        key = (r["bus"], r["addr"])
        si = next((i for i, x in enumerate(reads)
                   if (x["bus"], x["addr"]) == key and x.get("reg") == 0x01 and x.get("length", x.get("len")) == 2), None)
        ohm = self.shunt_ohm.get(key)
        pts = []
        for row in rows:
            v = row["v"][ci]
            if v is None:
                continue
            try:
                p = parts_mod.decode("ina226", v)
                sv = row["v"][si] if si is not None else None
                if sv:
                    d = parts_mod.decode("ina226", v, shunt_raw=sv, shunt_ohm=ohm or 1.0)
                    p["shunt_mv"] = d["shunt_mv"]
                    if ohm:
                        p["current_ma"] = d["current_ma"]
                pts.append({"t_ms": row["t"], **p})
            except parts_mod.DecodeError:
                pass
        return {"decoder": "ina226", "column": r["name"], "points": pts, "complete": complete}

    # ---- エージェントに渡す要約 ---------------------------------------------------------------

    def overview(self) -> dict:
        m = self.manifest
        return {
            "device_id": self.device_id,
            "name": m["device"].get("name"),
            "board": m["device"].get("board"),
            "i2c_buses": [{"bus": b["bus"], "label": b.get("label"), "scan": b["scan"]["enabled"],
                           "scan_range": [b["scan"]["first"], b["scan"]["last"]],
                           "unknown_address": b["unknown"], "fixed_addresses": [
                               {"addr": a["addr"], "label": a.get("label"), "protected": a["protected"]}
                               for a in b["addresses"]]} for b in m["i2c"]["buses"]],
            "gpio_lines": [{"line": ln["line"], "label": ln.get("label"), "direction": ln["direction"],
                            "write": ln.get("write"), "safe_value": ln.get("safe_value"),
                            "max_on_ms": ln.get("max_on_ms")} for ln in m["gpio"]["lines"]],
            "cameras": [{"id": c["id"], "kind": c["kind"], "label": c.get("label"), "capture": c["capture"],
                         "max_size": [c["max_width"], c["max_height"]]}
                        for c in m.get("camera", {}).get("devices", [])],
            "limits": m["limits"], "sample": m["sample"],
            "bindings": [b.to_wire() for b in self.bindings.values()],
        }
