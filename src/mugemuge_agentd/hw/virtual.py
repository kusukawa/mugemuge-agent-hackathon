"""仮想デバイスの部品（M4）。FakeI2C に載せて、本物らしく振る舞う模擬センサ。

審査員が実機なしでエージェントの同定・承認・計測を試せるようにする。部品はデータシートどおりのレジスタや
コマンドを持ち、エージェントからは本物と区別がつかない（同じカタログ・同じデコーダで扱える）。

- BMP280（0x76）: chip_id 0x58。電源投入時はスリープで、ctrl_meas（0xF4）を書くまで 0x80000 を返す
- SHT30（0x44）: 16ビットコマンド。シリアル番号（0x3780）、定期計測（0x2236 など）、fetch（0xE000）、CRC 付き。
  定期計測中は fetch・break・ソフトリセット以外を NACK する
- INA226（0x40）: 製造者ID 0x5449、ダイID 0x2260。負荷が揺らぐ電源（約5V、約120mA）

値は時刻の関数（ゆっくりした変化＋小さな揺らぎ）。clock を差し替えればテストで決まった値になる。
"""
from __future__ import annotations

import errno
import math
import random
import struct
import time
from typing import Callable

from .i2c import FakeDevice, FakeI2C, I2CError

Clock = Callable[[], float]

# kusupi5 の BMP280 で実際に読んだ校正値（2026-10-06）。本物の個体と同じ式で換算される
BMP280_CALIB = bytes.fromhex("e96b8b6632002c939dd7d00bd023c1fef9ff8c3cf8c67017")


class Environment:
    """部屋の空気と電源。全部品が同じ環境を測る（BMP280 と SHT30 の気温がほぼ一致する）。"""

    def __init__(self, clock: Clock = time.monotonic, seed: int | None = None):
        self.clock = clock
        self.t0 = clock()
        self.rng = random.Random(seed)

    def _t(self) -> float:
        return self.clock() - self.t0

    def temperature_c(self) -> float:
        return 24.6 + 0.4 * math.sin(self._t() / 90.0) + self.rng.gauss(0, 0.02)

    def pressure_pa(self) -> float:
        return 100_450 + 30 * math.sin(self._t() / 300.0) + self.rng.gauss(0, 2)

    def humidity_pct(self) -> float:
        return 58.0 + 3.0 * math.sin(self._t() / 120.0 + 1.0) + self.rng.gauss(0, 0.1)

    def load(self) -> tuple[float, float]:
        """(バス電圧 V, 電流 A)。数秒おきに負荷が増える。"""
        t = self._t()
        current = 0.120 + (0.080 if int(t / 4) % 3 == 0 else 0.0) + self.rng.gauss(0, 0.002)
        return 5.02 - current * 0.15, current


# ---- BMP280 -------------------------------------------------------------------------------

def _bmp280_forward(cal: bytes, adc_t: int, adc_p: int) -> tuple[float, float]:
    """データシートの補償式（換算の逆を解くために使う。クラウドのデコーダとは独立に書いている）。"""
    T1, T2, T3, P1, P2, P3, P4, P5, P6, P7, P8, P9 = struct.unpack("<HhhHhhhhhhhh", cal[:24])
    v1 = (adc_t / 16384.0 - T1 / 1024.0) * T2
    v2 = ((adc_t / 131072.0 - T1 / 8192.0) ** 2) * T3
    t_fine = v1 + v2
    v1 = t_fine / 2.0 - 64000.0
    v2 = v1 * v1 * P6 / 32768.0 + v1 * P5 * 2.0
    v2 = v2 / 4.0 + P4 * 65536.0
    v1 = (P3 * v1 * v1 / 524288.0 + P2 * v1) / 524288.0
    v1 = (1.0 + v1 / 32768.0) * P1
    p = 1048576.0 - adc_p
    p = (p - v2 / 4096.0) * 6250.0 / v1
    return t_fine / 5120.0, p + (P9 * p * p / 2147483648.0 + p * P8 / 32768.0 + P7) / 16.0


def _bisect(f: Callable[[int], float], target: float, lo: int, hi: int, increasing: bool) -> int:
    for _ in range(24):
        mid = (lo + hi) // 2
        if (f(mid) < target) == increasing:
            lo = mid
        else:
            hi = mid
    return lo


class BMP280(FakeDevice):
    def __init__(self, env: Environment, calib: bytes = BMP280_CALIB):
        regs = {0xD0: b"\x58", 0xF3: b"\x00", 0xF4: b"\x00", 0xF5: b"\x00"}
        for i, b in enumerate(calib[:24]):
            regs[0x88 + i] = bytes([b])
        super().__init__(regs=regs, reg_size=1)
        self.env, self.calib = env, calib

    def _measurement(self) -> bytes:
        if self.regs[0xF4][0] & 0x03 == 0:          # スリープ: 未計測の値
            return bytes.fromhex("800000800000")
        t_target, p_target = self.env.temperature_c(), self.env.pressure_pa()
        adc_t = _bisect(lambda a: _bmp280_forward(self.calib, a, 300_000)[0], t_target, 0, 1 << 20, True)
        adc_p = _bisect(lambda a: _bmp280_forward(self.calib, adc_t, a)[1], p_target, 0, 1 << 20, False)
        enc = lambda v: bytes([(v >> 12) & 0xFF, (v >> 4) & 0xFF, (v & 0xF) << 4])
        return enc(adc_p) + enc(adc_t)

    def read(self, reg, length, reg_width=8):
        r = self.pointer if reg is None else reg
        if 0xF7 <= r <= 0xFC:
            data = self._measurement()[r - 0xF7:]
            return (data + bytes(length))[:length]
        return super().read(reg, length)

    def write(self, reg, data, reg_width=8):
        if reg == 0xE0 and data[:1] == b"\xb6":     # ソフトリセット
            self.regs[0xF4] = b"\x00"
            return
        super().write(reg, data)


# ---- SHT30 --------------------------------------------------------------------------------

def crc8(data: bytes) -> int:
    c = 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x31) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def _words(*ws: int) -> bytes:
    out = b""
    for w in ws:
        b = w.to_bytes(2, "big")
        out += b + bytes([crc8(b)])
    return out


PERIODIC = {0x2032, 0x2024, 0x202F, 0x2130, 0x2126, 0x212D, 0x2236, 0x2220, 0x222B, 0x2334, 0x2322, 0x2329,
            0x2737, 0x2721, 0x272A}
SINGLE_SHOT = {0x2C06, 0x2C0D, 0x2C10, 0x2400, 0x240B, 0x2416}


class SHT30:
    """コマンド方式。i2c.read の reg（16ビット）と i2c.write の reg＋データを、つないで1つのコマンドとして扱う。"""

    def __init__(self, env: Environment, serial: int = 0x0BD45BA7):
        self.env = env
        self.serial = serial
        self.periodic = False
        self.status = 0x8010           # 電源投入直後（リセット検出・アラート保留）
        self.pending: bytes | None = None
        self.commands: list[int] = []

    def _command(self, cmd: int) -> None:
        self.commands.append(cmd)
        if self.periodic and cmd not in (0xE000, 0x3093, 0x30A2):
            raise I2CError(errno.EREMOTEIO, f"SHT30 ignores 0x{cmd:04x} in periodic mode")
        if cmd == 0x30A2:                                 # ソフトリセット
            self.periodic, self.pending, self.status = False, None, 0x0010
        elif cmd == 0x3093:                               # break（定期計測をやめる）
            self.periodic, self.pending = False, None
        elif cmd in PERIODIC:
            self.periodic, self.pending = True, None
        elif cmd == 0xE000:
            if not self.periodic:
                raise I2CError(errno.EREMOTEIO, "SHT30 fetch without periodic mode")
            self.pending = self._measure()
        elif cmd in SINGLE_SHOT:
            self.pending = self._measure()
        elif cmd == 0x3780 or cmd == 0x3682:              # シリアル番号
            self.pending = _words(self.serial >> 16, self.serial & 0xFFFF)
        elif cmd == 0xF32D:                               # ステータス
            self.pending = _words(self.status)
        elif cmd == 0x3041:                               # ステータスのクリア
            self.status &= ~0x8010 & 0xFFFF
        elif cmd in (0x306D, 0x3066):                     # ヒーター ON/OFF
            self.status = (self.status | 0x2000) if cmd == 0x306D else (self.status & ~0x2000 & 0xFFFF)
        else:
            raise I2CError(errno.EREMOTEIO, f"SHT30 unknown command 0x{cmd:04x}")

    def _measure(self) -> bytes:
        t = self.env.temperature_c() + 0.1               # BMP280 とわずかに違う（別の個体なので）
        rh = self.env.humidity_pct()
        return _words(round((t + 45) / 175 * 65535), round(rh / 100 * 65535))

    def read(self, reg, length, reg_width=8):
        if reg is not None:
            self._command(reg if reg_width == 16 else reg)
        if self.pending is None:
            raise I2CError(errno.EREMOTEIO, "SHT30 has no data to read")
        data, self.pending = self.pending, None
        return (data + bytes(length))[:length]

    def write(self, reg, data, reg_width=8):
        raw = (b"" if reg is None else reg.to_bytes(2 if reg_width == 16 else 1, "big")) + data
        if len(raw) != 2:
            raise I2CError(errno.EREMOTEIO, "SHT30 commands are 2 bytes")
        self._command(int.from_bytes(raw, "big"))


# ---- INA226 -------------------------------------------------------------------------------

class INA226(FakeDevice):
    def __init__(self, env: Environment, shunt_ohm: float = 0.1):
        super().__init__(reg_size=2, regs={0x00: bytes.fromhex("4127"), 0x05: bytes.fromhex("0000"),
                                           0xFE: bytes.fromhex("5449"), 0xFF: bytes.fromhex("2260")})
        self.env, self.shunt_ohm = env, shunt_ohm

    def read(self, reg, length, reg_width=8):
        r = self.pointer if reg is None else reg
        v, i = self.env.load()
        live = {0x01: struct.pack(">h", round(i * self.shunt_ohm / 2.5e-6)),
                0x02: round(v / 1.25e-3).to_bytes(2, "big")}
        cal = int.from_bytes(self.regs.get(0x05, b"\x00\x00"), "big")
        if cal:
            current_lsb = 0.00512 / (cal * self.shunt_ohm)
            live[0x04] = struct.pack(">h", round(i / current_lsb))
            live[0x03] = round(v * i / (25 * current_lsb)).to_bytes(2, "big")
        else:
            live[0x03] = live[0x04] = b"\x00\x00"
        out = b""
        while len(out) < length:
            out += live.get(r, self.regs.get(r, b"\x00\x00"))
            r += 1
        return out[:length]


def virtual_i2c(env: Environment | None = None) -> FakeI2C:
    env = env or Environment()
    return FakeI2C({1: {0x40: INA226(env), 0x44: SHT30(env), 0x76: BMP280(env)}})
