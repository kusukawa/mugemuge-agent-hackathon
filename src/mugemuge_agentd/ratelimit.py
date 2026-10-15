"""トークンバケット（PROTOCOL.md 8.4節）。バースト容量は毎秒量の2倍。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TokenBucket:
    rate_per_s: float
    capacity: float
    tokens: float
    updated_ms: int

    @classmethod
    def create(cls, rate_per_s: int, now_ms: int) -> "TokenBucket":
        cap = float(rate_per_s * 2)
        return cls(rate_per_s=float(rate_per_s), capacity=cap, tokens=cap, updated_ms=now_ms)

    def _refill(self, now_ms: int) -> None:
        if now_ms > self.updated_ms:
            self.tokens = min(self.capacity, self.tokens + (now_ms - self.updated_ms) * self.rate_per_s / 1000)
            self.updated_ms = now_ms

    def retry_after_ms(self, n: float, now_ms: int) -> int:
        """n 個取れるまでの待ち時間。0 なら今すぐ取れる。容量を超える要求は -1。"""
        if n > self.capacity:
            return -1
        self._refill(now_ms)
        if self.tokens >= n:
            return 0
        return int((n - self.tokens) * 1000 / self.rate_per_s) + 1

    def consume(self, n: float, now_ms: int) -> None:
        self._refill(now_ms)
        self.tokens -= n
