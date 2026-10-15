"""「現在の状態」のテスター表示（画面で状態だけを最大化したとき）。

同定済み（バインド済み）の部品を、カタログの measure レジスタで読んで、クラウドのデコーダで換算する。
送るのは自動で許可される読み出し（バインド済みの read、write_allow_regs に入っている 16 ビットのコマンド）と
gpio.read だけ。部品がまだ計測できる状態でなければ（BMP280 がスリープ中、SHT30 が定期計測でない）その旨を返す。
"""
from __future__ import annotations

import asyncio
import itertools

from ..agent import parts as parts_mod

_step = itertools.count(1)


def part_params(device: dict, bus: int, addr: int) -> dict:
    """部品ごとの換算の設定（INA226 のシャント抵抗など）。人がテスター画面で決め、デバイスの文書に残す。"""
    return (device.get("part_params") or {}).get(f"{bus}_{addr}", {})


class Tester:
    def __init__(self, gateway):
        self.gateway = gateway
        self.calibration: dict[tuple, str] = {}       # (device, bus, addr, 指紋) → 校正値（部品が同じ間は変わらない）

    async def _read(self, device_id: str, uid: str, args: dict) -> tuple[str | None, str | None]:
        status, res = await self.gateway.exec({"device_id": device_id, "op": "i2c.read", "args": args,
                                               "run_id": f"ui-tester:{uid}"[:64], "step_id": f"tester-{next(_step)}"})
        if status != 200:
            return None, res.get("error", f"http {status}")
        if not res.get("ok"):
            return None, res["error"]["code"]
        return res["data"]["hex"], None

    async def _part(self, device_id: str, uid: str, b: dict, params: dict | None = None) -> dict | None:
        part = parts_mod.PARTS.get(b["part"])
        out = {"bus": b["bus"], "addr": b["addr"], "part": b["part"], "kind": part.kind if part else "",
               "values": {}, "note": None}
        if part is None or part.decoder is None or part.measure is None:
            out["note"] = "この部品の換算方法はまだない"
            return out
        bus, addr = b["bus"], b["addr"]
        cal = ""
        if part.calibration is not None:
            key = (device_id, bus, addr, b["fingerprint"]["hex"])
            cal = self.calibration.get(key, "")
            if not cal:
                cal, err = await self._read(device_id, uid, part.calibration.args(bus, addr))
                if err:
                    out["note"] = f"校正値を読めない（{err}）"
                    return out
                self.calibration[key] = cal
        if part.decoder == "ina226":
            bus_v, e1 = await self._read(device_id, uid, {"bus": bus, "addr": addr, "reg": 0x02, "len": 2})
            shunt, e2 = await self._read(device_id, uid, {"bus": bus, "addr": addr, "reg": 0x01, "len": 2})
            if e1 or e2:
                out["note"] = f"読めない（{e1 or e2}）"
                return out
            # シャント抵抗は基板で違う（0.1Ω・5mΩ・80mΩ…）。人がテスター画面で設定するまで電流は出さない
            ohm = (params or {}).get("shunt_ohm")
            out["shunt_ohm"] = ohm
            v = parts_mod.decode("ina226", bus_v, shunt_raw=shunt, shunt_ohm=ohm or 1.0)
            if not ohm:
                v.pop("current_ma")
                out["note"] = "電流はシャント抵抗を設定すると出ます"
            out["values"] = v
            return out
        raw, err = await self._read(device_id, uid, part.measure.args(bus, addr))
        if err:
            out["note"] = ("計測モードではない（設定の書き込み前）" if part.decoder == "sht3x" and err == "io_error"
                           else f"読めない（{err}）")
            return out
        try:
            out["values"] = parts_mod.decode(part.decoder, raw, cal)
        except parts_mod.DecodeError as e:
            out["note"] = "スリープ中（設定の書き込み前）" if "sleep" in str(e) else f"換算できない（{e}）"
        return out

    async def _gpio(self, device_id: str, uid: str, ln: dict) -> dict:
        status, res = await self.gateway.exec({"device_id": device_id, "op": "gpio.read", "args": {"line": ln["line"]},
                                               "run_id": f"ui-tester:{uid}"[:64], "step_id": f"tester-{next(_step)}"})
        value = res["data"]["value"] if status == 200 and res.get("ok") else None
        return {"line": ln["line"], "label": ln.get("label"), "direction": ln["direction"], "value": value,
                "safe_value": ln.get("safe_value"), "max_on_ms": ln.get("max_on_ms")}

    async def read_all(self, device: dict, uid: str) -> dict:
        device_id = device["device_id"]
        bindings = (device.get("state") or {}).get("bindings", [])
        lines = (device.get("manifest") or {}).get("gpio", {}).get("lines", [])
        parts_res, gpio_res = await asyncio.gather(
            asyncio.gather(*(self._part(device_id, uid, b, part_params(device, b["bus"], b["addr"])) for b in bindings)),
            asyncio.gather(*(self._gpio(device_id, uid, ln) for ln in lines)))
        return {"readings": [r for r in parts_res if r], "gpio": list(gpio_res)}
