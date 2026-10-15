"""PROTOCOL.md 7章: 承認グラントの検証。"""
from conftest import SESSION, T0_MS, T0_S, check
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mugemuge_agentd.approval import sign_grant

ARGS = {"line": 17, "value": 1, "hold_ms": 1500}


def test_valid_grant_executes_once(gate, sign):
    g = sign("gpio.write", ARGS)
    v = check(gate, "gpio.write", ARGS, g)
    assert v.may_execute and v.decision == "approved" and v.grant_id == g["grant_id"]
    v2 = check(gate, "gpio.write", ARGS, g, now_ms=T0_MS + 1000)
    assert v2.error_code == "approval_invalid" and v2.detail["reason"] == "replayed"


def test_grant_for_other_args_is_rejected(gate, sign):
    g = sign("gpio.write", {"line": 17, "value": 1, "hold_ms": 500})
    v = check(gate, "gpio.write", ARGS, g)
    assert v.detail["reason"] == "args_mismatch"


def test_tampered_grant_is_rejected(gate, sign):
    g = sign("gpio.write", ARGS)
    g["approved_by"] = "user:someone-else"
    assert check(gate, "gpio.write", ARGS, g).detail["reason"] == "bad_signature"


def test_grant_signed_by_unknown_key(gate):
    other = Ed25519PrivateKey.generate()  # 例：Agent サービスが自前の鍵で作ったグラント
    g = sign_grant(other, kid="test-key", device_id="pi-desk-01", session_id=SESSION, op="gpio.write",
                   args=ARGS, approved_by="agent", iat=T0_S)
    assert check(gate, "gpio.write", ARGS, g).detail["reason"] == "bad_signature"
    g2 = sign_grant(other, kid="agent-key", device_id="pi-desk-01", session_id=SESSION, op="gpio.write",
                    args=ARGS, approved_by="agent", iat=T0_S)
    assert check(gate, "gpio.write", ARGS, g2).detail["reason"] == "unknown_kid"


def test_grant_from_previous_session(gate, sign):
    g = sign("gpio.write", ARGS, session_id="ses_OLDSESSION00001")
    assert check(gate, "gpio.write", ARGS, g).detail["reason"] == "wrong_session"


def test_grant_for_other_device(gate, sign):
    g = sign("gpio.write", ARGS, device_id="pi-other-02")
    assert check(gate, "gpio.write", ARGS, g).detail["reason"] == "wrong_device"


def test_expiry_with_clock_skew(gate, sign):
    g = sign("gpio.write", ARGS, ttl_s=60)
    assert check(gate, "gpio.write", ARGS, g, now_ms=(T0_S + 60 + 31) * 1000).detail["reason"] == "expired"
    g = sign("gpio.write", ARGS, ttl_s=60)
    assert check(gate, "gpio.write", ARGS, g, now_ms=(T0_S + 60 + 29) * 1000).may_execute


def test_ttl_longer_than_manifest_allows(gate, sign):
    g = sign("gpio.write", ARGS, ttl_s=301)
    assert check(gate, "gpio.write", ARGS, g).detail["reason"] == "ttl_too_long"


def test_rate_limited_request_does_not_burn_grant(gate, sign):
    for _ in range(10):  # gpio_write バースト = 5/s × 2
        check(gate, "gpio.write", ARGS, sign("gpio.write", ARGS))
    g = sign("gpio.write", ARGS)
    assert check(gate, "gpio.write", ARGS, g).error_code == "rate_limited"
    assert check(gate, "gpio.write", ARGS, g, now_ms=T0_MS + 1000).may_execute


def test_approval_required_response_matches_what_grant_must_cover(gate, sign):
    v = check(gate, "gpio.write", ARGS)
    g = sign(v.request["op"], v.request["args"])
    assert g["args_sha256"] == v.request["args_sha256"]
    assert check(gate, "gpio.write", ARGS, g).may_execute
