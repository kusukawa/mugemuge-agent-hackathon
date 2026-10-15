"""承認の期限判定に使う補正済み時刻（PROTOCOL.md 4.2節）。"""
from mugemuge_agentd.clock import Clock


class FakeTimeClock(Clock):
    """壁時計と単調時計を手で進められる Clock。"""

    def __init__(self, wall_ms: int, mono_ms: int = 1_000):
        super().__init__()
        self.wall, self.mono = wall_ms, mono_ms

    def now_ms(self) -> int:
        return self.wall

    def mono_ms(self) -> int:
        return self.mono


SERVER_MS = 1_792_002_120_000


def test_before_welcome_uses_wall_clock():
    c = FakeTimeClock(SERVER_MS)
    assert c.corrected_s() == SERVER_MS // 1000
    assert c.offset_ms == 0


def test_corrects_a_wall_clock_that_is_behind():
    c = FakeTimeClock(SERVER_MS - 900_000)          # 起動直後: 前回の停止時刻のまま（15分遅れ）
    c.sync(SERVER_MS, c.mono_ms())
    c.mono += 5_000
    c.wall += 5_000
    assert c.corrected_ms() == SERVER_MS + 5_000
    assert c.offset_ms == 900_000


def test_ntp_step_after_welcome_does_not_shift_corrected_time():
    """RTC のない Pi が NTP の同期前に接続し、そのあと NTP が壁時計を15分進めた（pi-kusupizaro2 の不具合）。"""
    c = FakeTimeClock(SERVER_MS - 900_000)
    c.sync(SERVER_MS, c.mono_ms())
    c.wall += 900_000 + 60_000                      # NTP の補正＋1分経過
    c.mono += 60_000
    assert c.corrected_ms() == SERVER_MS + 60_000  # 以前の実装では SERVER_MS + 960_000（未来にずれて expired）
    assert c.offset_ms == 0


def test_offset_setter_keeps_tests_working():
    c = FakeTimeClock(SERVER_MS)
    c.offset_ms = 30_000
    assert c.corrected_ms() == SERVER_MS + 30_000
    c.mono += 1_000
    c.wall += 1_000
    assert c.corrected_ms() == SERVER_MS + 31_000
