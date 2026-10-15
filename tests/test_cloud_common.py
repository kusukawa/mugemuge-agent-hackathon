"""services/common: MemoryStore の振る舞いと、開発モードの安全装置。"""
import asyncio

import pytest

from services.common.auth import CallerAuth, dev_insecure
from services.common.store import MemoryStore, samples_doc


def test_samples_rows_become_maps():
    doc = samples_doc({"job_id": "job_1", "seq": 3, "ts": 1, "rows": [[0, "0a14", None]], "errors": 1})
    assert doc["rows"] == [{"t": 0, "v": ["0a14", None]}]


def test_pairing_code_is_single_use_and_expires():
    async def go():
        s = MemoryStore()
        assert await s.create_pairing_code("123456", "user:a", expires_ms=2_000_000_000_000)
        assert not await s.create_pairing_code("123456", "user:b", expires_ms=2_000_000_000_000)
        assert await s.consume_pairing_code("123456", now_ms=1) == "user:a"
        assert await s.consume_pairing_code("123456", now_ms=1) is None
        await s.create_pairing_code("654321", "user:a", expires_ms=10)
        assert await s.consume_pairing_code("654321", now_ms=11) is None
    asyncio.run(go())


def test_repairing_replaces_token():
    async def go():
        s = MemoryStore()
        await s.set_device_token("pi-1", "user:a", "aaa")
        await s.set_device_token("pi-1", "user:a", "bbb")
        assert await s.device_by_token("aaa") is None
        assert (await s.device_by_token("bbb"))["device_id"] == "pi-1"
    asyncio.run(go())


def test_approval_is_decided_once():
    async def go():
        s = MemoryStore()
        await s.create_approval("apr_1", {"status": "pending", "created_ms": 1, "request": {}})
        assert await s.decide_approval("apr_1", {"status": "approved"})
        assert not await s.decide_approval("apr_1", {"status": "denied"})
        assert (await s.get_approval("apr_1"))["status"] == "approved"
        assert await s.list_approvals("pending") == []
    asyncio.run(go())


def test_dev_insecure_refused_on_cloud_run(monkeypatch):
    monkeypatch.setenv("MUGEMUGE_DEV_INSECURE", "1")
    monkeypatch.setenv("K_SERVICE", "mugemuge-gateway")
    with pytest.raises(RuntimeError):
        dev_insecure()


def test_caller_auth_requires_config_outside_dev(monkeypatch):
    monkeypatch.delenv("MUGEMUGE_DEV_INSECURE", raising=False)
    monkeypatch.delenv("X_AUD", raising=False)
    with pytest.raises(RuntimeError):
        CallerAuth.from_env("X_AUD", "X_CALLERS")
