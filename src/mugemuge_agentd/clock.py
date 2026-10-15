"""時刻。ts 用の壁時計、タイマ用の単調時計、承認の期限判定用の補正済み時刻（PROTOCOL.md 4.2節）。

補正済み時刻は「welcome の server_ts ＋ そこからの単調時計の経過」で出す。壁時計との差を固定で持つと、
RTC のない Pi（Zero 2 W など）が NTP の同期前に接続し、そのあと NTP が壁時計を進めたときに、
進めた分だけ補正済み時刻が未来にずれ、次の welcome まで承認がすべて expired になる（2026-10-15 に実機で確認）。
"""
from __future__ import annotations

import time


class Clock:
    def __init__(self) -> None:
        self._ref_server_ms: int | None = None   # 最後の welcome の server_ts
        self._ref_mono_ms = 0                    # それを受け取ったときの単調時計

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def mono_ms(self) -> int:
        return int(time.monotonic() * 1000)

    def sync(self, server_ts: int, received_mono_ms: int | None = None) -> None:
        """welcome を受けるたびに呼ぶ。received_mono_ms は受信時の mono_ms()。"""
        self._ref_server_ms = server_ts
        self._ref_mono_ms = self.mono_ms() if received_mono_ms is None else received_mono_ms

    def corrected_ms(self) -> int:
        """サーバの時計に合わせた UNIX ミリ秒。welcome の前は壁時計のまま。"""
        if self._ref_server_ms is None:
            return self.now_ms()
        return self._ref_server_ms + (self.mono_ms() - self._ref_mono_ms)

    def corrected_s(self) -> int:
        """NTP が未同期でも、あとで壁時計が飛んでも、承認の iat/exp を判定できる UNIX 秒。"""
        return self.corrected_ms() // 1000

    @property
    def offset_ms(self) -> int:
        """補正済み時刻 − 壁時計（ログとテスト用）。"""
        return self.corrected_ms() - self.now_ms()

    @offset_ms.setter
    def offset_ms(self, value: int) -> None:
        self.sync(self.now_ms() + value)
