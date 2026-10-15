"""出力の自動 OFF、デッドマン、非常停止。"""
import asyncio

from mugemuge_agentd.clock import Clock
from mugemuge_agentd.hw.gpio import FakeGPIO
from mugemuge_agentd.messages import message_errors
from mugemuge_agentd.safety import Safety


def make_safety(manifest, state):
    events = []
    gpio = FakeGPIO()
    s = Safety(manifest, state, gpio, Clock(), events.append)
    s.init_outputs()
    return s, gpio, events


def test_init_outputs_sets_safe_values(manifest, state):
    s, gpio, _ = make_safety(manifest, state)
    assert gpio.values[17] == 0
    assert s.outputs_state() == [{"line": 17, "value": 0}]


def test_active_output_reverts_after_hold(manifest, state):
    async def go():
        s, gpio, events = make_safety(manifest, state)
        data = await s.set_output(17, 1, 150)
        assert gpio.values[17] == 1 and "revert_at_ms" in data
        assert s.outputs_state()[0]["revert_at_ms"] == data["revert_at_ms"]
        await asyncio.sleep(0.3)
        assert gpio.values[17] == 0
        assert [e["event"] for e in events] == ["output_reverted"]
        assert events[0]["detail"]["reason"] == "hold_ms"
        assert message_errors(events[0]) == []
    asyncio.run(go())


def test_hold_omitted_uses_max_on_ms(manifest, state):
    async def go():
        s, gpio, events = make_safety(manifest, state)
        s.lines[17]["max_on_ms"] = 100
        await s.set_output(17, 1, None)
        await asyncio.sleep(0.25)
        assert gpio.values[17] == 0
        assert events[0]["detail"]["reason"] == "max_on_ms"
    asyncio.run(go())


def test_safe_write_cancels_pending_revert(manifest, state):
    async def go():
        s, gpio, events = make_safety(manifest, state)
        await s.set_output(17, 1, 100)
        await s.set_output(17, 0, None)
        await asyncio.sleep(0.2)
        assert events == []
        assert gpio.history[-1] == (17, 0)
    asyncio.run(go())


def test_deadman_reverts_outputs_and_stops_jobs(manifest, state):
    async def go():
        s, gpio, events = make_safety(manifest, state)
        s.deadman_ms = 200
        s.stop_jobs = lambda reason: ["job_1"] if reason == "deadman" else []
        s.start_watchdog()
        s.touch()
        await s.set_output(17, 1, 2000)
        await asyncio.sleep(0.45)
        await s.stop_watchdog()
        assert gpio.values[17] == 0
        ev = [e for e in events if e["event"] == "deadman_tripped"]
        assert len(ev) == 1                       # 作動は1回だけ（再び受信するまで再武装しない）
        assert ev[0]["detail"]["outputs_reverted"] == [17]
        assert ev[0]["detail"]["jobs_stopped"] == ["job_1"]
        assert ev[0]["detail"]["silent_ms"] >= 200
        assert message_errors(ev[0]) == []
    asyncio.run(go())


def test_deadman_does_not_trip_while_receiving(manifest, state):
    async def go():
        s, _, events = make_safety(manifest, state)
        s.deadman_ms = 200
        s.start_watchdog()
        for _ in range(6):
            s.touch()
            await asyncio.sleep(0.08)
        await s.stop_watchdog()
        assert events == []
    asyncio.run(go())


def test_deadman_unarmed_before_first_message(manifest, state):
    async def go():
        s, _, events = make_safety(manifest, state)
        s.deadman_ms = 100
        s.start_watchdog()
        await asyncio.sleep(0.3)
        await s.stop_watchdog()
        assert events == []
    asyncio.run(go())


def test_estop_latches_and_clears(manifest, state):
    async def go():
        s, gpio, events = make_safety(manifest, state)
        await s.set_output(17, 1, 1000)
        await s.engage_estop("run_x")
        assert state.estop and gpio.values[17] == 0
        s.clear_estop("user:test")
        assert not state.estop
        assert [e["event"] for e in events] == ["estop_engaged", "estop_cleared"]
        for e in events:
            assert message_errors(e) == []
    asyncio.run(go())


def test_unwritable_latch_starts_in_estop(manifest, state, tmp_path):
    """非常停止のラッチの置き場所に書けなければ、非常停止をかけた状態で起動し、hello で知らせる（P4）。
    書けないまま動くと、非常停止をかけても再起動で承認なしに解除されるため。運転中に書けなくなったら event で知らせる。"""
    import os

    from mugemuge_agentd.executor import hello_state
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        s = Safety(manifest, state, FakeGPIO(), Clock(), [].append, latch_path=ro / "estop.json")
        assert state.estop is True and s.estop_persist is False
        assert hello_state(state, s)["estop_persist"] is False
        assert not list(ro.iterdir())                          # 試しに書いたファイルも残らない
    finally:
        os.chmod(ro, 0o700)

    # 書ける場所なら今までどおり（止まらずに起動し、hello に estop_persist は載せない）
    state.estop = False
    ok = Safety(manifest, state, FakeGPIO(), Clock(), [].append, latch_path=tmp_path / "rw" / "estop.json")
    assert state.estop is False and ok.estop_persist and "estop_persist" not in hello_state(state, ok)
    assert not list((tmp_path / "rw").iterdir())

    # 運転中に書けなくなった → 止める動作は続け、event に persisted: false
    async def go():
        events = []
        s2 = Safety(manifest, state, FakeGPIO(), Clock(), events.append, latch_path=tmp_path / "rw" / "estop.json")
        s2.init_outputs()
        os.chmod(tmp_path / "rw", 0o500)
        try:
            await s2.engage_estop("user:test")
        finally:
            os.chmod(tmp_path / "rw", 0o700)
        ev = [e for e in events if e["type"] == "event"][-1]
        assert ev["event"] == "estop_engaged" and ev["detail"]["persisted"] is False and state.estop
        assert message_errors(ev) == []
    asyncio.run(go())
