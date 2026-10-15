"""PROTOCOL.md 6章のルール解決と、gate による最終判定。"""
from conftest import T0_MS, check, manifest_variant

from mugemuge_agentd.gate import Gate
from mugemuge_agentd.policy import Binding, DeviceState, Job


# ---- I2C: 未知・予約・未掲載 ---------------------------------------------

def test_unknown_address_read_is_automatic(gate):
    v = check(gate, "i2c.read", {"bus": 1, "addr": 0x40, "reg": 0xFE, "len": 2})
    assert v.may_execute and v.decision == "allow" and v.rule == "i2c.bus1.unknown.read"


def test_16bit_register_read_is_treated_as_write(gate):
    """SHT3x などでは 16 ビットの reg がコマンドになる（0x30A2 ソフトリセットなど）。未知アドレスでは承認が必要。"""
    v = check(gate, "i2c.read", {"bus": 1, "addr": 0x44, "reg": 0x30A2, "len": 1, "reg_width": 16})
    assert v.error_code == "approval_required" and v.rule == "i2c.bus1.unknown.write"
    assert "コマンド 0x30a2" in v.request["summary"]
    # reg を書かない読み出しは 16 ビット指定でも普通の読み出し
    v = check(gate, "i2c.read", {"bus": 1, "addr": 0x44, "len": 6, "reg_width": 16})
    assert v.may_execute and v.rule == "i2c.bus1.unknown.read"


def test_16bit_register_read_on_bound_device_uses_write_allow_regs(gate, state):
    state.bindings[(1, 0x44)] = Binding(bus=1, addr=0x44, part="SHT30",
                                        fingerprint={"reg": 0x3780, "len": 6, "hex": "0102030405060708"},
                                        write_allow_regs=[0xE000], last_verified_ms=T0_MS)
    v = check(gate, "i2c.read", {"bus": 1, "addr": 0x44, "reg": 0xE000, "len": 6, "reg_width": 16})
    assert v.may_execute and v.rule == "i2c.bus1.bound.write"
    v = check(gate, "i2c.read", {"bus": 1, "addr": 0x44, "reg": 0x306D, "len": 1, "reg_width": 16})
    assert v.error_code == "approval_required"


def test_sample_start_with_16bit_command_needs_approval_unless_bound(gate):
    args = {"period_ms": 1000, "duration_ms": 5000,
            "reads": [{"name": "rht", "bus": 1, "addr": 0x44, "reg": 0xE000, "len": 6, "reg_width": 16}]}
    assert check(gate, "sample.start", args).error_code == "approval_required"


def test_unknown_address_write_needs_approval(gate):
    v = check(gate, "i2c.write", {"bus": 1, "addr": 0x40, "reg": 0x00, "hex": "4127"})
    assert v.error_code == "approval_required"
    assert v.request["summary"].startswith("bus1 0x40")
    assert v.request["session_id"] == "ses_TESTSESSION0001"


def test_reserved_address_is_denied_even_for_reads(gate):
    v = check(gate, "i2c.read", {"bus": 1, "addr": 0x03, "reg": 0, "len": 1})
    assert v.error_code == "policy_denied" and v.detail["reason"] == "i2c.reserved_address"


def test_unlisted_bus_is_denied(gate):
    # i2c-0（HAT ID EEPROM のバス）はマニフェストにないので拒否
    v = check(gate, "i2c.read", {"bus": 0, "addr": 0x50, "reg": 0, "len": 1})
    assert v.error_code == "policy_denied" and v.detail["reason"] == "i2c.bus_not_listed"


def test_read_length_limit(gate):
    v = check(gate, "i2c.read", {"bus": 1, "addr": 0x40, "reg": 0, "len": 33})
    assert v.error_code == "policy_denied" and v.detail["reason"] == "limits.max_read_len"


def test_reg_must_fit_width(gate):
    v = check(gate, "i2c.read", {"bus": 1, "addr": 0x40, "reg": 0x1234, "len": 2})
    assert v.error_code == "bad_request"
    v = check(gate, "i2c.read", {"bus": 1, "addr": 0x40, "reg": 0x1234, "len": 2, "reg_width": 16})
    assert v.error_code == "approval_required"   # 幅には収まる。16 ビットの reg は書き込みとして判定（2a）


# ---- I2C: 固定ルール（むげむげ HAT の PMCU） -----------------------------

def _with_pmcu(r):
    r["i2c"]["buses"][0]["addresses"] = [
        {"addr": 0x08, "label": "pmcu", "protected": True, "read": "allow", "write": "deny", "write_allow_regs": [0x20]}
    ]


def test_protected_address_cannot_be_bound(raw_manifest, state):
    g = Gate(manifest_variant(raw_manifest, _with_pmcu), state, T0_MS)
    v = check(g, "i2c.bind", {"bus": 1, "addr": 0x08, "part": "BME280",
                              "fingerprint": {"reg": 0xD0, "len": 1, "hex": "60"}, "write_allow_regs": [0]})
    assert v.error_code == "policy_denied" and v.detail["reason"] == "i2c.protected_address"


def test_fixed_rule_write_allow_regs_and_deny(raw_manifest, state, sign):
    g = Gate(manifest_variant(raw_manifest, _with_pmcu), state, T0_MS)
    assert check(g, "i2c.write", {"bus": 1, "addr": 0x08, "reg": 0x20, "hex": "01"}).may_execute
    args = {"bus": 1, "addr": 0x08, "reg": 0x21, "hex": "01"}
    v = check(g, "i2c.write", args, sign("i2c.write", args))
    # 承認があっても deny は覆らない
    assert v.error_code == "policy_denied"


def test_unprotected_fixed_rule_yields_to_binding(raw_manifest, state, ina226_binding):
    def unprotected(r):
        r["i2c"]["buses"][0]["addresses"] = [
            {"addr": 0x40, "label": "shunt", "protected": False, "read": "allow", "write": "deny"}]
    g = Gate(manifest_variant(raw_manifest, unprotected), state, T0_MS)
    args = {"bus": 1, "addr": 0x40, "reg": 0x05, "hex": "0a00"}
    assert check(g, "i2c.write", args).error_code == "policy_denied"      # バインド前は固定ルール
    state.bindings[(1, 0x40)] = ina226_binding
    assert check(g, "i2c.write", args, now_ms=T0_MS + 1).may_execute       # バインド後はバインドのルール


# ---- I2C: バインド --------------------------------------------------------

def test_bound_device_allowed_regs_write_automatically(gate, state, ina226_binding):
    state.bindings[(1, 0x40)] = ina226_binding
    v = check(gate, "i2c.write", {"bus": 1, "addr": 0x40, "reg": 0x05, "hex": "0a00"}, now_ms=T0_MS + 100)
    assert v.may_execute and v.rule == "i2c.bus1.bound.write"
    assert v.fingerprint_check is None  # 100ms 前に確認済み


def test_bound_device_write_rechecks_stale_fingerprint(gate, state, ina226_binding):
    state.bindings[(1, 0x40)] = ina226_binding
    v = check(gate, "i2c.write", {"bus": 1, "addr": 0x40, "reg": 0x05, "hex": "0a00"}, now_ms=T0_MS + 5000)
    assert v.may_execute and v.fingerprint_check == (1, 0x40)


def test_bound_device_other_regs_need_approval(gate, state, ina226_binding):
    state.bindings[(1, 0x40)] = ina226_binding
    v = check(gate, "i2c.write", {"bus": 1, "addr": 0x40, "reg": 0x06, "hex": "0000"})
    assert v.error_code == "approval_required" and "INA226" in v.request["summary"]


def test_bind_needs_approval_and_limits_regs(gate):
    args = {"bus": 1, "addr": 0x40, "part": "INA226",
            "fingerprint": {"reg": 0xFE, "len": 2, "hex": "5449"}, "write_allow_regs": [0, 5]}
    v = check(gate, "i2c.bind", args)
    assert v.error_code == "approval_required" and "INA226 として扱う" in v.request["summary"]
    args["write_allow_regs"] = list(range(9))
    assert check(gate, "i2c.bind", args).error_code == "policy_denied"


# ---- GPIO -----------------------------------------------------------------

def test_gpio_active_write_needs_approval(gate):
    v = check(gate, "gpio.write", {"line": 17, "value": 1, "hold_ms": 1500})
    assert v.error_code == "approval_required"
    assert v.request["summary"] == "relay-1（GPIO17）を 1.5秒 1 にする"


def test_gpio_safe_value_always_allowed(gate):
    v = check(gate, "gpio.write", {"line": 17, "value": 0})
    assert v.may_execute and v.rule == "gpio.17.write.safe"


def test_gpio_hold_over_max_on_is_denied_even_with_approval(gate, sign):
    args = {"line": 17, "value": 1, "hold_ms": 5000}
    v = check(gate, "gpio.write", args, sign("gpio.write", args))
    assert v.error_code == "policy_denied" and v.detail["reason"] == "gpio.hold_exceeds_max_on"


def test_gpio_unlisted_and_input_lines(gate):
    assert check(gate, "gpio.write", {"line": 14, "value": 1}).error_code == "policy_denied"   # UART TX
    assert check(gate, "gpio.write", {"line": 27, "value": 0}).error_code == "policy_denied"   # 入力ピン
    assert check(gate, "gpio.read", {"line": 27}).may_execute


# ---- 計測ジョブ -----------------------------------------------------------

READS = [{"name": "bus_v", "bus": 1, "addr": 0x40, "reg": 2, "len": 2},
         {"name": "current", "bus": 1, "addr": 0x40, "reg": 4, "len": 2}]


def test_sample_start_all_reads_allowed(gate):
    v = check(gate, "sample.start", {"period_ms": 100, "duration_ms": 10000, "reads": READS})
    assert v.may_execute and v.rule == "sample.start"


def test_sample_start_takes_strictest_read(gate):
    reads = READS + [{"name": "bad", "bus": 1, "addr": 0x7A, "reg": 0, "len": 1}]
    v = check(gate, "sample.start", {"period_ms": 100, "duration_ms": 10000, "reads": reads})
    assert v.error_code == "policy_denied" and v.detail["reason"] == "i2c.reserved_address"


def test_sample_rate_includes_running_jobs(gate, state):
    state.jobs["job_1"] = Job("job_1", reads_per_s=150)
    # 2 reads / 10ms = 200/s、実行中の 150/s と合わせて 350/s > 200/s
    v = check(gate, "sample.start", {"period_ms": 10, "duration_ms": 1000, "reads": READS})
    assert v.error_code == "policy_denied" and v.detail["reason"] == "sample.read_rate_exceeded"


def test_sample_period_and_duration_limits(gate):
    assert check(gate, "sample.start", {"period_ms": 5, "duration_ms": 1000, "reads": READS}).error_code == "policy_denied"
    assert check(gate, "sample.start", {"period_ms": 100, "duration_ms": 600001, "reads": READS}).error_code == "policy_denied"


def test_sample_duplicate_names_is_bad_request(gate):
    v = check(gate, "sample.start", {"period_ms": 100, "duration_ms": 1000, "reads": [READS[0], READS[0]]})
    assert v.error_code == "bad_request"


# ---- 非常停止・レート ----------------------------------------------------

def test_estop_blocks_actions_but_not_reads_or_safe_ops(gate, state):
    state.estop = True
    assert check(gate, "i2c.read", {"bus": 1, "addr": 0x40, "reg": 0, "len": 2}).may_execute
    assert check(gate, "gpio.write", {"line": 17, "value": 0}).may_execute
    assert check(gate, "sample.stop", {"job_id": "job_1"}).may_execute
    assert check(gate, "sample.start", {"period_ms": 100, "duration_ms": 1000, "reads": READS}).error_code == "estop_active"
    assert check(gate, "gpio.write", {"line": 17, "value": 1}).error_code == "estop_active"
    assert check(gate, "sys.estop_clear", {}).error_code == "approval_required"


def test_write_rate_limit_then_recovers(gate, state, ina226_binding):
    state.bindings[(1, 0x40)] = ina226_binding
    args = {"bus": 1, "addr": 0x40, "reg": 0x05, "hex": "0a00"}
    results = [check(gate, "i2c.write", args, now_ms=T0_MS + 1) for _ in range(41)]
    assert all(r.may_execute for r in results[:40])          # バースト = 20/s × 2
    assert results[40].error_code == "rate_limited" and results[40].retry_after_ms > 0
    assert check(gate, "i2c.write", args, now_ms=T0_MS + 1000).may_execute


def test_safe_ops_ignore_rate_limit(gate):
    for _ in range(100):
        assert check(gate, "gpio.write", {"line": 17, "value": 0}).may_execute
    assert check(gate, "sys.estop", {}).may_execute


def test_unknown_op_is_bad_request(gate):
    assert check(gate, "pico.flash", {}).error_code == "bad_request"


def test_approval_summaries_never_round_durations(gate):
    """承認カードは人の判断の根拠なので、期間・間隔を丸めない（SPEC_REVIEW.md B2）。"""
    from mugemuge_agentd.policy import fmt_ms
    assert [fmt_ms(x) for x in (500, 1000, 1500, 30_000, 90_000, 3_600_000, 5_400_000, 172_800_000, 3_723_456)] == [
        "500ミリ秒", "1秒", "1.5秒", "30秒", "1分30秒", "1時間", "1時間30分", "48時間", "1時間2分3.456秒"]
    reads = [{"name": "v", "bus": 1, "addr": 0x40, "reg": 2, "len": 2}]
    v = check(gate, "sample.start", {"period_ms": 100, "duration_ms": 500, "reads": reads})
    assert v.may_execute
    d = gate.policy.evaluate("sample.start", {"period_ms": 100, "duration_ms": 500, "reads": reads}, gate.state)
    assert d.summary == "100ミリ秒ごとに 500ミリ秒計測する（v）"
    d = gate.policy.evaluate("log.start", {"name": "x", "interval_ms": 1500, "duration_ms": 5_400_000, "reads": reads},
                             gate.state)
    assert "1.5秒ごとに 1時間30分記録する" in d.summary


def test_16bit_read_counts_toward_read_rate_not_write_rate(gate, state, ina226_binding):
    """16ビットの reg の読み出しは、判定は write のルール、レートは i2c_read_per_s で数える（F4）。"""
    from mugemuge_agentd.policy import Binding
    state.bindings[(1, 0x40)] = ina226_binding
    state.bindings[(1, 0x44)] = Binding(1, 0x44, "SHT30", {"reg": 0x3780, "len": 6, "hex": "00" * 6, "recheck": False},
                                        [0xE000], last_verified_ms=T0_MS)
    fetch = {"bus": 1, "addr": 0x44, "reg": 0xE000, "reg_width": 16, "len": 6}
    d = gate.policy.evaluate("i2c.read", fetch, state)
    assert d.i2c_reads == 1 and d.i2c_writes == 0
    # 30回読むと読み出しの枠だけが減り、書き込みの枠（20/s × 2 = 40）は丸ごと残る
    assert all(check(gate, "i2c.read", fetch, now_ms=T0_MS + 1).may_execute for _ in range(30))
    assert gate.buckets["i2c_read"].tokens == gate.buckets["i2c_read"].capacity - 30
    assert gate.buckets["i2c_write"].tokens == gate.buckets["i2c_write"].capacity


def test_no_recheck_binding_requires_manifest_permission(raw_manifest, state):
    """recheck: false のバインドは、マニフェストの bound.allow_no_recheck が true のときだけ通る（I1）。"""
    import copy

    from mugemuge_agentd.manifest import parse_manifest
    from mugemuge_agentd.policy import Level, Policy
    args = {"bus": 1, "addr": 0x44, "part": "SHT30", "write_allow_regs": [0xE000],
            "fingerprint": {"reg": 0x3780, "len": 6, "hex": "00" * 6, "reg_width": 16, "recheck": False}}
    raw = copy.deepcopy(raw_manifest)
    raw["i2c"]["buses"][0].setdefault("bound", {})["allow_no_recheck"] = False
    d = Policy(parse_manifest(raw)).evaluate("i2c.bind", args, state)
    assert d.level is Level.DENY and d.reason == "i2c.no_recheck_not_allowed"
    # 読み直すバインドは今までどおり承認で通る
    ok = Policy(parse_manifest(raw)).evaluate("i2c.bind", {**args, "fingerprint": {**args["fingerprint"], "recheck": True}}, state)
    assert ok.level is Level.APPROVAL
    raw["i2c"]["buses"][0]["bound"]["allow_no_recheck"] = True
    d = Policy(parse_manifest(raw)).evaluate("i2c.bind", args, state)
    assert d.level is Level.APPROVAL and "書き込み前の指紋の確認をしない" in d.summary
    # 既定は false
    raw["i2c"]["buses"][0]["bound"].pop("allow_no_recheck")
    assert Policy(parse_manifest(raw)).evaluate("i2c.bind", args, state).reason == "i2c.no_recheck_not_allowed"
