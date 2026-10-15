"""監査ログの鎖とローテート。"""
import json

import pytest

from mugemuge_agentd.audit import GENESIS, AuditLog, verify_chain


def test_chain_links_each_line(tmp_path):
    a = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        a.append({"op": "i2c.read", "i": i})
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["prev_sha256"] == GENESIS
    assert [json.loads(x)["seq"] for x in lines] == [0, 1, 2, 3, 4]
    assert verify_chain([tmp_path / "audit.jsonl"]) == 5


def test_tampering_breaks_chain(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    for i in range(3):
        a.append({"op": "i2c.read", "i": i})
    lines = p.read_text().splitlines()
    lines[1] = lines[1].replace('"i":1', '"i":9')
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError):
        verify_chain([p])


def test_restart_continues_chain(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p).append({"op": "a"})
    a2 = AuditLog(p)
    a2.append({"op": "b"})
    assert [json.loads(x)["seq"] for x in p.read_text().splitlines()] == [0, 1]
    assert verify_chain([p]) == 2


def test_rotation_carries_hash_into_new_file(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p, max_mb=1)
    a.max_bytes = 300
    for i in range(10):
        a.append({"op": "i2c.read", "pad": "x" * 50, "i": i})
    files = sorted(tmp_path.glob("audit.jsonl.*"), key=lambda f: -int(f.suffix[1:])) + [p]
    assert len(files) >= 3
    # 残っているファイルを古い順につなげても鎖が切れない
    assert verify_chain(files) > 0
