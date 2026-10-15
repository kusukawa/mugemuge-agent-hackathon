"""部品カタログとデコーダ。データシートの計算例と、実機（kusupi5）で読んだ値で確かめる。"""
import struct

import pytest

from services.agent.parts import (PARTS, DecodeError, catalog_for, crc8_sensirion, decode, decode_bme280,
                                  sensirion_words)

# BMP280 データシート 3.12 節の計算例
DS_CAL = struct.pack("<HhhHhhhhhhhh", 27504, 26435, -1000, 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000)


def _raw20(v: int) -> bytes:
    return bytes([(v >> 12) & 0xFF, (v >> 4) & 0xFF, (v & 0xF) << 4])


def test_bmp280_matches_datasheet_example():
    out = decode("bmp280", (_raw20(415148) + _raw20(519888)).hex(), DS_CAL.hex())
    assert out == {"temperature_c": 25.08, "pressure_hpa": 1006.53}


def test_bmp280_real_reading_from_kusupi5():
    # 2026-10-06 に実機で読んだ校正値と生データ（PLAN.md のメモ）。BMP280 24.81℃ / 1004.50hPa
    out = decode("bmp280", "4835007f4300", "e96b8b6632002c939dd7d00bd023c1fef9ff8c3cf8c67017")
    assert out == {"temperature_c": 24.81, "pressure_hpa": 1004.5}


def test_bmp280_sleep_mode_is_reported():
    with pytest.raises(DecodeError, match="sleep"):
        decode("bmp280", "800000800000", DS_CAL.hex())


def test_sht3x_real_reading_and_crc():
    assert decode("sht3x", "65e6c4a11b23") == {"temperature_c": 24.66, "humidity_pct": 62.9}
    assert crc8_sensirion(b"\xbe\xef") == 0x92                    # データシートの例
    with pytest.raises(DecodeError, match="CRC"):
        decode("sht3x", "65e6c5a11b23")
    assert sensirion_words(bytes.fromhex("0bd45ba7eaca"))       # 実機のシリアル番号は CRC が通る


def test_ina226():
    out = decode("ina226", "0a14", shunt_raw="0190", shunt_ohm=0.1)
    assert out == {"bus_v": 3.225, "shunt_mv": 1.0, "current_ma": 10.0}
    assert decode("ina226", "0a14", shunt_raw="fe70")["current_ma"] == -10.0     # 逆向きの電流
    # 0x01 を4バイト読んだもの（シャント電圧＋ff）をバス電圧として渡すと止める（本番で 0.0213V と誤表示した）
    with pytest.raises(DecodeError, match="0x02"):
        decode("ina226", "0011ffff", shunt_ohm=0.005)
    with pytest.raises(DecodeError):
        decode("ina226", "0a14", shunt_raw="0011ffff")
    assert decode("ina226", "0a14", shunt_raw="0011", shunt_ohm=0.005)["current_ma"] == 8.5


def _bme280_h_int32(adc_h, t_fine, H1, H2, H3, H4, H5, H6):
    """Bosch の BME280 ドライバの整数版（独立した実装として突き合わせに使う）。"""
    v = t_fine - 76800
    v = (((((adc_h << 14) - (H4 << 20) - (H5 * v)) + 16384) >> 15) *
         (((((((v * H6) >> 10) * (((v * H3) >> 11) + 32768)) >> 10) + 2097152) * H2 + 8192) >> 14))
    v = v - (((((v >> 15) * (v >> 15)) >> 7) * H1) >> 4)
    v = max(0, min(v, 419430400))
    return (v >> 12) / 1024.0


def test_bme280_humidity_matches_integer_reference():
    H1, H2, H3, H4, H5, H6 = 75, 362, 0, 313, 50, 30
    e = struct.pack("<h", H2) + bytes([H3, (H4 >> 4) & 0xFF, (H4 & 0x0F) | ((H5 & 0x0F) << 4), (H5 >> 4) & 0xFF]) \
        + struct.pack("b", H6)
    cal = DS_CAL + b"\x00" + bytes([H1]) + e
    adc_t, adc_p, adc_h = 519888, 415148, 30000
    raw = _raw20(adc_p) + _raw20(adc_t) + adc_h.to_bytes(2, "big")
    out = decode_bme280(raw, cal)
    assert out["temperature_c"] == 25.08
    # t_fine（整数版）= 128422（データシートの例）
    ref = _bme280_h_int32(adc_h, 128422, H1, H2, H3, H4, H5, H6)
    assert abs(out["humidity_pct"] - ref) < 0.2


def test_catalog_for_0x40_lists_ina226_and_hdc1080_with_distinguishing_probe():
    c = catalog_for(0x40)
    names = [x["part"] for x in c["candidates"]]
    assert {"INA226", "HDC1080", "INA219"} <= set(names)
    ina = next(x for x in c["candidates"] if x["part"] == "INA226")
    assert ina["probes"][1]["args"] == {"bus": 1, "addr": 64, "reg": 255, "len": 2} and ina["probes"][1]["expect"] == "2260"
    assert ina["measure"] == {"bus": 1, "addr": 64, "reg": 2, "len": 2}       # バス電圧。レジスタは自動で進まない


def test_catalog_sht30_probe_is_16bit_command():
    sht = next(x for x in catalog_for(0x44)["candidates"] if x["part"] == "SHT30")
    assert sht["probes"][0]["args"]["reg_width"] == 16 and sht["probes"][0]["crc_check"]
    assert sht["bind"]["write_allow_regs"] == [0x22, 0xE000]


def test_unknown_address_has_hint():
    assert catalog_for(0x29)["candidates"] == [] and catalog_for(0x29)["unknown_hint"]


def test_every_part_with_decoder_has_measure_and_channels():
    for p in PARTS.values():
        if p.decoder:
            assert p.measure and p.channels, p.name
