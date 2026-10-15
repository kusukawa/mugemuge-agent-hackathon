"""実機確認: BMP280(0x76) と SHT30(0x44) をバインドして計測する（docs/PLAN.md M1 のメモ）。

I2C に書き込む（BMP280 の ctrl_meas 0xF4=0x27、SHT30 の定期計測開始 0x2130）ので、実行前に配線を確認すること。
承認は stub_gateway の例専用の鍵で自動的に署名する。値の換算はこのスクリプトの中だけで行う（デーモンは生バイトのみ）。

    PYTHONPATH=src python3 tools/hw_check_env_sensors.py &      # ポート 8767 で待つ
    python3 -m mugemuge_agentd run --manifest <gpio.lines: [] の開発用マニフェスト> \
        --gateway-url ws://127.0.0.1:8767/v1/device --audit-path /tmp/audit-hw.jsonl
"""
import asyncio
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stub_gateway import StubGateway  # noqa: E402


def show(label, r):
    pol = r["policy"]
    body = json.dumps(r["data"], ensure_ascii=False) if r["ok"] else f"{r['error']['code']}: {r['error']['message']}"
    print(f"{label:<44} [{pol['decision']:<17} {pol['rule']}] {body}")
    if not r["ok"] and "request" in r["error"]:
        print(f"{'':<44}  承認カード: {r['error']['request']['summary']}")
    return r


async def need_approval(gw, label, op, args):
    r = show(label, await gw.exec(op, args))
    assert r["error"]["code"] == "approval_required", r
    return show("  → approve", await gw.approve())


def bmp280_compensate(cal: bytes, raw: bytes):
    T1, T2, T3, P1, P2, P3, P4, P5, P6, P7, P8, P9 = struct.unpack("<HhhHhhhhhhhh", cal)
    adc_p = (raw[0] << 12) | (raw[1] << 4) | (raw[2] >> 4)
    adc_t = (raw[3] << 12) | (raw[4] << 4) | (raw[5] >> 4)
    v1 = (adc_t / 16384 - T1 / 1024) * T2
    v2 = ((adc_t / 131072 - T1 / 8192) ** 2) * T3
    t_fine = v1 + v2
    t = t_fine / 5120
    v1 = t_fine / 2 - 64000
    v2 = v1 * v1 * P6 / 32768 + v1 * P5 * 2
    v2 = v2 / 4 + P4 * 65536
    v1 = (P3 * v1 * v1 / 524288 + P2 * v1) / 524288
    v1 = (1 + v1 / 32768) * P1
    p = 1048576 - adc_p
    p = (p - v2 / 4096) * 6250 / v1
    v1 = P9 * p * p / 2147483648
    v2 = p * P8 / 32768
    p = p + (v1 + v2 + P7) / 16
    return t, p / 100


def crc8(data):
    c = 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x31) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def sht30_decode(raw: bytes):
    assert crc8(raw[0:2]) == raw[2] and crc8(raw[3:5]) == raw[5], "CRC mismatch"
    t = -45 + 175 * int.from_bytes(raw[0:2], "big") / 65535
    rh = 100 * int.from_bytes(raw[3:5], "big") / 65535
    return t, rh


async def main():
    gw = StubGateway(port=8767)
    await gw.start()
    print("waiting for daemon…")
    await asyncio.wait_for(gw.connected.wait(), 15)
    print(f"connected: {gw.hello['device_id']} state={gw.hello['state']}\n")

    show("i2c.scan", await gw.exec("i2c.scan", {"bus": 1}))

    # ---- BMP280 ------------------------------------------------------------
    await need_approval(gw, "BMP280 bind (fp 0xD0=58, auto 0xF4/0xF5)", "i2c.bind",
                        {"bus": 1, "addr": 0x76, "part": "BMP280", "fingerprint": {"reg": 0xD0, "len": 1, "hex": "58"},
                         "write_allow_regs": [0xF4, 0xF5]})
    show("BMP280 write 0xF4=0x27 (normal, x1/x1)", await gw.exec("i2c.write",
                                                                 {"bus": 1, "addr": 0x76, "reg": 0xF4, "hex": "27"}))
    cal = bytes.fromhex(show("BMP280 read calib 0x88 len24", await gw.exec(
        "i2c.read", {"bus": 1, "addr": 0x76, "reg": 0x88, "len": 24}))["data"]["hex"])

    # ---- SHT30 -------------------------------------------------------------
    r = await need_approval(gw, "SHT30 read serial (cmd 0x3780, 16bit)", "i2c.read",
                            {"bus": 1, "addr": 0x44, "reg": 0x3780, "len": 6, "reg_width": 16})
    sht_bound = False
    if r["ok"]:
        serial = r["data"]["hex"]
        r = await need_approval(gw, "SHT30 bind (fp serial, auto cmd 0xE000)", "i2c.bind",
                                {"bus": 1, "addr": 0x44, "part": "SHT30",
                                 "fingerprint": {"reg": 0x3780, "len": 6, "hex": serial},
                                 "write_allow_regs": [0xE000]})
        sht_bound = r["ok"]
    await need_approval(gw, "SHT30 write 0x21 0x30 (periodic 1 mps)", "i2c.write",
                        {"bus": 1, "addr": 0x44, "reg": 0x21, "hex": "30"})
    await asyncio.sleep(1.3)
    fetch = {"bus": 1, "addr": 0x44, "reg": 0xE000, "len": 6, "reg_width": 16}
    r = show("SHT30 fetch (cmd 0xE000)", await gw.exec("i2c.read", fetch))
    if r["ok"]:
        t, rh = sht30_decode(bytes.fromhex(r["data"]["hex"]))
        print(f"{'':<44}  → {t:.2f} °C, {rh:.1f} %RH")

    raw = bytes.fromhex(show("BMP280 read 0xF7 len6", await gw.exec(
        "i2c.read", {"bus": 1, "addr": 0x76, "reg": 0xF7, "len": 6}))["data"]["hex"])
    t, p = bmp280_compensate(cal, raw)
    print(f"{'':<44}  → {t:.2f} °C, {p:.2f} hPa")

    # ---- 計測ジョブ ----------------------------------------------------------
    reads = [{"name": "bmp", "bus": 1, "addr": 0x76, "reg": 0xF7, "len": 6}]
    if sht_bound:
        reads.append({"name": "sht", **fetch})
    r = show("sample.start 1000ms x 6s", await gw.exec("sample.start", {
        "period_ms": 1000, "duration_ms": 6000, "batch_ms": 2000, "reads": reads}))
    while not gw.events("job_end"):
        await asyncio.sleep(0.2)
    print()
    for m in gw.inbox:
        if m.get("type") != "samples":
            continue
        for row in m["rows"]:
            parts = [f"t={row[0]:>5}ms"]
            if row[1]:
                bt, bp = bmp280_compensate(cal, bytes.fromhex(row[1]))
                parts.append(f"BMP280 {bt:6.2f}°C {bp:8.2f}hPa")
            else:
                parts.append("BMP280 null")
            if len(row) > 2:
                if row[2]:
                    st, srh = sht30_decode(bytes.fromhex(row[2]))
                    parts.append(f"SHT30 {st:6.2f}°C {srh:5.1f}%RH")
                else:
                    parts.append("SHT30 null")
            print("  " + "  ".join(parts))
    print(f"job_end: {gw.events('job_end')[0]['detail']}")
    print(f"schema violations: {len(gw.violations)}")
    await gw.stop()


asyncio.run(main())
