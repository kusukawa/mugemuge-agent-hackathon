"""マニフェストとメッセージの例がスキーマに通ること、壊れた例が弾かれること。"""
import copy
import json

import pytest
import yaml
from conftest import ROOT

from mugemuge_agentd.approval import GrantLedger, public_key_from_b64, verify_grant
from mugemuge_agentd.canonical import canonical_json
from mugemuge_agentd.manifest import ManifestError, load_manifest, manifest_sha256, parse_manifest
from mugemuge_agentd.messages import MalformedMessage, message_errors, parse_message

EXAMPLES = ["examples/manifest.pi-grove.yaml", "examples/manifest.mugemuge-hat.yaml"]


@pytest.mark.parametrize("path", EXAMPLES)
def test_example_manifests_are_valid(path):
    m = load_manifest(ROOT / path)
    assert len(manifest_sha256(m)) == 64


def _raw():
    return yaml.safe_load((ROOT / EXAMPLES[0]).read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate, why", [
    (lambda r: r["gpio"]["lines"][0].pop("safe_value"), "出力ピンに safe_value がない"),
    (lambda r: r["gpio"]["lines"][0].pop("max_on_ms"), "出力ピンの保持時間を決めていない（無制限なら null と明記する）"),
    (lambda r: r["gpio"]["lines"][1].update(write="allow"), "入力ピンに write がある"),
    (lambda r: r["i2c"]["buses"][0]["unknown"].update(read="approval"), "未知アドレスの読み出しを承認制にはできない"),
    (lambda r: r["i2c"]["buses"][0]["addresses"].append({"addr": 0x05, "label": "x", "read": "allow", "write": "allow"}), "予約アドレス"),
    (lambda r: r["i2c"]["buses"].append(copy.deepcopy(r["i2c"]["buses"][0])), "バス番号の重複"),
    (lambda r: r["safety"].update(deadman_ms=1000), "デッドマンが短すぎる"),
    (lambda r: r["safety"].update(estop_clear="deny"), "非常停止の解除を deny にすると二度と動かせない"),
    (lambda r: r.update(extra=1), "未定義のキー"),
    (lambda r: r["link"].update(gateway_url="ws://insecure.example/v1/device"), "TLS なしの接続先"),
])
def test_invalid_manifests_are_rejected(mutate, why):
    r = _raw()
    mutate(r)
    with pytest.raises(ManifestError):
        parse_manifest(r)


def _session():
    return json.loads((ROOT / "examples/session.json").read_text(encoding="utf-8"))


def test_every_example_message_matches_protocol_schema():
    for step in _session():
        assert message_errors(step["msg"]) == [], step["note"]


def test_example_grants_verify_against_example_manifest():
    manifest = load_manifest(ROOT / EXAMPLES[0])
    keys = {k["kid"]: public_key_from_b64(k["key"]) for k in manifest["approval"]["public_keys"]}
    grants = [s["msg"] for s in _session() if s["msg"]["type"] == "exec" and s["msg"].get("approval")]
    assert grants
    for x in grants:
        verify_grant(x["approval"], public_keys=keys, device_id=manifest["device"]["id"],
                     session_id=x["approval"]["session_id"], op=x["op"], args=x["args"],
                     now_s=x["ts"] // 1000, max_ttl_s=300, ledger=GrantLedger())


def test_example_hello_carries_matching_manifest_hash():
    hello = _session()[0]["msg"]
    assert hello["manifest_sha256"] == manifest_sha256(hello["manifest"])


@pytest.mark.parametrize("text", [
    '{"v":1,"type":"exec","id":"x-0001","ts":1759581000000,"run_id":"r","step_id":"s","op":"i2c.read","args":{"bus":1,"addr":64,"reg":254,"len":2.0}}',
    '{"v":1,"type":"exec","id":"x-0001","ts":1759581000000,"run_id":"r","step_id":"s","op":"i2c.read","args":{"bus":1,"addr":64,"reg":254,"len":2e0}}',
    '{"v":1,"type":"ping","id":"p-0001","ts":NaN}',
    '[1,2]',
])
def test_parse_rejects_non_integer_numbers(text):
    # JSON Schema は 2.0 を integer とみなすので、受信時のパーサで弾く
    with pytest.raises(MalformedMessage):
        parse_message(text)


def test_parse_accepts_example_frames():
    for step in _session():
        assert parse_message(json.dumps(step["msg"], ensure_ascii=False)) == step["msg"]


@pytest.mark.parametrize("bad, why", [
    ({"op": "i2c.read", "args": {"bus": 1, "addr": 64, "reg": 254}}, "len がない"),
    ({"op": "i2c.write", "args": {"bus": 1, "addr": 64, "reg": 5, "hex": "0A00"}}, "16進は小文字"),
    ({"op": "i2c.write", "args": {"bus": 1, "addr": 64, "reg": 5, "hex": "a00"}}, "奇数桁の16進"),
    ({"op": "gpio.write", "args": {"line": 17, "value": 2}}, "value は 0 か 1"),
    ({"op": "shell.exec", "args": {"cmd": "rm -rf /"}}, "未定義の op"),
    ({"op": "sys.info", "args": {"x": 1}}, "引数のない op に引数"),
])
def test_malformed_exec_is_rejected(bad, why):
    msg = {"v": 1, "type": "exec", "id": "x-0001", "ts": 1759581000000, "run_id": "run_1", "step_id": "stp_1", **bad}
    assert message_errors(msg), why


def test_result_cannot_be_both_ok_and_error():
    msg = {"v": 1, "type": "result", "id": "r-0001", "ts": 1759581000000, "re": "x-0001", "ok": True, "data": {},
           "error": {"code": "internal", "message": "x"},
           "policy": {"decision": "allow", "rule": "sys.info", "manifest_sha256": "0" * 64}}
    assert message_errors(msg)


def test_canonical_json_rejects_floats_and_sorts_keys():
    assert canonical_json({"b": 1, "a": "む"}) == '{"a":"む","b":1}'.encode()
    with pytest.raises(TypeError):
        canonical_json({"x": 1.5})
