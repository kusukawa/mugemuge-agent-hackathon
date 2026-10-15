"""I2C バス（PROTOCOL.md 5.1節 i2c.*）。

どの実装もブロッキングで、スレッドから呼ばれる前提。直列化は呼び出し側（executor のバスロック）が行う。
失敗はすべて I2CError(errno) にする。NACK は ENXIO か EREMOTEIO になる。
"""
from __future__ import annotations

import errno as _errno
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..policy import RESERVED_I2C


class I2CError(OSError):
    """I/O の失敗。detail.errno には errno の名前（例: ENXIO）を載せる。"""

    @property
    def errno_name(self) -> str:
        return _errno.errorcode.get(self.errno or 0, str(self.errno))


def reg_bytes(reg: int, reg_width: int) -> bytes:
    """レジスタアドレスのバイト列。16 ビットはビッグエンディアン。"""
    return reg.to_bytes(2 if reg_width == 16 else 1, "big")


class I2CBus(ABC):
    @abstractmethod
    def read(self, bus: int, addr: int, reg: int | None, length: int, reg_width: int = 8) -> bytes:
        """reg があれば「ポインタ書き込み→リピーテッドスタート→読み出し」、なければそのまま length バイト読む。"""

    @abstractmethod
    def write(self, bus: int, addr: int, reg: int | None, data: bytes, reg_width: int = 8) -> int:
        """reg（あれば）に続けて data を1トランザクションで書く。書いたデータのバイト数を返す。"""

    @abstractmethod
    def probe(self, bus: int, addr: int) -> bool:
        """1バイト読み出し（SMBus read byte）で応答があるか。quick write は使わない。"""

    def scan(self, bus: int, first: int, last: int, exclude: list[int] | tuple[int, ...] = ()) -> list[int]:
        skip = set(exclude) | RESERVED_I2C
        return [a for a in range(first, last + 1) if a not in skip and self.probe(bus, a)]

    def close(self) -> None:
        pass


class SmbusI2C(I2CBus):
    """実機の /dev/i2c-N。smbus2 の i2c_rdwr を使う。"""

    def __init__(self) -> None:
        from smbus2 import SMBus, i2c_msg  # 実機でだけ必要

        self._SMBus = SMBus
        self._msg = i2c_msg
        self._buses: dict[int, object] = {}
        self._lock = threading.Lock()

    def _bus(self, bus: int):
        with self._lock:
            b = self._buses.get(bus)
            if b is None:
                try:
                    b = self._SMBus(bus)
                except OSError as e:
                    raise I2CError(e.errno, f"cannot open /dev/i2c-{bus}: {e.strerror}") from None
                self._buses[bus] = b
            return b

    def read(self, bus, addr, reg, length, reg_width=8):
        b = self._bus(bus)
        rd = self._msg.read(addr, length)
        try:
            if reg is None:
                b.i2c_rdwr(rd)
            else:
                b.i2c_rdwr(self._msg.write(addr, reg_bytes(reg, reg_width)), rd)
        except OSError as e:
            raise I2CError(e.errno, f"i2c read 0x{addr:02x}: {e.strerror}") from None
        return bytes(rd)

    def write(self, bus, addr, reg, data, reg_width=8):
        b = self._bus(bus)
        payload = (b"" if reg is None else reg_bytes(reg, reg_width)) + data
        try:
            b.i2c_rdwr(self._msg.write(addr, payload))
        except OSError as e:
            raise I2CError(e.errno, f"i2c write 0x{addr:02x}: {e.strerror}") from None
        return len(data)

    def probe(self, bus, addr):
        try:
            self._bus(bus).read_byte(addr)
            return True
        except OSError:
            return False

    def close(self):
        with self._lock:
            for b in self._buses.values():
                b.close()
            self._buses.clear()


# ---- 模擬デバイス ---------------------------------------------------------------


@dataclass
class FakeDevice:
    """模擬レジスタ。regs はレジスタ番号→そのレジスタの中身（reg_size バイト）。

    読み出しは指定レジスタから順に連結して length バイトを返す（自動インクリメント）。
    無いレジスタは 0x00 で埋める。reg を省略した読み書きは pointer（最後に指したレジスタ）を使う。
    """

    regs: dict[int, bytes]
    reg_size: int = 1
    pointer: int = 0
    writes: list[tuple[int, bytes]] = field(default_factory=list)

    def read(self, reg: int | None, length: int, reg_width: int = 8) -> bytes:
        r = self.pointer if reg is None else reg
        out = b""
        while len(out) < length:
            out += self.regs.get(r, bytes(self.reg_size))
            r += 1
        return out[:length]

    def write(self, reg: int | None, data: bytes, reg_width: int = 8) -> None:
        r = self.pointer if reg is None else reg
        self.pointer = r
        self.writes.append((r, data))
        for i in range(0, len(data), self.reg_size):
            chunk = data[i:i + self.reg_size]
            old = self.regs.get(r, bytes(self.reg_size))
            self.regs[r] = chunk + old[len(chunk):]
            r += 1


def ina226() -> FakeDevice:
    """TI INA226（16 ビットレジスタ）。製造者ID 0x5449、ダイID 0x2260。"""
    return FakeDevice(reg_size=2, regs={
        0x00: bytes.fromhex("4127"),   # CONFIG の既定値
        0x01: bytes.fromhex("0190"),   # シャント電圧
        0x02: bytes.fromhex("0a14"),   # バス電圧
        0x03: bytes.fromhex("0000"),   # 電力
        0x04: bytes.fromhex("04b0"),   # 電流
        0x05: bytes.fromhex("0000"),   # 校正
        0xFE: bytes.fromhex("5449"),
        0xFF: bytes.fromhex("2260"),
    })


def bme280() -> FakeDevice:
    """Bosch BME280（8 ビットレジスタ）。chip_id 0xD0 = 0x60。"""
    return FakeDevice(reg_size=1, regs={0xD0: b"\x60", 0xF3: b"\x00", 0xF4: b"\x00"})


def sh1107() -> FakeDevice:
    """SH1107 OLED。制御バイト 0x00／0x40 に続く書き込みを writes に残す。読み出しは状態バイト（表示 OFF なら 0x40）。"""
    return FakeDevice(reg_size=1, regs={0x00: b"\x40"})


def default_fake_devices() -> dict[int, dict[int, FakeDevice]]:
    return {1: {0x40: ina226(), 0x76: bme280()}}


class FakeI2C(I2CBus):
    """辞書で表した模擬 I2C バス。テストと仮想デバイス（--fake-hw）で使う。"""

    def __init__(self, devices: dict[int, dict[int, FakeDevice]] | None = None):
        self.devices = default_fake_devices() if devices is None else devices
        self.fail_next: dict[tuple[int, int], int] = {}   # (bus, addr) → 次の1回だけ返す errno
        self._lock = threading.Lock()

    def _dev(self, bus: int, addr: int) -> FakeDevice:
        if bus not in self.devices:
            raise I2CError(_errno.ENOENT, f"no such bus: /dev/i2c-{bus}")
        e = self.fail_next.pop((bus, addr), None)
        if e is not None:
            raise I2CError(e, f"injected error at 0x{addr:02x}")
        dev = self.devices[bus].get(addr)
        if dev is None:
            raise I2CError(_errno.ENXIO, f"no ACK from 0x{addr:02x}")
        return dev

    def read(self, bus, addr, reg, length, reg_width=8):
        with self._lock:
            return self._dev(bus, addr).read(reg, length, reg_width)

    def write(self, bus, addr, reg, data, reg_width=8):
        with self._lock:
            self._dev(bus, addr).write(reg, data, reg_width)
        return len(data)

    def probe(self, bus, addr):
        with self._lock:
            try:
                self._dev(bus, addr)
                return True
            except I2CError:
                return False
