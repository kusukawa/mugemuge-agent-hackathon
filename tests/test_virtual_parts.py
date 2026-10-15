"""仮想デバイスの部品が、本物と同じカタログ・同じデコーダで扱えること。"""
import errno

import pytest

from mugemuge_agentd.hw.i2c import I2CError
from mugemuge_agentd.hw.virtual import Environment, virtual_i2c
from services.agent.parts import catalog_for, decode, sensirion_words


class FixedClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def bus():
    clock = FixedClock()
    env = Environment(clock, seed=1)
    b = virtual_i2c(env)
    b.clock, b.env = clock, env
    return b


def test_scan_finds_three_parts(bus):
    assert bus.scan(1, 0x08, 0x77) == [0x40, 0x44, 0x76]


def test_catalog_probes_identify_each_part(bus):
    for addr, part in ((0x76, "BMP280"), (0x40, "INA226"), (0x44, "SHT30")):
        cand = next(c for c in catalog_for(addr)["candidates"] if c["part"] == part)
        for p in cand["probes"]:
            a = p["args"]
            got = bus.read(1, addr, a["reg"], a["len"], a.get("reg_width", 8))
            if p["expect"]:
                assert got.hex() == p["expect"], (part, p)
            if p["crc_check"]:
                assert sensirion_words(got)


def test_bmp280_sleeps_until_ctrl_meas_then_reads_room_air(bus):
    assert bus.read(1, 0x76, 0xF7, 6).hex() == "800000800000"
    bus.write(1, 0x76, 0xF4, bytes.fromhex("27"))
    cal = bus.read(1, 0x76, 0x88, 24).hex()
    v = decode("bmp280", bus.read(1, 0x76, 0xF7, 6).hex(), cal)
    assert abs(v["temperature_c"] - bus.env.temperature_c()) < 0.2
    assert 1003 < v["pressure_hpa"] < 1006


def test_sht30_periodic_mode_and_fetch(bus):
    with pytest.raises(I2CError):
        bus.read(1, 0x44, 0xE000, 6, 16)                   # 定期計測の前に fetch → NACK
    bus.write(1, 0x44, 0x22, bytes.fromhex("36"))         # 0x2236: 定期計測 2回/秒
    v = decode("sht3x", bus.read(1, 0x44, 0xE000, 6, 16).hex())
    assert 23 < v["temperature_c"] < 26 and 50 < v["humidity_pct"] < 65
    with pytest.raises(I2CError) as e:
        bus.read(1, 0x44, 0x3780, 6, 16)                   # 定期計測中はシリアルを読めない
    assert e.value.errno == errno.EREMOTEIO
    bus.write(1, 0x44, 0x30, bytes.fromhex("93"))         # break
    assert sensirion_words(bus.read(1, 0x44, 0x3780, 6, 16))


def test_ina226_decodes_to_a_plausible_supply(bus):
    v = decode("ina226", bus.read(1, 0x40, 0x02, 2).hex(), shunt_raw=bus.read(1, 0x40, 0x01, 2).hex())
    assert 4.9 < v["bus_v"] < 5.1 and 100 < v["current_ma"] < 220


def test_values_drift_over_time(bus):
    bus.write(1, 0x76, 0xF4, bytes.fromhex("27"))
    cal = bus.read(1, 0x76, 0x88, 24).hex()
    t1 = decode("bmp280", bus.read(1, 0x76, 0xF7, 6).hex(), cal)["temperature_c"]
    bus.clock.t += 140
    t2 = decode("bmp280", bus.read(1, 0x76, 0xF7, 6).hex(), cal)["temperature_c"]
    assert abs(t2 - t1) > 0.1
