"""実行部: gate を通ったものだけを実行すること、二重実行の防止、直列化、指紋、計測ジョブ。"""
import asyncio
import re
import errno
import itertools
import json

import pytest
from conftest import SESSION, T0_S

from mugemuge_agentd.audit import AuditLog, verify_chain
from mugemuge_agentd.clock import Clock
from mugemuge_agentd.executor import Executor
from mugemuge_agentd.gate import Gate
from mugemuge_agentd.hw.gpio import FakeGPIO
from mugemuge_agentd.hw.i2c import FakeI2C, bme280
from mugemuge_agentd.messages import message_errors
from mugemuge_agentd.policy import Binding
from mugemuge_agentd.safety import Safety
from mugemuge_agentd.sampler import Sampler

_ids = itertools.count(1)


class Rig:
    def __init__(self, manifest, state, tmp_path):
        self.clock = Clock()
        self.clock.offset_ms = T0_S * 1000 - self.clock.now_ms()   # 承認の iat/exp をテストの時刻に合わせる
        self.out = []
        self.i2c = FakeI2C()
        self.gpio = FakeGPIO()
        state.session_id = SESSION
        self.state = state
        self.gate = Gate(manifest, state, now_ms=self.clock.mono_ms())
        self.safety = Safety(manifest, state, self.gpio, self.clock, self.out.append)
        self.safety.init_outputs()
        self.sampler = Sampler(state, self.i2c, self.clock, self.out.append)
        self.safety.stop_jobs = self.sampler.stop_all
        self.audit_path = tmp_path / "audit.jsonl"
        self.ex = Executor(manifest, state, self.gate, self.i2c, self.gpio, self.safety, self.sampler,
                           AuditLog(self.audit_path), self.clock, self.out.append, "0.1.0")

    def msg(self, op, args, approval=None, xid=None, **kw):
        m = {"v": 1, "type": "exec", "id": xid or f"x-test-{next(_ids):06d}", "ts": self.clock.now_ms(),
             "op": op, "args": args, "run_id": "run_test", "step_id": "stp_1", **kw}
        if approval is not None:
            m["approval"] = approval
        assert message_errors(m) == []
        return m

    async def run(self, op, args, approval=None, **kw):
        r = await self.ex.handle(self.msg(op, args, approval, **kw))
        assert message_errors(r) == [], r
        return r


@pytest.fixture
def rig(manifest, state, tmp_path):
    return Rig(manifest, state, tmp_path)


BIND = {"bus": 1, "addr": 64, "part": "INA226", "fingerprint": {"reg": 0xFE, "len": 2, "hex": "5449"},
        "write_allow_regs": [0, 5]}


def code(r):
    return r["error"]["code"] if not r["ok"] else r["policy"]["decision"]


def test_session_flow(rig, sign):
    """session.json の流れ: scan → read → bind 要承認 → 承認付き bind → write → sample → 注入の拒否。"""
    async def go():
        r = await rig.run("i2c.scan", {"bus": 1})
        assert r["data"] == {"bus": 1, "found": [64, 118], "method": "read_byte"}
        assert (await rig.run("i2c.read", {"bus": 1, "addr": 64, "reg": 0xFE, "len": 2}))["data"] == {"hex": "5449"}
        assert (await rig.run("i2c.read", {"bus": 1, "addr": 64, "reg": 0xFF, "len": 2}))["data"] == {"hex": "2260"}

        r = await rig.run("i2c.bind", BIND)
        assert code(r) == "approval_required"
        req = r["error"]["request"]
        assert (1, 64) not in rig.state.bindings
        grant = sign(req["op"], req["args"])
        r = await rig.run("i2c.bind", BIND, approval=grant)
        assert r["ok"] and r["policy"]["decision"] == "approved" and r["data"] == {"bound": True}
        assert rig.state.bindings[(1, 64)].part == "INA226"

        r = await rig.run("i2c.write", {"bus": 1, "addr": 64, "reg": 5, "hex": "0a00"})
        assert r["ok"] and r["policy"]["rule"] == "i2c.bus1.bound.write" and r["data"] == {"written": 2}
        assert rig.i2c.devices[1][64].regs[5].hex() == "0a00"

        r = await rig.run("sample.start", {"period_ms": 50, "duration_ms": 500, "batch_ms": 200, "reads": [
            {"name": "bus_v", "bus": 1, "addr": 64, "reg": 2, "len": 2},
            {"name": "current", "bus": 1, "addr": 64, "reg": 4, "len": 2}]})
        assert r["ok"] and r["data"]["columns"] == ["t_ms", "bus_v", "current"]
        await rig.sampler.wait_all()
        samples = [m for m in rig.out if m["type"] == "samples"]
        assert [s["seq"] for s in samples] == list(range(len(samples)))
        rows = [row for s in samples for row in s["rows"]]
        assert len(rows) == 10 and rows[0] == [0, "0a14", "04b0"] and rows[-1][0] == 450
        end = [m for m in rig.out if m["type"] == "event"][-1]
        assert end["event"] == "job_end" and end["detail"] == {"job_id": r["data"]["job_id"], "reason": "completed", "rows": 10}
        assert re.fullmatch(r"job_[0-9a-z]{8}_1", r["data"]["job_id"])
        for m in rig.out:
            assert message_errors(m) == [], m
        assert rig.state.jobs == {}

        r = await rig.run("gpio.write", {"line": 17, "value": 1, "hold_ms": 5000})
        assert code(r) == "policy_denied"
        r = await rig.run("i2c.write", {"bus": 0, "addr": 0x50, "reg": 0, "hex": "ff"})
        assert code(r) == "policy_denied"
        assert rig.gpio.values[17] == 0

        # 監査ログに全 exec が鎖つきで残る
        lines = [json.loads(x) for x in rig.audit_path.read_text().splitlines()]
        assert [x["op"] for x in lines] == ["i2c.scan", "i2c.read", "i2c.read", "i2c.bind", "i2c.bind", "i2c.write",
                                            "sample.start", "gpio.write", "i2c.write"]
        assert lines[4]["decision"] == "approved" and lines[4]["grant_id"] == grant["grant_id"]
        assert verify_chain([rig.audit_path]) == 9
    asyncio.run(go())


def test_grant_accepted_after_wall_clock_step(rig, sign, monkeypatch):
    """welcome のあとで NTP が壁時計を15分進めても、承認は expired にならない（RTC のない Pi Zero 2 W で起きた）。"""
    async def go():
        real_time = __import__("time").time
        monkeypatch.setattr("mugemuge_agentd.clock.time.time", lambda: real_time() + 900)
        r = await rig.run("i2c.bind", BIND)
        assert code(r) == "approval_required"
        grant = sign("i2c.bind", r["error"]["request"]["args"])
        r = await rig.run("i2c.bind", BIND, approval=grant)
        assert r["ok"] and r["policy"]["decision"] == "approved", r
    asyncio.run(go())


def test_grant_still_expires_by_monotonic_clock(rig, sign, monkeypatch):
    """単調時計が 400 秒進めば、承認（TTL 300 秒＋許容 30 秒）は expired になる。期限が永遠に切れない形で壊れていないこと。"""
    async def go():
        r = await rig.run("i2c.bind", BIND)
        grant = sign("i2c.bind", r["error"]["request"]["args"])
        real_monotonic = __import__("time").monotonic
        monkeypatch.setattr("mugemuge_agentd.clock.time.monotonic", lambda: real_monotonic() + 400)
        r = await rig.run("i2c.bind", BIND, approval=grant)
        assert code(r) == "approval_invalid" and r["error"]["detail"] == {"reason": "expired"}, r
        assert (1, 64) not in rig.state.bindings
    asyncio.run(go())


def test_wall_clock_step_back_does_not_stall_rate_limit(rig, monkeypatch):
    """壁時計が1時間戻っても、レート上限のトークンは単調時計で補充される（壁時計で測ると1時間止まっていた: P3）。"""
    from mugemuge_agentd.ratelimit import TokenBucket

    async def go():
        rig.gate.buckets["exec"] = TokenBucket.create(5, rig.clock.mono_ms())       # 毎秒5、バースト10
        for _ in range(10):
            assert (await rig.run("sys.info", {}))["ok"]
        assert code(await rig.run("sys.info", {})) == "rate_limited"
        real_time = __import__("time").time
        monkeypatch.setattr("mugemuge_agentd.clock.time.time", lambda: real_time() - 3600)
        await asyncio.sleep(0.5)                                                   # 2.5 トークン分
        assert (await rig.run("sys.info", {}))["ok"]
    asyncio.run(go())


def test_fingerprint_recheck_interval_ignores_wall_clock(rig, sign, monkeypatch):
    """指紋を確かめた時刻は単調時計。壁時計が1時間戻っても、読み直しの間隔（1秒）が延びない（P3）。"""
    async def go():
        r = await rig.run("i2c.bind", BIND)
        assert (await rig.run("i2c.bind", BIND, approval=sign("i2c.bind", r["error"]["request"]["args"])))["ok"]
        real_time = __import__("time").time
        monkeypatch.setattr("mugemuge_agentd.clock.time.time", lambda: real_time() - 3600)
        rig.i2c.devices[1][64] = bme280()     # 登録のあとで部品が差し替えられた
        await asyncio.sleep(1.1)              # fingerprint_recheck_ms（1000）を過ぎた
        r = await rig.run("i2c.write", {"bus": 1, "addr": 64, "reg": 5, "hex": "0a00"})
        assert code(r) == "fingerprint_mismatch" and (1, 64) not in rig.state.bindings
    asyncio.run(go())


def test_relay_needs_approval_and_turns_off(rig, sign, manifest):
    async def go():
        manifest["gpio"]["lines"][0]["max_on_ms"] = 200
        args = {"line": 17, "value": 1}
        r = await rig.run("gpio.write", args)
        assert code(r) == "approval_required" and rig.gpio.values[17] == 0
        r = await rig.run("gpio.write", args, approval=sign("gpio.write", args))
        assert r["ok"] and r["data"]["value"] == 1 and rig.gpio.values[17] == 1
        await asyncio.sleep(0.35)
        assert rig.gpio.values[17] == 0
        assert [m["event"] for m in rig.out if m["type"] == "event"] == ["output_reverted"]
    asyncio.run(go())


def test_grant_is_single_use(rig, sign):
    async def go():
        args = {"line": 17, "value": 1, "hold_ms": 100}
        g = sign("gpio.write", args)
        assert (await rig.run("gpio.write", args, approval=g))["ok"]
        r = await rig.run("gpio.write", args, approval=g)
        assert code(r) == "approval_invalid" and r["error"]["detail"]["reason"] == "replayed"
    asyncio.run(go())


def test_duplicate_exec_id_is_not_executed_twice(rig):
    async def go():
        m = rig.msg("i2c.write", {"bus": 1, "addr": 0x76, "reg": 0xF4, "hex": "27"}, xid="x-dup-000001")
        # 未知アドレスへの書き込みは要承認なので、書き込みが通る bound アドレスで試す
        rig.state.bindings[(1, 0x76)] = Binding(1, 0x76, "BME280", {"reg": 0xD0, "len": 1, "hex": "60"}, [0xF4],
                                                last_verified_ms=rig.clock.mono_ms())
        r1 = await rig.ex.handle(m)
        r2 = await rig.ex.handle(m)
        assert r1["ok"] and r2["ok"] and r2["duplicate"] is True and "duplicate" not in r1
        assert r2["re"] == r1["re"] and r2["id"] != r1["id"]
        assert len(rig.i2c.devices[1][0x76].writes) == 1
        assert message_errors(r2) == []
    asyncio.run(go())


def test_concurrent_duplicate_waits_for_first(rig):
    async def go():
        m = rig.msg("i2c.scan", {"bus": 1}, xid="x-dup-000002")
        r1, r2 = await asyncio.gather(rig.ex.handle(m), rig.ex.handle(m))
        assert r1["data"] == r2["data"] and r2.get("duplicate") is True
    asyncio.run(go())


def test_fingerprint_recheck_mismatch_drops_binding(rig):
    async def go():
        rig.state.bindings[(1, 64)] = Binding(1, 64, "INA226", {"reg": 0xFE, "len": 2, "hex": "5449"}, [5],
                                              last_verified_ms=0)
        rig.i2c.devices[1][64] = bme280()     # 計測中に部品が差し替えられた
        r = await rig.run("i2c.write", {"bus": 1, "addr": 64, "reg": 5, "hex": "0a00"})
        assert code(r) == "fingerprint_mismatch"
        assert r["policy"]["decision"] == "allow"
        assert (1, 64) not in rig.state.bindings
        assert rig.i2c.devices[1][64].writes == []
        ev = [m for m in rig.out if m["type"] == "event"]
        assert ev[0]["event"] == "binding_dropped" and ev[0]["detail"]["expected"] == "5449"
        assert message_errors(ev[0]) == []
    asyncio.run(go())


def test_fingerprint_recheck_match_updates_timestamp(rig):
    async def go():
        b = Binding(1, 64, "INA226", {"reg": 0xFE, "len": 2, "hex": "5449"}, [5], last_verified_ms=0)
        rig.state.bindings[(1, 64)] = b
        r = await rig.run("i2c.write", {"bus": 1, "addr": 64, "reg": 5, "hex": "0a00"})
        assert r["ok"] and b.last_verified_ms > 0
    asyncio.run(go())


def test_bind_reads_fingerprint(rig, sign):
    async def go():
        args = {**BIND, "fingerprint": {"reg": 0xFE, "len": 2, "hex": "1234"}}
        r = await rig.run("i2c.bind", args, approval=sign("i2c.bind", args))
        assert code(r) == "fingerprint_mismatch" and (1, 64) not in rig.state.bindings
    asyncio.run(go())


def test_io_error_reports_errno(rig):
    async def go():
        r = await rig.run("i2c.read", {"bus": 1, "addr": 0x41, "reg": 0, "len": 1})
        assert code(r) == "io_error" and r["error"]["detail"]["errno"] == "ENXIO"
        assert r["policy"]["decision"] == "allow"
        rig.i2c.fail_next[(1, 64)] = errno.EREMOTEIO
        r = await rig.run("i2c.read", {"bus": 1, "addr": 64, "reg": 0, "len": 2})
        assert r["error"]["detail"]["errno"] == "EREMOTEIO"
    asyncio.run(go())


def test_busy_when_queue_is_full(rig, manifest):
    async def go():
        rig.ex.queue_depth = 2
        lock = rig.ex.bus_lock(1)
        await lock.acquire()               # バスを塞いでおく
        tasks = [asyncio.create_task(rig.run("i2c.read", {"bus": 1, "addr": 64, "reg": 2, "len": 2}))
                 for _ in range(3)]
        await asyncio.sleep(0.05)
        lock.release()
        results = await asyncio.gather(*tasks)
        assert sorted(code(r) for r in results) == ["allow", "allow", "busy"]
    asyncio.run(go())


def test_timeout_when_bus_is_held_past_deadline(rig):
    async def go():
        lock = rig.ex.bus_lock(1)
        await lock.acquire()
        t = asyncio.create_task(rig.run("i2c.read", {"bus": 1, "addr": 64, "reg": 2, "len": 2}, deadline_ms=100))
        await asyncio.sleep(0.2)
        lock.release()
        assert code(await t) == "timeout"
    asyncio.run(go())


def test_denied_and_safe_ops_do_not_wait_for_queue(rig):
    async def go():
        lock = rig.ex.bus_lock(1)
        await lock.acquire()
        r = await asyncio.wait_for(rig.run("i2c.write", {"bus": 0, "addr": 0x50, "reg": 0, "hex": "ff"}), 0.5)
        assert code(r) == "policy_denied"
        r = await asyncio.wait_for(rig.run("i2c.unbind", {"bus": 1, "addr": 64}), 0.5)
        assert r["ok"]
        r = await asyncio.wait_for(rig.run("sys.estop", {}), 0.5)
        assert r["ok"] and rig.state.estop
        lock.release()
    asyncio.run(go())


def test_estop_blocks_writes_and_stops_jobs(rig, sign):
    async def go():
        r = await rig.run("sample.start", {"period_ms": 50, "duration_ms": 5000, "reads": [
            {"name": "v", "bus": 1, "addr": 64, "reg": 2, "len": 2}]})
        assert r["ok"]
        await asyncio.sleep(0.1)
        assert (await rig.run("sys.estop", {}))["ok"]
        await rig.sampler.wait_all()
        ends = [m for m in rig.out if m.get("event") == "job_end"]
        assert ends[0]["detail"]["reason"] == "estop"
        args = {"line": 17, "value": 1, "hold_ms": 100}
        assert code(await rig.run("gpio.write", args, approval=sign("gpio.write", args))) == "estop_active"
        assert (await rig.run("i2c.read", {"bus": 1, "addr": 64, "reg": 2, "len": 2}))["ok"]   # 読み出しは通る
        assert code(await rig.run("sys.estop_clear", {})) == "approval_required"
        r = await rig.run("sys.estop_clear", {}, approval=sign("sys.estop_clear", {}))
        assert r["ok"] and not rig.state.estop
        cleared = [m for m in rig.out if m.get("event") == "estop_cleared"]
        assert cleared[0]["detail"]["approved_by"] == "user:test"
    asyncio.run(go())


def test_sample_stop(rig):
    async def go():
        r = await rig.run("sample.start", {"period_ms": 20, "duration_ms": 5000, "batch_ms": 100, "reads": [
            {"name": "v", "bus": 1, "addr": 64, "reg": 2, "len": 2}]})
        await asyncio.sleep(0.15)
        r = await rig.run("sample.stop", {"job_id": r["data"]["job_id"]})
        assert r["data"] == {"stopped": True}
        await rig.sampler.wait_all()
        end = [m for m in rig.out if m.get("event") == "job_end"][0]
        assert end["detail"]["reason"] == "stopped" and end["detail"]["rows"] > 0
        assert (await rig.run("sample.stop", {"job_id": "job_99"}))["data"] == {"stopped": False}
    asyncio.run(go())


def test_sample_read_errors_become_null(rig):
    async def go():
        r = await rig.run("sample.start", {"period_ms": 50, "duration_ms": 150, "reads": [
            {"name": "v", "bus": 1, "addr": 64, "reg": 2, "len": 2},
            {"name": "gone", "bus": 1, "addr": 0x41, "reg": 0, "len": 1}]})
        assert r["ok"]
        await rig.sampler.wait_all()
        s = [m for m in rig.out if m["type"] == "samples"]
        rows = [row for m in s for row in m["rows"]]
        assert all(row[2] is None for row in rows) and sum(m["errors"] for m in s) == len(rows)
    asyncio.run(go())


def test_sys_info(rig):
    async def go():
        r = await rig.run("sys.info", {})
        assert r["data"]["buses"] == [1] and r["data"]["gpio_lines"] == [17, 27] and r["data"]["estop"] is False
    asyncio.run(go())


def test_job_cancelled_before_first_tick_still_ends(rig):
    async def go():
        data = rig.sampler.start({"period_ms": 50, "duration_ms": 1000, "reads": [
            {"name": "v", "bus": 1, "addr": 64, "reg": 2, "len": 2}]})
        assert rig.sampler.stop_all("estop") == [data["job_id"]]   # 同じ tick のうちに止める
        await rig.sampler.wait_all()
        await asyncio.sleep(0)
        ends = [m for m in rig.out if m.get("event") == "job_end"]
        assert len(ends) == 1 and ends[0]["detail"]["reason"] == "estop"
        assert rig.state.jobs == {}
    asyncio.run(go())


def test_sht30_with_recheck_false_can_leave_periodic_mode(manifest, state, tmp_path):
    """定期計測中の SHT30 は指紋のコマンドを受け付けない。recheck: false のバインドなら読み直さずに書ける（B3）。"""
    from mugemuge_agentd.hw.virtual import Environment, virtual_i2c

    async def go():
        r = Rig(manifest, state, tmp_path)
        bus = virtual_i2c(Environment())
        r.ex.i2c = bus
        serial = bus.read(1, 0x44, 0x3780, 6, 16).hex()
        fp = {"reg": 0x3780, "len": 6, "hex": serial, "reg_width": 16, "recheck": False}
        d = r.gate.policy.evaluate("i2c.bind", {"bus": 1, "addr": 0x44, "part": "SHT30", "fingerprint": fp,
                                                "write_allow_regs": [0x22, 0x30, 0xE000]}, r.state)
        assert "書き込み前の指紋の確認をしない" in d.summary
        r.state.bindings[(1, 0x44)] = Binding(1, 0x44, "SHT30", fp, [0x22, 0x30, 0xE000], last_verified_ms=0)
        bus.write(1, 0x44, 0x22, bytes.fromhex("36"))                    # 定期計測へ
        res = await r.run("i2c.write", {"bus": 1, "addr": 0x44, "reg": 0x30, "hex": "93"})   # break
        assert res["ok"] and (1, 0x44) in r.state.bindings
        assert not bus.devices[1][0x44].periodic
        # recheck を省略したバインドは今までどおり読み直す（部品の差し替えに気づく）
        r.state.bindings[(1, 0x44)] = Binding(1, 0x44, "SHT30", {"reg": 0x3780, "len": 6, "hex": serial}, [0x22],
                                              last_verified_ms=0)
        bus.write(1, 0x44, 0x22, bytes.fromhex("36"))
        res = await r.run("i2c.write", {"bus": 1, "addr": 0x44, "reg": 0x22, "hex": "36"})
        assert res["error"]["code"] == "fingerprint_mismatch"
    asyncio.run(go())


def test_fingerprint_reg_width_is_honoured(rig):
    """指紋の reg_width が書いてあれば、reg が 0xFF 以下でもその幅で読む（B7）。"""
    calls = []
    orig = rig.i2c.read

    def spy(bus, addr, reg, length, reg_width=8):
        calls.append((reg, reg_width))
        return orig(bus, addr, reg, length, reg_width)

    async def go():
        rig.ex.i2c.read = spy
        rig.state.bindings[(1, 64)] = Binding(1, 64, "INA226", {"reg": 0xFE, "len": 2, "hex": "5449", "reg_width": 16},
                                              [5], last_verified_ms=0)
        await rig.run("i2c.write", {"bus": 1, "addr": 64, "reg": 5, "hex": "0a00"})
        assert (0xFE, 16) in calls
    asyncio.run(go())


def _rig_with_logger(rig, tmp_path):
    from mugemuge_agentd.logger import Logger
    rig.ex.logger = Logger(rig.ex.manifest, rig.state, rig.i2c, {}, rig.clock, tmp_path / "spool", send=lambda m: True,
                           emit=rig.out.append)
    return rig.ex.logger


INA_READ = {"name": "v", "bus": 1, "addr": 64, "reg": 2, "len": 2}
BME_READ = {"name": "t", "bus": 1, "addr": 118, "reg": 0xD0, "len": 1}


def test_unbind_stops_dependent_jobs(rig, sign, tmp_path):
    """unbind は常に通り、そのアドレスを読む計測・記録ジョブを reason=unbound で止める。ほかのジョブは続く（F2）。"""
    async def go():
        lg = _rig_with_logger(rig, tmp_path)
        rig.state.bindings[(1, 64)] = Binding(1, 64, "INA226", BIND["fingerprint"], [0, 5], last_verified_ms=0)
        job = (await rig.run("sample.start", {"period_ms": 50, "duration_ms": 60_000, "reads": [INA_READ]}))["data"]["job_id"]
        other = (await rig.run("sample.start", {"period_ms": 50, "duration_ms": 60_000, "reads": [BME_READ]}))["data"]["job_id"]
        log_id = lg.start({"name": "x", "interval_ms": 1000, "duration_ms": 60_000, "reads": [INA_READ]})["log_id"]
        await asyncio.sleep(0.1)
        r = await rig.run("i2c.unbind", {"bus": 1, "addr": 64})
        assert r["ok"] and r["data"]["bound"] is False and sorted(r["data"]["jobs_stopped"]) == sorted([job, log_id])
        await asyncio.sleep(0.1)
        ends = {e["detail"]["job_id"]: e["detail"]["reason"] for e in rig.out
                if e["type"] == "event" and e["event"] == "job_end"}
        assert ends == {job: "unbound", log_id: "unbound"}
        assert other in rig.state.jobs and job not in rig.state.jobs and log_id not in rig.state.jobs
        for m in rig.out:
            assert message_errors(m) == [], m
        rig.sampler.stop_all("stopped")
        await rig.sampler.wait_all()
    asyncio.run(go())


def test_fingerprint_mismatch_stops_dependent_jobs(rig, tmp_path):
    """書き込み前の指紋の読み直しで部品が変わっていたら、バインドを外し、依存するジョブも止める（F2）。"""
    async def go():
        lg = _rig_with_logger(rig, tmp_path)
        rig.state.bindings[(1, 64)] = Binding(1, 64, "INA226", {"reg": 0xFE, "len": 2, "hex": "ffff"}, [0, 5],
                                              last_verified_ms=0)
        job = (await rig.run("sample.start", {"period_ms": 50, "duration_ms": 60_000, "reads": [INA_READ]}))["data"]["job_id"]
        log_id = lg.start({"name": "x", "interval_ms": 1000, "duration_ms": 60_000, "reads": [INA_READ]})["log_id"]
        r = await rig.run("i2c.write", {"bus": 1, "addr": 64, "reg": 5, "hex": "0a00"})
        assert r["error"]["code"] == "fingerprint_mismatch"
        await asyncio.sleep(0.1)
        drop = next(e for e in rig.out if e["type"] == "event" and e["event"] == "binding_dropped")
        assert sorted(drop["detail"]["jobs_stopped"]) == sorted([job, log_id])
        ends = {e["detail"]["job_id"]: e["detail"]["reason"] for e in rig.out
                if e["type"] == "event" and e["event"] == "job_end"}
        assert ends == {job: "unbound", log_id: "unbound"}
        for m in rig.out:
            assert message_errors(m) == [], m
    asyncio.run(go())
