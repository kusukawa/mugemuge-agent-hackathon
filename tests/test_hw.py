"""偽の I2C／GPIO と、SmbusI2C のトランザクションの組み立て。"""
import errno

import pytest

from mugemuge_agentd.hw.gpio import FakeGPIO, LineConfig, line_configs
from mugemuge_agentd.hw.i2c import FakeI2C, I2CError, SmbusI2C, reg_bytes


def test_fake_i2c_has_ina226_and_bme280():
    bus = FakeI2C()
    assert bus.read(1, 0x40, 0xFE, 2).hex() == "5449"
    assert bus.read(1, 0x40, 0xFF, 2).hex() == "2260"
    assert bus.read(1, 0x76, 0xD0, 1).hex() == "60"
    assert bus.scan(1, 0x08, 0x77) == [0x40, 0x76]


def test_fake_i2c_scan_skips_excluded_and_reserved():
    bus = FakeI2C()
    assert bus.scan(1, 0x00, 0x7F, exclude=[0x76]) == [0x40]


def test_fake_i2c_nack_is_enxio():
    with pytest.raises(I2CError) as e:
        FakeI2C().read(1, 0x41, 0, 1)
    assert e.value.errno == errno.ENXIO
    assert e.value.errno_name == "ENXIO"


def test_fake_i2c_write_then_read_16bit_register():
    bus = FakeI2C()
    assert bus.write(1, 0x40, 0x05, bytes.fromhex("0a00")) == 2
    assert bus.read(1, 0x40, 0x05, 2).hex() == "0a00"


def test_fake_i2c_auto_increment_8bit():
    bus = FakeI2C()
    bus.write(1, 0x76, 0xF4, bytes.fromhex("2701"))
    assert bus.read(1, 0x76, 0xF4, 2).hex() == "2701"


def test_fake_i2c_injected_error_once():
    bus = FakeI2C()
    bus.fail_next[(1, 0x40)] = errno.EREMOTEIO
    with pytest.raises(I2CError):
        bus.read(1, 0x40, 2, 2)
    assert bus.read(1, 0x40, 2, 2).hex() == "0a14"


def test_reg_bytes_16bit_is_big_endian():
    assert reg_bytes(0x1234, 16) == b"\x12\x34"
    assert reg_bytes(0x12, 8) == b"\x12"


class _RecordingSMBus:
    """smbus2.SMBus の代わり。i2c_rdwr に渡されたメッセージを記録する。"""

    def __init__(self, bus):
        self.calls = []
        self.reply = b"\x54\x49"
        self.error = None

    def i2c_rdwr(self, *msgs):
        if self.error:
            raise OSError(self.error, "Remote I/O error")
        self.calls.append([(m.flags, bytes(m) if m.flags == 0 else len(m)) for m in msgs])
        for m in msgs:
            if m.flags:  # 読み出し
                for i in range(m.len):
                    m.buf[i] = self.reply[i]

    def read_byte(self, addr):
        if addr != 0x40:
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        return 0

    def close(self):
        pass


@pytest.fixture
def smbus():
    s = SmbusI2C()
    rec = _RecordingSMBus(1)
    s._SMBus = lambda bus: rec
    return s, rec


def test_smbus_register_read_is_one_rdwr_with_repeated_start(smbus):
    s, rec = smbus
    assert s.read(1, 0x40, 0xFE, 2).hex() == "5449"
    assert rec.calls == [[(0, b"\xfe"), (1, 2)]]


def test_smbus_16bit_register_pointer(smbus):
    s, rec = smbus
    s.read(1, 0x40, 0x0102, 2, reg_width=16)
    assert rec.calls[0][0] == (0, b"\x01\x02")


def test_smbus_read_without_register(smbus):
    s, rec = smbus
    s.read(1, 0x40, None, 2)
    assert rec.calls == [[(1, 2)]]


def test_smbus_write_prefixes_register(smbus):
    s, rec = smbus
    assert s.write(1, 0x40, 0x05, bytes.fromhex("0a00")) == 2
    assert rec.calls == [[(0, b"\x05\x0a\x00")]]


def test_smbus_nack_becomes_i2c_error(smbus):
    s, rec = smbus
    rec.error = errno.EREMOTEIO
    with pytest.raises(I2CError) as e:
        s.read(1, 0x40, 0, 1)
    assert e.value.errno_name == "EREMOTEIO"


def test_smbus_scan_uses_read_byte(smbus):
    s, _ = smbus
    assert s.scan(1, 0x08, 0x77) == [0x40]


def test_fake_gpio_outputs_start_at_safe_value(manifest):
    g = FakeGPIO()
    g.configure(line_configs(manifest))
    assert g.values == {17: 0}
    assert g.read(27) == 1          # pull_up
    g.write(17, 1)
    assert g.read(17) == 1
    with pytest.raises(OSError):
        g.write(27, 1)              # 入力には書けない


def test_fake_gpio_safe_value_one():
    g = FakeGPIO()
    g.configure([LineConfig(5, "output", safe_value=1)])
    assert g.values[5] == 1
