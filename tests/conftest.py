import copy
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mugemuge_agentd.approval import public_key_to_b64, sign_grant
from mugemuge_agentd.gate import Gate
from mugemuge_agentd.manifest import parse_manifest
from mugemuge_agentd.policy import Binding, DeviceState

ROOT = Path(__file__).resolve().parents[1]
T0_MS = 1_759_581_000_000
T0_S = T0_MS // 1000
SESSION = "ses_TESTSESSION0001"


@pytest.fixture(scope="session")
def signing_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def raw_manifest(signing_key):
    raw = yaml.safe_load((ROOT / "examples/manifest.pi-grove.yaml").read_text(encoding="utf-8"))
    raw["approval"]["public_keys"] = [{"kid": "test-key", "key": public_key_to_b64(signing_key.public_key())}]
    return raw


@pytest.fixture
def manifest(raw_manifest):
    return parse_manifest(raw_manifest)


@pytest.fixture
def state(manifest):
    return DeviceState(device_id=manifest["device"]["id"], session_id=SESSION)


@pytest.fixture
def gate(manifest, state):
    return Gate(manifest, state, now_ms=T0_MS)


@pytest.fixture
def sign(signing_key, manifest):
    def _sign(op, args, **kw):
        params = dict(
            kid="test-key",
            device_id=manifest["device"]["id"],
            session_id=SESSION,
            op=op,
            args=args,
            approved_by="user:test",
            iat=T0_S,
            ttl_s=300,
        )
        params.update(kw)
        return sign_grant(signing_key, **params)

    return _sign


@pytest.fixture
def ina226_binding():
    return Binding(bus=1, addr=0x40, part="INA226",
                   fingerprint={"reg": 0xFE, "len": 2, "hex": "5449"},
                   write_allow_regs=[0x00, 0x05], last_verified_ms=T0_MS)


def check(gate, op, args, approval=None, now_ms=T0_MS):
    return gate.check(op, args, approval, now_ms=now_ms, device_now_s=now_ms // 1000)


def manifest_variant(raw, mutate):
    r = copy.deepcopy(raw)
    mutate(r)
    return parse_manifest(r)
