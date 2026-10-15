"""部品カタログと宣言的デコーダ（クラウド側。PROTOCOL.md 0章の5: 値の換算はクラウドで行う）。

エージェントは推測で型番を決めない。このカタログの「候補 → 確かめ方（読むレジスタと期待値）」に沿って
実際に読み、一致したものをバインドする。デバイスには生バイトしか流れない。換算はここだけで行う。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Probe:
    """同定の手がかり。reg を len バイト読み、expect（16進）と一致すれば候補が強まる。"""
    reg: int
    len: int
    expect: str | None = None          # None なら「応答して CRC が正しい」ことで判定（crc8_sensirion）
    reg_width: int = 8
    crc8_sensirion: bool = False
    note: str = ""

    def args(self, bus: int, addr: int) -> dict:
        a = {"bus": bus, "addr": addr, "reg": self.reg, "len": self.len}
        if self.reg_width == 16:
            a["reg_width"] = 16
        return a


@dataclass(frozen=True)
class Part:
    name: str
    maker: str
    kind: str
    addrs: tuple[int, ...]
    probes: tuple[Probe, ...]            # 上から順に試す。最初のものが一番安全（読むだけ・自動で通る）
    fingerprint: Probe | None            # バインドの指紋（個体ごとに変わる値なら同定時に読んだ値を使う）
    write_allow_regs: tuple[int, ...]    # バインド後に自動で書いてよいレジスタ（設定・読み出しコマンド）
    setup: tuple[dict, ...] = ()         # 計測前の書き込み {"reg", "hex", "note"}
    calibration: Probe | None = None     # 換算に要る校正値の読み出し
    measure: Probe | None = None         # 1回の計測で読むレジスタ（sample.start の reads に入れる）
    decoder: str | None = None
    channels: tuple[str, ...] = ()
    notes: str = ""
    params: dict = field(default_factory=dict)
    fingerprint_recheck: bool = True     # 書き込み前に指紋を読み直すか（読み直せない状態になる部品は False）


PARTS: dict[str, Part] = {p.name: p for p in [
    Part("BMP280", "Bosch", "気温・気圧", (0x76, 0x77),
         probes=(Probe(0xD0, 1, "58", note="chip_id。BME280 なら 60、BMP180 なら 55"),),
         fingerprint=Probe(0xD0, 1, "58"),
         write_allow_regs=(0xF4, 0xF5),
         setup=({"reg": 0xF4, "hex": "27", "note": "ctrl_meas: ノーマルモード、温度・気圧ともオーバーサンプリング x1"},),
         calibration=Probe(0x88, 24, note="dig_T1..dig_P9（リトルエンディアン）"),
         measure=Probe(0xF7, 6, note="press_msb..temp_xlsb"),
         decoder="bmp280", channels=("temperature_c", "pressure_hpa")),
    Part("BME280", "Bosch", "気温・気圧・湿度", (0x76, 0x77),
         probes=(Probe(0xD0, 1, "60", note="chip_id"),),
         fingerprint=Probe(0xD0, 1, "60"),
         write_allow_regs=(0xF2, 0xF4, 0xF5),
         setup=({"reg": 0xF2, "hex": "01", "note": "ctrl_hum: 湿度オーバーサンプリング x1（0xF4 より先に書く）"},
                {"reg": 0xF4, "hex": "27", "note": "ctrl_meas: ノーマルモード、x1/x1"}),
         calibration=Probe(0x88, 26, note="dig_T/dig_P（0x88..0x9F）と dig_H1（0xA1）。dig_H2..H6 は 0xE1 から7バイト"),
         measure=Probe(0xF7, 8, note="press, temp, hum"),
         decoder="bme280", channels=("temperature_c", "pressure_hpa", "humidity_pct"),
         notes="校正値は 0x88 から26バイトと 0xE1 から7バイトの2回読む。デコーダには連結して渡す"),
    Part("SHT30", "Sensirion", "気温・湿度", (0x44, 0x45),
         probes=(Probe(0x3780, 6, reg_width=16, crc8_sensirion=True,
                       note="シリアル番号（コマンド 0x3780）。2ワード＋CRC。16ビットのコマンドなので承認が要る"),),
         fingerprint=Probe(0x3780, 6, reg_width=16, note="シリアル番号。同定時に読んだ値をそのまま指紋にする"),
         # 0xE000（fetch）と 0x22xx（定期計測の開始、2回/秒の各精度）だけ自動。
         # 0x30xx（ソフトリセット・ヒーター ON など）は承認が要るまま
         write_allow_regs=(0x22, 0xE000),
         setup=({"reg": 0x22, "hex": "36", "note": "定期計測 2回/秒（コマンド 0x2236）。以後は 0xE000 で読む。"
                                                   "定期計測中は指紋のコマンドを受け付けないので、バインドの後に書く"},),
         measure=Probe(0xE000, 6, reg_width=16, note="fetch（コマンド 0xE000）。温度・湿度＋CRC"),
         decoder="sht3x", channels=("temperature_c", "humidity_pct"),
         fingerprint_recheck=False,
         notes="SHT31/SHT35 も同じコマンド体系。新しいデータがない瞬間に読むと NACK（値は null）。"
               "定期計測中はシリアル番号のコマンドを受け付けないので、指紋の読み直しはしない（recheck: false）"),
    Part("INA226", "TI", "電圧・電流", tuple(range(0x40, 0x50)),
         probes=(Probe(0xFE, 2, "5449", note="製造者ID（TI）"), Probe(0xFF, 2, "2260", note="ダイID")),
         fingerprint=Probe(0xFE, 2, "5449"),
         write_allow_regs=(0x00, 0x05),
         # レジスタは自動で進まない（3バイト目からは ff）。バス電圧とシャント電圧は2バイトずつ別々に読む
         measure=Probe(0x02, 2, note="バス電圧。電流はシャント電圧 0x01 も2バイト読む"),
         decoder="ina226", channels=("bus_v", "shunt_mv", "current_ma"),
         params={"shunt_ohm": 0.1},
         notes="measure はバス電圧(0x02, 2バイト, 1.25mV/LSB)。電流はシャント電圧(0x01, 2バイト, 2.5µV/LSB)も読み、"
               "decode_value(raw_hex=0x02 の値, shunt_raw_hex=0x01 の値, shunt_ohm) で換算する。"
               "計測ジョブでは reads に 0x02 と 0x01 を2バイトずつ別の name で入れ、get_samples の column にバス電圧、"
               "shunt_column にシャント電圧の name を渡す。レジスタは自動で進まないので、len 4 などでまとめて読まない。"
               "シャント抵抗値は基板で違う（既定 0.1Ω。M5Stack Unit INA226-10A は 5mΩ、INA226-1A は 80mΩ）。"
               "分からなければ推測せず人に聞く"),
    Part("HDC1080", "TI", "気温・湿度", (0x40,),
         probes=(Probe(0xFE, 2, "5449", note="製造者ID（TI）"), Probe(0xFF, 2, "1050", note="デバイスID")),
         fingerprint=Probe(0xFF, 2, "1050"), write_allow_regs=(0x02,),
         notes="0x40 で INA226 と同じ製造者ID。0xFF で区別する"),
    Part("SH1107", "Sino Wealth", "表示（OLED 128x128。Grove OLED 1.12\" など）", (0x3C, 0x3D),
         probes=(Probe(0x00, 1, None, note="状態バイト（表示 OFF なら 40、ON なら 00 が多い）。ID はないので、"
                                            "応答があれば候補。SSD1306 や画面の大きさは区別できないので、人に基板を確認する"),),
         fingerprint=Probe(0x00, 1, note="状態バイト。同定時に読んだ値をそのまま指紋にする"),
         write_allow_regs=(),
         fingerprint_recheck=False,
         notes="表示は display_draw で行う（デバイスが文字を描いて送る）。i2c_write で画面のデータを送らない。"
               "状態バイトは表示の ON/OFF で変わるので、指紋の読み直しはしない（recheck: false）"),
    Part("SH1107_64x128", "Sino Wealth", "表示（OLED 64x128。M5Stack Unit OLED など）", (0x3C, 0x3D),
         probes=(Probe(0x00, 1, None, note="状態バイト（表示 OFF なら 40、ON なら 00 が多い）。ID はないので、"
                                            "応答があれば候補。SSD1306 や画面の大きさは区別できないので、人に基板を確認する"),),
         fingerprint=Probe(0x00, 1, note="状態バイト。同定時に読んだ値をそのまま指紋にする"),
         write_allow_regs=(),
         fingerprint_recheck=False,
         notes="画面は横64×縦128画素（size=1 で1行10文字×16行、size=2 で5文字×8行）。表示は display_draw で行う（デバイスが文字を描いて送る）。i2c_write で画面のデータを送らない。"
               "状態バイトは表示の ON/OFF で変わるので、指紋の読み直しはしない（recheck: false）"),
    Part("INA219", "TI", "電圧・電流", tuple(range(0x40, 0x50)),
         probes=(Probe(0x00, 2, "399f", note="CONFIG のリセット値。ID レジスタがないので決め手にならない"),),
         fingerprint=None, write_allow_regs=(0x00, 0x05),
         notes="ID レジスタがない。0xFE が読めない（NACK）か 5449 でなく、CONFIG が 399f なら可能性がある。人に確認する"),
]}


# 承認カードに出す、部品ごとのレジスタ・コマンドの意味（人向け）。「読むだけ」かどうかも書く。
# 16ビットのコマンドの部品（SHT3x）は、8ビットの reg＋1バイトのデータで書いた場合も上位・下位を合わせて引く。
REG_MEANINGS: dict[str, dict[int, str]] = {
    "BMP280": {0xD0: "部品の種類（chip_id）を読む。読むだけ", 0x88: "校正値を読む。読むだけ",
               0xF4: "計測の設定（モードと精度）", 0xF5: "計測の間隔とフィルタの設定", 0xF7: "測定値（気圧・気温）を読む",
               0xE0: "ソフトリセット", 0xF3: "状態を読む。読むだけ"},
    "BME280": {0xD0: "部品の種類（chip_id）を読む。読むだけ", 0x88: "校正値を読む。読むだけ", 0xE1: "湿度の校正値を読む。読むだけ",
               0xF2: "湿度の精度の設定", 0xF4: "計測の設定（モードと精度）", 0xF5: "計測の間隔とフィルタの設定",
               0xF7: "測定値（気圧・気温・湿度）を読む", 0xE0: "ソフトリセット"},
    "SHT30": {0x3780: "シリアル番号の読み出し（読むだけ。設定は変わらない）", 0xE000: "測定値（気温・湿度）の取り出し",
              0x2236: "定期計測を始める（2回/秒・高精度）", 0x2130: "定期計測を始める（1回/秒・高精度）",
              0x2032: "定期計測を始める（0.5回/秒・高精度）", 0x3093: "定期計測を止める",
              0x30A2: "ソフトリセット", 0x306D: "ヒーターを入れる（センサが温まり、値が上がる）",
              0x3066: "ヒーターを切る", 0xF32D: "状態を読む", 0x3041: "状態をクリアする",
              0x2C06: "1回だけ計測する（高精度）", 0x2400: "1回だけ計測する（高精度）",
              0x22: "定期計測を始める（2回/秒。続く1バイトで精度を選ぶ）", 0x30: "リセット・ヒーター・定期計測の停止（続く1バイトで選ぶ）"},
    "INA226": {0x00: "計測の設定（平均回数・変換時間・モード）", 0x01: "シャント電圧を読む", 0x02: "バス電圧を読む",
               0x03: "電力を読む", 0x04: "電流を読む", 0x05: "校正値（電流の換算）の設定",
               0xFE: "製造者 ID を読む。読むだけ", 0xFF: "ダイ ID を読む。読むだけ"},
    "HDC1080": {0x00: "気温を読む", 0x01: "湿度を読む", 0x02: "設定", 0xFE: "製造者 ID を読む。読むだけ",
                0xFF: "デバイス ID を読む。読むだけ"},
    "INA219": {0x00: "計測の設定", 0x01: "シャント電圧を読む", 0x02: "バス電圧を読む", 0x03: "電力を読む",
               0x04: "電流を読む", 0x05: "校正値の設定"},
}


def reg_meaning(part: str, reg: int, data_hex: str = "") -> str | None:
    """部品 part のレジスタ／コマンド reg の意味。分からなければ None。"""
    table = REG_MEANINGS.get(part, {})
    # 16ビットのコマンドを「8ビットの reg＋1バイトのデータ」で送った場合（SHT30 の 0x22 0x36 など）を先に引く
    if reg <= 0xFF and len(data_hex) >= 2:
        cmd = (reg << 8) | int(data_hex[:2], 16)
        if cmd in table:
            return table[cmd]
    return table.get(reg)


def candidates(addr: int) -> list[Part]:
    return [p for p in PARTS.values() if addr in p.addrs]


def catalog_for(addr: int, bus: int = 1) -> dict:
    """エージェント向け: このアドレスの候補と、それぞれの確かめ方。"""
    out = []
    for p in candidates(addr):
        out.append({
            "part": p.name, "maker": p.maker, "kind": p.kind,
            "probes": [{"op": "i2c.read", "args": pr.args(bus, addr), "expect": pr.expect,
                        "crc_check": pr.crc8_sensirion, "note": pr.note} for pr in p.probes],
            "bind": None if p.fingerprint is None else {
                "fingerprint": {"reg": p.fingerprint.reg, "len": p.fingerprint.len,
                                "hex": p.fingerprint.expect or "<同定時に読んだ値>",
                                "reg_width": p.fingerprint.reg_width, "recheck": p.fingerprint_recheck},
                "write_allow_regs": list(p.write_allow_regs)},
            "setup": [{"op": "i2c.write", "args": {"bus": bus, "addr": addr, **s}} for s in p.setup],
            "calibration": None if p.calibration is None else p.calibration.args(bus, addr),
            "measure": None if p.measure is None else p.measure.args(bus, addr),
            "decoder": p.decoder, "channels": list(p.channels), "notes": p.notes,
        })
    return {"addr": addr, "addr_hex": f"0x{addr:02x}", "candidates": out,
            "unknown_hint": None if out else "カタログにない。読み出しだけで候補を絞り、人に型番を確認する"}


# ---- CRC -------------------------------------------------------------------------------

def crc8_sensirion(data: bytes) -> int:
    c = 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x31) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def sensirion_words(raw: bytes) -> list[int]:
    """2バイト＋CRC の並びを検査してワードの列にする。CRC が合わなければ ValueError。"""
    if len(raw) % 3:
        raise ValueError("length must be a multiple of 3")
    words = []
    for i in range(0, len(raw), 3):
        if crc8_sensirion(raw[i:i + 2]) != raw[i + 2]:
            raise ValueError(f"CRC mismatch at word {i // 3}")
        words.append(int.from_bytes(raw[i:i + 2], "big"))
    return words


# ---- デコーダ -----------------------------------------------------------------------------

class DecodeError(ValueError):
    pass


def _bmp280_tp(cal: bytes, adc_t: int, adc_p: int) -> tuple[float, float, float]:
    """Bosch BMP280/BME280 データシートの浮動小数点の補償式。(温度℃, 気圧Pa, t_fine)。"""
    T1, T2, T3, P1, P2, P3, P4, P5, P6, P7, P8, P9 = struct.unpack("<HhhHhhhhhhhh", cal[:24])
    v1 = (adc_t / 16384.0 - T1 / 1024.0) * T2
    v2 = ((adc_t / 131072.0 - T1 / 8192.0) ** 2) * T3
    t_fine = v1 + v2
    t = t_fine / 5120.0
    v1 = t_fine / 2.0 - 64000.0
    v2 = v1 * v1 * P6 / 32768.0 + v1 * P5 * 2.0
    v2 = v2 / 4.0 + P4 * 65536.0
    v1 = (P3 * v1 * v1 / 524288.0 + P2 * v1) / 524288.0
    v1 = (1.0 + v1 / 32768.0) * P1
    if v1 == 0:
        raise DecodeError("invalid calibration (division by zero)")
    p = 1048576.0 - adc_p
    p = (p - v2 / 4096.0) * 6250.0 / v1
    v1 = P9 * p * p / 2147483648.0
    v2 = p * P8 / 32768.0
    return t, p + (v1 + v2 + P7) / 16.0, t_fine


def _adc20(b: bytes) -> int:
    return (b[0] << 12) | (b[1] << 4) | (b[2] >> 4)


def decode_bmp280(raw: bytes, cal: bytes, **_) -> dict:
    if len(cal) < 24 or len(raw) < 6:
        raise DecodeError("BMP280 needs 24 calibration bytes and 6 data bytes")
    adc_p, adc_t = _adc20(raw[0:3]), _adc20(raw[3:6])
    if adc_t == 0x80000:
        raise DecodeError("no measurement yet (sleep mode: write ctrl_meas 0xF4 first)")
    t, p, _ = _bmp280_tp(cal, adc_t, adc_p)
    return {"temperature_c": round(t, 2), "pressure_hpa": round(p / 100, 2)}


def decode_bme280(raw: bytes, cal: bytes, **_) -> dict:
    """cal は 0x88 から26バイト（dig_H1 は 0xA1＝25バイト目）＋ 0xE1 から7バイト。"""
    if len(cal) < 33 or len(raw) < 8:
        raise DecodeError("BME280 needs 26+7 calibration bytes and 8 data bytes")
    adc_p, adc_t = _adc20(raw[0:3]), _adc20(raw[3:6])
    adc_h = (raw[6] << 8) | raw[7]
    if adc_t == 0x80000:
        raise DecodeError("no measurement yet (sleep mode)")
    t, p, t_fine = _bmp280_tp(cal, adc_t, adc_p)
    H1 = cal[25]
    e = cal[26:33]
    H2 = struct.unpack("<h", e[0:2])[0]
    H3 = e[2]
    H4 = (struct.unpack("b", e[3:4])[0] << 4) | (e[4] & 0x0F)
    H5 = (struct.unpack("b", e[5:6])[0] << 4) | (e[4] >> 4)
    H6 = struct.unpack("b", e[6:7])[0]
    h = t_fine - 76800.0
    h = (adc_h - (H4 * 64.0 + H5 / 16384.0 * h)) * (
        H2 / 65536.0 * (1.0 + H6 / 67108864.0 * h * (1.0 + H3 / 67108864.0 * h)))
    h = h * (1.0 - H1 * h / 524288.0)
    h = min(100.0, max(0.0, h))
    return {"temperature_c": round(t, 2), "pressure_hpa": round(p / 100, 2), "humidity_pct": round(h, 1)}


def decode_sht3x(raw: bytes, **_) -> dict:
    try:
        st, srh = sensirion_words(raw[:6])
    except ValueError as e:
        raise DecodeError(str(e)) from None
    return {"temperature_c": round(-45 + 175 * st / 65535, 2), "humidity_pct": round(100 * srh / 65535, 1)}


def decode_ina226(raw: bytes, *, shunt_raw: bytes | None = None, shunt_ohm: float = 0.1, **_) -> dict:
    """raw はバス電圧レジスタ（0x02）、shunt_raw はシャント電圧レジスタ（0x01、符号付き）。どちらも2バイト。"""
    # 0x01 を4バイト読んで raw に渡すと、シャント電圧をバス電圧として換算してしまう（本番で起きた）。長さで止める
    if len(raw) != 2 or (shunt_raw is not None and len(shunt_raw) != 2):
        raise DecodeError("ina226: raw_hex はバス電圧レジスタ 0x02、shunt_raw_hex はシャント電圧レジスタ 0x01 を"
                          "それぞれ2バイト（16進4桁）で渡す。レジスタは自動で進まないので別々に読む")
    out = {"bus_v": round(int.from_bytes(raw[:2], "big") * 1.25e-3, 4)}
    if shunt_raw is not None:
        sv = struct.unpack(">h", shunt_raw[:2])[0] * 2.5e-6
        out["shunt_mv"] = round(sv * 1000, 4)
        out["current_ma"] = round(sv / shunt_ohm * 1000, 3)
    return out


DECODERS = {"bmp280": decode_bmp280, "bme280": decode_bme280, "sht3x": decode_sht3x, "ina226": decode_ina226}
# 部品名で指定されても通す（モデルは型番を書きがち）
ALIASES = {"sht30": "sht3x", "sht31": "sht3x", "sht35": "sht3x"}


def decode(decoder: str, raw_hex: str, calibration_hex: str = "", **params) -> dict:
    decoder = ALIASES.get(decoder.lower(), decoder.lower())
    fn = DECODERS.get(decoder)
    if fn is None:
        raise DecodeError(f"unknown decoder {decoder!r}; available: {', '.join(sorted(DECODERS))}")
    try:
        raw, cal = bytes.fromhex(raw_hex), bytes.fromhex(calibration_hex)
        extra = {k: (bytes.fromhex(v) if k.endswith("_raw") else v) for k, v in params.items()}
    except ValueError as e:
        raise DecodeError(f"bad hex: {e}") from None
    return fn(raw, cal=cal, **extra)
