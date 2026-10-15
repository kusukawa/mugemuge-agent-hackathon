"""exec を実行してよいかの最終判定（PROTOCOL.md 6.1節の手順2〜5）。

    verdict = gate.check(op, args, approval, now_ms=..., device_now_s=...)
    if verdict.may_execute:
        if verdict.fingerprint_check: ...読み直して不一致なら fingerprint_mismatch
        ...I/O を実行...
    result.policy = verdict.policy(manifest_sha256)

check() が may_execute=True を返したときだけ、レートのトークンと承認グラントを消費する。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .approval import ApprovalInvalid, GrantLedger, public_key_from_b64, verify_grant
from .canonical import args_sha256
from .manifest import manifest_sha256
from .policy import ALWAYS_SAFE_OPS, READ_OPS, BadRequest, DeviceState, Level, Policy
from .ratelimit import TokenBucket


@dataclass
class Verdict:
    decision: str                      # allow / approved / approval_required / deny
    rule: str
    error_code: str | None = None      # None なら実行してよい
    message: str = ""
    detail: dict = field(default_factory=dict)
    request: dict | None = None
    retry_after_ms: int | None = None
    fingerprint_check: tuple[int, int] | None = None
    grant_id: str | None = None

    @property
    def may_execute(self) -> bool:
        return self.error_code is None

    def error(self) -> dict | None:
        if self.error_code is None:
            return None
        e: dict = {"code": self.error_code, "message": self.message}
        if self.detail:
            e["detail"] = self.detail
        if self.request is not None:
            e["request"] = self.request
        if self.retry_after_ms is not None:
            e["retry_after_ms"] = self.retry_after_ms
        return e

    def policy(self, manifest_hash: str) -> dict:
        return {"decision": self.decision, "rule": self.rule, "manifest_sha256": manifest_hash}


class Gate:
    def __init__(self, manifest: dict, state: DeviceState, now_ms: int):
        self.manifest = manifest
        self.manifest_hash = manifest_sha256(manifest)
        self.policy = Policy(manifest)
        self.state = state
        self.ledger = GrantLedger()
        self.public_keys = {k["kid"]: public_key_from_b64(k["key"]) for k in manifest["approval"]["public_keys"]}
        lim = manifest["limits"]
        self.buckets = {
            "exec": TokenBucket.create(lim["exec_per_s"], now_ms),
            "i2c_read": TokenBucket.create(lim["i2c_read_per_s"], now_ms),
            "i2c_write": TokenBucket.create(lim["i2c_write_per_s"], now_ms),
            "gpio_write": TokenBucket.create(lim["gpio_write_per_s"], now_ms),
            "display": TokenBucket.create(lim["display_per_s"], now_ms),
        }
        # カメラごとの直近1時間の撮影時刻（camera.capture は読み出し扱いで capture: allow なら連写できるため: I2）
        self.camera_shots: dict[str, deque[int]] = {}
        self.cameras = {c["id"]: c for c in (manifest.get("camera") or {}).get("devices", [])}

    def _camera_wait_ms(self, camera: str, now_ms: int) -> int:
        """次に撮れるまでの待ち時間（0 なら今撮れる）。min_interval_ms と max_per_hour の両方を見る。"""
        cam = self.cameras.get(camera)
        if cam is None:
            return 0
        shots = self.camera_shots.setdefault(camera, deque())
        while shots and now_ms - shots[0] >= 3_600_000:
            shots.popleft()
        wait = 0
        if shots:
            wait = max(wait, shots[-1] + cam["min_interval_ms"] - now_ms)
        if len(shots) >= cam["max_per_hour"]:
            wait = max(wait, shots[len(shots) - cam["max_per_hour"]] + 3_600_000 - now_ms)
        return max(0, wait)

    def check(self, op: str, args: dict, approval: dict | None, *, now_ms: int, device_now_s: int) -> Verdict:
        """now_ms は単調時計（clock.mono_ms）。レート上限・カメラの上限・指紋の読み直しの間隔に使う。
        device_now_s は承認の期限判定用の補正済み時刻（clock.corrected_s。PROTOCOL.md 4.2節）。"""
        # 1. 形式（スキーマ検証はメッセージ受信時に済ませておく。ここは意味の検査）
        try:
            d = self.policy.evaluate(op, args, self.state, now_ms)
        except BadRequest as e:
            return Verdict("deny", op, "bad_request", str(e))

        # 2. 非常停止
        if self.state.estop and not (op in READ_OPS or d.safe or op == "sys.estop_clear"):
            return Verdict("deny", d.rule, "estop_active", "emergency stop is latched")

        # 3〜4. ルールと静的な上限（policy.evaluate 済み）
        if d.level is Level.DENY:
            return Verdict("deny", d.rule, "policy_denied", f"denied by manifest ({d.reason or d.rule})",
                           detail={"reason": d.reason} if d.reason else {})

        # 4. レート上限（安全側の操作にはかけない）
        need = {"exec": 1, "i2c_read": d.i2c_reads, "i2c_write": d.i2c_writes, "gpio_write": d.gpio_writes,
                "display": d.displays}
        if not d.safe:
            for name, n in need.items():
                if n == 0:
                    continue
                wait = self.buckets[name].retry_after_ms(n, now_ms)
                if wait != 0:
                    if wait < 0:
                        return Verdict("deny", d.rule, "policy_denied", f"{name} burst capacity exceeded",
                                       detail={"reason": f"limits.{name}_burst"})
                    return Verdict("allow", d.rule, "rate_limited", f"{name} rate limit", retry_after_ms=wait)
        if op == "camera.capture":
            wait = self._camera_wait_ms(args["camera"], now_ms)
            if wait:
                return Verdict("allow", d.rule, "rate_limited", "camera rate limit", retry_after_ms=wait)

        # 5. 承認
        decision, grant_id = "allow", None
        if d.level is Level.APPROVAL:
            request = {
                "device_id": self.state.device_id,
                "session_id": self.state.session_id or "",
                "op": op,
                "args": args,
                "args_sha256": args_sha256(op, args),
                "rule": d.rule,
                "summary": d.summary or op,
            }
            if approval is None:
                return Verdict("approval_required", d.rule, "approval_required",
                               f"{op} requires approval ({d.rule})", request=request)
            try:
                verify_grant(
                    approval,
                    public_keys=self.public_keys,
                    device_id=self.state.device_id,
                    session_id=self.state.session_id or "",
                    op=op,
                    args=args,
                    now_s=device_now_s,
                    max_ttl_s=self.manifest["approval"]["max_ttl_s"],
                    ledger=self.ledger,
                )
            except ApprovalInvalid as e:
                return Verdict("approval_required", d.rule, "approval_invalid", f"approval rejected: {e.reason}",
                               detail={"reason": e.reason}, request=request)
            decision, grant_id = "approved", approval["grant_id"]

        # ここまで来たら実行してよい。トークンとグラントを消費する。
        if not d.safe:
            for name, n in need.items():
                if n:
                    self.buckets[name].consume(n, now_ms)
        if op == "camera.capture" and args["camera"] in self.cameras:
            self.camera_shots.setdefault(args["camera"], deque()).append(now_ms)
        if grant_id is not None:
            self.ledger.mark(grant_id, approval["exp"])
            self.ledger.prune(device_now_s)
        return Verdict(decision, d.rule, fingerprint_check=d.fingerprint_check, grant_id=grant_id)
