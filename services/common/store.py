"""保存先（docs/CLOUD.md 5章）。

Store が両サービスから見たインタフェース。テストとローカル開発は MemoryStore、本番は FirestoreStore。
M1 の FakeI2C と同じく、クラウドなしでテストが回るようにするための層。
"""
from __future__ import annotations

import asyncio
import copy
import time
from abc import ABC, abstractmethod

APPROVAL_STATUSES = ("pending", "approved", "denied", "expired")


def _now_ms() -> int:
    return int(time.time() * 1000)


def samples_doc(msg: dict) -> dict:
    """samples メッセージを Firestore に置ける形にする（配列の中に配列を置けないので行を map にする）。"""
    return {
        "job_id": msg["job_id"],
        "seq": msg["seq"],
        "ts": msg["ts"],
        "errors": msg.get("errors", 0),
        "rows": [{"t": row[0], "v": list(row[1:])} for row in msg["rows"]],
    }


def request_key(device_id: str, session_id: str, args_sha256: str) -> str:
    return f"{device_id}/{session_id}_{args_sha256}"


class Store(ABC):
    # ---- devices -----------------------------------------------------------------
    @abstractmethod
    async def get_device(self, device_id: str) -> dict | None: ...

    @abstractmethod
    async def device_by_token(self, token_sha256: str) -> dict | None: ...

    @abstractmethod
    async def set_device_token(self, device_id: str, owner: str, token_sha256: str) -> None:
        """ペアリング。デバイスがなければ作り、あれば token を差し替える（古いトークンは無効になる）。"""

    @abstractmethod
    async def update_device(self, device_id: str, fields: dict) -> None: ...

    @abstractmethod
    async def list_devices(self) -> list[dict]: ...

    # ---- pairing codes -------------------------------------------------------------
    @abstractmethod
    async def create_pairing_code(self, code: str, owner: str, expires_ms: int) -> bool:
        """同じコードが有効なまま残っていれば False。"""

    @abstractmethod
    async def consume_pairing_code(self, code: str, now_ms: int) -> str | None:
        """1回限り。有効なら消して owner を返す。"""

    # ---- samples / events ----------------------------------------------------------
    @abstractmethod
    async def add_samples(self, device_id: str, msg: dict) -> None: ...

    @abstractmethod
    async def list_samples(self, device_id: str, job_id: str) -> list[dict]: ...

    @abstractmethod
    async def add_event(self, device_id: str, msg: dict) -> None: ...

    @abstractmethod
    async def list_events(self, device_id: str, limit: int = 50) -> list[dict]: ...

    # ---- 長期の記録（log.start）と写真 -------------------------------------------------------
    @abstractmethod
    async def put_log(self, device_id: str, log_id: str, fields: dict) -> None:
        """記録の情報（名前・引数・状態・行数・写真の枚数・最新の値・デコーダ）を merge で書く。"""

    @abstractmethod
    async def get_log(self, device_id: str, log_id: str) -> dict | None: ...

    @abstractmethod
    async def list_logs(self, device_id: str) -> list[dict]: ...

    @abstractmethod
    async def put_log_batch(self, device_id: str, log_id: str, doc: dict) -> bool:
        """バッチを書く。同じ seq がもうあれば上書きして False（再送）。"""

    @abstractmethod
    async def list_log_batches(self, device_id: str, log_id: str) -> list[dict]: ...

    @abstractmethod
    async def put_blob_meta(self, device_id: str, blob_id: str, doc: dict) -> None: ...

    @abstractmethod
    async def get_blob_meta(self, device_id: str, blob_id: str) -> dict | None: ...

    # ---- チャット（Web/API が Agent のセッションを、始めたブラウザのセッションに結びつける） ----
    @abstractmethod
    async def put_chat(self, chat_id: str, doc: dict) -> None: ...

    @abstractmethod
    async def get_chat(self, chat_id: str) -> dict | None: ...

    # ---- Gateway の世代 -------------------------------------------------------------------
    @abstractmethod
    async def set_gateway_leader(self, instance: str, started_ms: int) -> None:
        """いちばん新しく起動した Gateway インスタンスを記録する（古いものは自分のデバイスを手放す）。"""

    @abstractmethod
    async def get_gateway_leader(self) -> dict | None: ...

    # ---- デバイスが発行した承認要求 ---------------------------------------------------
    @abstractmethod
    async def put_device_request(self, device_id: str, request: dict, ts: int) -> None:
        """Gateway が、デバイスの result に入っていた error.request を記録する。"""

    @abstractmethod
    async def get_device_request(self, device_id: str, session_id: str, args_sha256: str) -> dict | None: ...

    # ---- approvals -----------------------------------------------------------------
    @abstractmethod
    async def create_approval(self, approval_id: str, doc: dict) -> None: ...

    @abstractmethod
    async def get_approval(self, approval_id: str) -> dict | None: ...

    @abstractmethod
    async def decide_approval(self, approval_id: str, fields: dict) -> bool:
        """status が pending のときだけ fields で更新する（二重承認・承認と却下の競合を防ぐ）。"""

    @abstractmethod
    async def list_approvals(self, status: str = "pending") -> list[dict]: ...


class MemoryStore(Store):
    def __init__(self) -> None:
        self.devices: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.samples: dict[str, dict[str, dict]] = {}
        self.events: dict[str, list[dict]] = {}
        self.approvals: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.leader: dict | None = None
        self.chats: dict[str, dict] = {}
        self.logs: dict[tuple[str, str], dict] = {}
        self.log_batches: dict[tuple[str, str], dict[int, dict]] = {}
        self.blob_meta: dict[tuple[str, str], dict] = {}
        self._lock = asyncio.Lock()

    async def get_device(self, device_id):
        d = self.devices.get(device_id)
        return copy.deepcopy(d) if d else None

    async def device_by_token(self, token_sha256):
        for d in self.devices.values():
            if d.get("token_sha256") == token_sha256:
                return copy.deepcopy(d)
        return None

    async def set_device_token(self, device_id, owner, token_sha256):
        async with self._lock:
            d = self.devices.setdefault(device_id, {"device_id": device_id, "online": False})
            d.update(owner=owner, token_sha256=token_sha256)

    async def update_device(self, device_id, fields):
        async with self._lock:
            self.devices.setdefault(device_id, {"device_id": device_id}).update(copy.deepcopy(fields))

    async def list_devices(self):
        return [copy.deepcopy(d) for d in sorted(self.devices.values(), key=lambda d: d["device_id"])]

    async def create_pairing_code(self, code, owner, expires_ms):
        async with self._lock:
            old = self.codes.get(code)
            if old is not None and old["expires_ms"] >= _now_ms():
                return False
            self.codes[code] = {"owner": owner, "expires_ms": expires_ms}
            return True

    async def consume_pairing_code(self, code, now_ms):
        async with self._lock:
            c = self.codes.pop(code, None)
            if c is None or c["expires_ms"] < now_ms:
                return None
            return c["owner"]

    async def add_samples(self, device_id, msg):
        doc = samples_doc(msg)
        self.samples.setdefault(device_id, {})[f"{doc['job_id']}_{doc['seq']:06d}"] = doc

    async def list_samples(self, device_id, job_id):
        docs = self.samples.get(device_id, {})
        return [copy.deepcopy(docs[k]) for k in sorted(docs) if docs[k]["job_id"] == job_id]

    async def add_event(self, device_id, msg):
        self.events.setdefault(device_id, []).append(copy.deepcopy(msg))

    async def list_events(self, device_id, limit=50):
        return copy.deepcopy(self.events.get(device_id, [])[-limit:][::-1])

    async def put_log(self, device_id, log_id, fields):
        self.logs.setdefault((device_id, log_id), {"log_id": log_id, "device_id": device_id}).update(
            copy.deepcopy(fields))

    async def get_log(self, device_id, log_id):
        d = self.logs.get((device_id, log_id))
        return copy.deepcopy(d) if d else None

    async def list_logs(self, device_id):
        out = [copy.deepcopy(v) for (dev, _), v in self.logs.items() if dev == device_id]
        return sorted(out, key=lambda d: d.get("started_ts", 0), reverse=True)

    async def put_log_batch(self, device_id, log_id, doc):
        b = self.log_batches.setdefault((device_id, log_id), {})
        new = doc["seq"] not in b
        b[doc["seq"]] = copy.deepcopy(doc)
        return new

    async def list_log_batches(self, device_id, log_id):
        b = self.log_batches.get((device_id, log_id), {})
        return [copy.deepcopy(b[k]) for k in sorted(b)]

    async def put_blob_meta(self, device_id, blob_id, doc):
        self.blob_meta[(device_id, blob_id)] = copy.deepcopy(doc)

    async def get_blob_meta(self, device_id, blob_id):
        d = self.blob_meta.get((device_id, blob_id))
        return copy.deepcopy(d) if d else None

    async def put_chat(self, chat_id, doc):
        self.chats[chat_id] = copy.deepcopy(doc)

    async def get_chat(self, chat_id):
        c = self.chats.get(chat_id)
        return copy.deepcopy(c) if c else None

    async def set_gateway_leader(self, instance, started_ms):
        self.leader = {"instance": instance, "started_ms": started_ms}

    async def get_gateway_leader(self):
        return dict(self.leader) if self.leader else None

    async def put_device_request(self, device_id, request, ts):
        self.requests[request_key(device_id, request["session_id"], request["args_sha256"])] = {
            "request": copy.deepcopy(request), "ts": ts}

    async def get_device_request(self, device_id, session_id, args_sha256):
        r = self.requests.get(request_key(device_id, session_id, args_sha256))
        return copy.deepcopy(r) if r else None

    async def create_approval(self, approval_id, doc):
        async with self._lock:
            self.approvals[approval_id] = {"approval_id": approval_id, **copy.deepcopy(doc)}

    async def get_approval(self, approval_id):
        a = self.approvals.get(approval_id)
        return copy.deepcopy(a) if a else None

    async def decide_approval(self, approval_id, fields):
        async with self._lock:
            a = self.approvals.get(approval_id)
            if a is None or a["status"] != "pending":
                return False
            a.update(copy.deepcopy(fields))
            return True

    async def list_approvals(self, status="pending"):
        out = [a for a in self.approvals.values() if a["status"] == status]
        return copy.deepcopy(sorted(out, key=lambda a: a["created_ms"]))


class FirestoreStore(Store):
    """本番。google-cloud-firestore の AsyncClient（Cloud Run のサービスアカウントで認証）。"""

    def __init__(self, project: str | None = None, database: str = "(default)") -> None:
        from google.cloud import firestore

        self._fs = firestore
        self.db = firestore.AsyncClient(project=project, database=database)

    def _dev(self, device_id):
        return self.db.collection("devices").document(device_id)

    async def get_device(self, device_id):
        snap = await self._dev(device_id).get()
        return snap.to_dict() if snap.exists else None

    async def device_by_token(self, token_sha256):
        q = self.db.collection("devices").where(
            filter=self._fs.FieldFilter("token_sha256", "==", token_sha256)).limit(1)
        async for snap in q.stream():
            return snap.to_dict()
        return None

    async def set_device_token(self, device_id, owner, token_sha256):
        await self._dev(device_id).set({"device_id": device_id, "owner": owner, "token_sha256": token_sha256},
                                       merge=True)

    async def update_device(self, device_id, fields):
        await self._dev(device_id).set({"device_id": device_id, **fields}, merge=True)

    async def list_devices(self):
        return [s.to_dict() async for s in self.db.collection("devices").order_by("device_id").stream()]

    async def create_pairing_code(self, code, owner, expires_ms):
        from google.api_core.exceptions import AlreadyExists

        ref = self.db.collection("pairing_codes").document(code)
        try:
            await ref.create({"owner": owner, "expires_ms": expires_ms})
            return True
        except AlreadyExists:
            snap = await ref.get()
            if snap.exists and snap.to_dict()["expires_ms"] < _now_ms():
                await ref.set({"owner": owner, "expires_ms": expires_ms})   # 期限切れの残骸は上書きしてよい
                return True
            return False

    async def consume_pairing_code(self, code, now_ms):
        ref = self.db.collection("pairing_codes").document(code)
        transaction = self.db.transaction()

        @self._fs.async_transactional
        async def run(tx):
            snap = await ref.get(transaction=tx)
            if not snap.exists:
                return None
            tx.delete(ref)
            c = snap.to_dict()
            return c["owner"] if c["expires_ms"] >= now_ms else None

        return await run(transaction)

    async def add_samples(self, device_id, msg):
        doc = samples_doc(msg)
        await self._dev(device_id).collection("samples").document(f"{doc['job_id']}_{doc['seq']:06d}").set(doc)

    async def list_samples(self, device_id, job_id):
        q = self._dev(device_id).collection("samples").where(
            filter=self._fs.FieldFilter("job_id", "==", job_id))
        docs = [s.to_dict() async for s in q.stream()]
        return sorted(docs, key=lambda d: d["seq"])

    async def add_event(self, device_id, msg):
        await self._dev(device_id).collection("events").add(msg)

    async def list_events(self, device_id, limit=50):
        q = self._dev(device_id).collection("events").order_by("ts", direction="DESCENDING").limit(limit)
        return [s.to_dict() async for s in q.stream()]

    def _log(self, device_id, log_id):
        return self._dev(device_id).collection("logs").document(log_id)

    async def put_log(self, device_id, log_id, fields):
        await self._log(device_id, log_id).set({"log_id": log_id, "device_id": device_id, **fields}, merge=True)

    async def get_log(self, device_id, log_id):
        snap = await self._log(device_id, log_id).get()
        return snap.to_dict() if snap.exists else None

    async def list_logs(self, device_id):
        docs = [s.to_dict() async for s in self._dev(device_id).collection("logs").stream()]
        return sorted(docs, key=lambda d: d.get("started_ts", 0), reverse=True)

    async def put_log_batch(self, device_id, log_id, doc):
        ref = self._log(device_id, log_id).collection("batches").document(f"{doc['seq']:06d}")
        new = not (await ref.get()).exists
        await ref.set(doc)
        return new

    async def list_log_batches(self, device_id, log_id):
        q = self._log(device_id, log_id).collection("batches").order_by("seq")
        return [s.to_dict() async for s in q.stream()]

    async def put_blob_meta(self, device_id, blob_id, doc):
        await self._dev(device_id).collection("blobs").document(blob_id).set(doc)

    async def get_blob_meta(self, device_id, blob_id):
        snap = await self._dev(device_id).collection("blobs").document(blob_id).get()
        return snap.to_dict() if snap.exists else None

    async def put_chat(self, chat_id, doc):
        await self.db.collection("chats").document(chat_id).set(doc)

    async def get_chat(self, chat_id):
        snap = await self.db.collection("chats").document(chat_id).get()
        return snap.to_dict() if snap.exists else None

    async def set_gateway_leader(self, instance, started_ms):
        await self.db.collection("gateway").document("leader").set({"instance": instance, "started_ms": started_ms})

    async def get_gateway_leader(self):
        snap = await self.db.collection("gateway").document("leader").get()
        return snap.to_dict() if snap.exists else None

    async def put_device_request(self, device_id, request, ts):
        key = f"{request['session_id']}_{request['args_sha256']}"
        await self._dev(device_id).collection("requests").document(key).set({"request": request, "ts": ts})

    async def get_device_request(self, device_id, session_id, args_sha256):
        snap = await self._dev(device_id).collection("requests").document(f"{session_id}_{args_sha256}").get()
        return snap.to_dict() if snap.exists else None

    async def create_approval(self, approval_id, doc):
        await self.db.collection("approvals").document(approval_id).create({"approval_id": approval_id, **doc})

    async def get_approval(self, approval_id):
        snap = await self.db.collection("approvals").document(approval_id).get()
        return snap.to_dict() if snap.exists else None

    async def decide_approval(self, approval_id, fields):
        ref = self.db.collection("approvals").document(approval_id)
        transaction = self.db.transaction()

        @self._fs.async_transactional
        async def run(tx):
            snap = await ref.get(transaction=tx)
            if not snap.exists or snap.to_dict()["status"] != "pending":
                return False
            tx.update(ref, fields)
            return True

        return await run(transaction)

    async def list_approvals(self, status="pending"):
        q = self.db.collection("approvals").where(filter=self._fs.FieldFilter("status", "==", status))
        docs = [s.to_dict() async for s in q.stream()]
        return sorted(docs, key=lambda a: a["created_ms"])


def make_store() -> Store:
    import os

    if os.environ.get("MUGEMUGE_STORE", "firestore") == "memory":
        return MemoryStore()
    return FirestoreStore(project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                          database=os.environ.get("FIRESTORE_DATABASE", "(default)"))
