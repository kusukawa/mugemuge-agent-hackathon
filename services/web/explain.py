"""承認カードの「どこに・何を」の説明。

デバイスが返した request（op と args）と、そのデバイスのマニフェスト・登録済みの部品・部品カタログだけから作る。
エージェント（LLM）の言葉は使わない（エージェントの説明は承認カードの別の欄に「確認されていない」として出す）。

    explain(request, manifest, bindings) → {"where": [...], "pins": [物理ピン番号], "items": [...]}

物理的な場所は Raspberry Pi の40ピンの番号で示す。「GPIO2」と「ピン3」はどちらも番号なので取り違えやすく、
物理的な番号は必ず「40ピンの○番」と書き分ける。
"""
from __future__ import annotations

from ..agent import parts as parts_mod
from mugemuge_agentd.policy import DISPLAY_PARTS

# GPIO 番号 → 40ピンの物理番号（Raspberry Pi の全モデルで共通）
GPIO_TO_PIN = {2: 3, 3: 5, 4: 7, 14: 8, 15: 10, 17: 11, 18: 12, 27: 13, 22: 15, 23: 16, 24: 18, 10: 19, 9: 21,
               25: 22, 11: 23, 8: 24, 7: 26, 0: 27, 1: 28, 5: 29, 6: 31, 12: 32, 13: 33, 19: 35, 16: 36, 26: 37,
               20: 38, 21: 40}
# I2C バス → (SDA, SCL) の GPIO 番号。バス1 が一般用、バス0 は HAT の ID 用
I2C_GPIO = {1: (2, 3), 0: (0, 1)}


def board_name(manifest: dict) -> str:
    b = (manifest.get("device") or {}).get("board", "")
    if b == "virtual":
        return "仮想デバイス（Raspberry Pi と同じ配置）"
    return "Raspberry Pi"


def _of(name: str) -> str:
    """「Raspberry Pi の」「仮想デバイス（…）の」（括弧のあとに空白を入れない）。"""
    return name + ("の" if name.endswith("）") else " の")


def i2c_where(bus: int, manifest: dict) -> tuple[str, list[int]]:
    name = board_name(manifest)
    if bus in I2C_GPIO:
        sda, scl = I2C_GPIO[bus]
        ps, pc = GPIO_TO_PIN[sda], GPIO_TO_PIN[scl]
        extra = "。HAT の ID 用で、ふだんは使わない" if bus == 0 else ""
        return f"{_of(name)} I2C 端子（40ピンの {ps}番＝SDA・{pc}番＝SCL。GPIO{sda}/GPIO{scl}）{extra}", [ps, pc]
    return f"{_of(name)} I2C バス{bus}", []


def gpio_where(line: int, manifest: dict) -> tuple[str, list[int]]:
    name = board_name(manifest)
    label = next((ln.get("label") for ln in (manifest.get("gpio") or {}).get("lines", []) if ln.get("line") == line), None)
    lab = f"。マニフェストでの名前: {label}" if label else ""
    if line in GPIO_TO_PIN:
        return f"{_of(name)} GPIO{line}（40ピンの {GPIO_TO_PIN[line]}番{lab}）", [GPIO_TO_PIN[line]]
    return f"{_of(name)} GPIO{line}{lab}", []


def camera_where(cam_id: str, manifest: dict) -> str:
    cam = next((c for c in (manifest.get("camera") or {}).get("devices", []) if c.get("id") == cam_id), {})
    label = cam.get("label") or cam_id
    kind = cam.get("kind")
    if kind == "usb":
        return f"USB ポートのカメラ「{label}」（{cam.get('device', '/dev/video0')}）"
    if kind == "csi":
        return f"カメラ端子（CSI）のカメラ「{label}」"
    return f"カメラ「{label}」"


def _target(bus: int, addr: int, bindings: dict) -> tuple[str, list[str]]:
    """(相手の説明, 意味を引く部品名の候補)。登録済みならその部品、未登録ならカタログの候補。"""
    b = bindings.get((bus, addr))
    if b:
        return f"アドレス 0x{addr:02x}（登録済み: {b['part']}）", [b["part"]]
    cands = [p.name for p in parts_mod.candidates(addr)]
    if cands:
        return f"アドレス 0x{addr:02x}（まだ登録されていない部品。部品カタログの候補: {'・'.join(cands)}）", cands
    return f"アドレス 0x{addr:02x}（まだ登録されていない部品。部品カタログに候補なし）", []


def _meaning(parts: list[str], reg: int | None, data_hex: str = "", bound: bool = False) -> str | None:
    if reg is None:
        return None
    found = [(p, m) for p in parts if (m := parts_mod.reg_meaning(p, reg, data_hex))]
    if not found:
        return None
    if bound and len(found) == 1:
        return found[0][1]
    # 未登録なら、本当にその部品かはまだ分からない。「○○なら」と条件を付ける
    return "／".join(f"{p} なら「{m}」" for p, m in found)


def _i2c_item(bus: int, addr: int, reg: int | None, data_hex: str, what: str, bindings: dict) -> dict:
    target, parts = _target(bus, addr, bindings)
    return {"target": target, "what": what,
            "meaning": _meaning(parts, reg, data_hex, bound=(bus, addr) in bindings)}


def _reg_text(reg: int | None, width: int) -> str:
    if reg is None:
        return ""
    return f"コマンド 0x{reg:04x} " if width == 16 else f"レジスタ 0x{reg:02x} "


def explain(request: dict, manifest: dict, bindings_list: list[dict]) -> dict:
    op, a = request.get("op", ""), request.get("args") or {}
    bindings = {(b["bus"], b["addr"]): b for b in bindings_list or [] if "bus" in b and "addr" in b}
    where: list[str] = []
    pins: list[int] = []
    items: list[dict] = []

    def add_where(text: str, ps: list[int]) -> None:
        if text not in where:
            where.append(text)
        pins.extend(p for p in ps if p not in pins)

    if op == "display.draw":
        add_where(*i2c_where(a.get("bus", 1), manifest))
        target, _ = _target(a["bus"], a["addr"], bindings)
        texts = "」「".join(it.get("text", "") for it in a.get("items", []))
        items.append({"target": target, "what": f"画面に「{texts}」を表示する" if texts else "画面を消す",
                      "meaning": "文字の描画と書き込みはデバイスが行う（表示が変わるだけ）"})
    if op in ("i2c.read", "i2c.write", "i2c.bind", "i2c.unbind", "i2c.scan"):
        add_where(*i2c_where(a.get("bus", 1), manifest))
        if op == "i2c.read":
            w = a.get("reg_width", 8)
            if a.get("reg") is None:
                what = f"{a.get('len')} バイト読む"
            elif w == 16:
                what = f"{_reg_text(a['reg'], 16)}を送り {a.get('len')} バイト読む"
            else:
                what = f"{_reg_text(a['reg'], 8)}から {a.get('len')} バイト読む"
            items.append(_i2c_item(a["bus"], a["addr"], a.get("reg"), "", what, bindings))
        elif op == "i2c.write":
            what = f"{_reg_text(a.get('reg'), a.get('reg_width', 8))}に {a.get('hex')} を書き込む"
            items.append(_i2c_item(a["bus"], a["addr"], a.get("reg"), a.get("hex", ""), what, bindings))
        elif op == "i2c.bind":
            target, _ = _target(a["bus"], a["addr"], bindings)
            auto = [(f"0x{r:04x}" if r > 0xFF else f"0x{r:02x}") + (f"＝{m}" if (m := parts_mod.reg_meaning(a["part"], r)) else "")
                    for r in a.get("write_allow_regs", [])]
            meaning = ("登録すると、次の書き込みは承認なしで通る: " + "、".join(auto)) if auto else None
            if a["part"] in DISPLAY_PARTS:
                meaning = "登録すると、この画面に文字を表示できる（承認なしで通すかは Pi のマニフェストの bound.display による）"
            items.append({"target": target, "what": f"この部品を {a['part']} として登録する", "meaning": meaning})
    elif op in ("sample.start", "log.start"):
        for r in a.get("reads", []):
            add_where(*i2c_where(r["bus"], manifest))
            what = f"「{r.get('name')}」として {_reg_text(r.get('reg'), r.get('reg_width', 8))}から {r.get('len')} バイトを繰り返し読む"
            items.append(_i2c_item(r["bus"], r["addr"], r.get("reg"), "", what, bindings))
        if op == "log.start" and a.get("photo"):
            add_where(camera_where(a["photo"]["camera"], manifest), [])
    elif op in ("gpio.write", "gpio.read"):
        add_where(*gpio_where(a.get("line", -1), manifest))
    elif op == "camera.capture":
        add_where(camera_where(a.get("camera", ""), manifest), [])
    return {"where": where, "pins": pins, "items": items}


PIN_ROLE = {3: "SDA", 5: "SCL", 27: "ID_SD", 28: "ID_SC"}


def pins_svg(pins: list[int]) -> str:
    """40ピンを横長に描き、pins を黄色で目立たせた図と、その下の説明文。数字と決まった文言だけから作るので、
    そのまま HTML に埋めてよい。

    基板を上から見て、40ピンのヘッダーを上の辺に置いた向き（左下が1番、上の列が偶数）。
    """
    if not pins:
        return ""
    step, r = 24, 10
    w = 20 * step + 16
    out = [f'<div class="pins40"><svg viewBox="0 0 {w} 64" role="img" '
           f'aria-label="40ピンのうち {"・".join(f"{p}番" for p in pins)}">',
           f'<rect x="2" y="2" width="{w - 4}" height="60" rx="6" fill="#1f6f43"/>']
    for n in range(1, 41):
        col, top = (n - 1) // 2, n % 2 == 0
        cx, cy = 20 + col * step, 18 if top else 46
        on = n in pins
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{r if on else r - 2}" fill="{"#fab219" if on else "#d9d6cf"}" '
                   f'stroke="{"#111" if on else "none"}" stroke-width="2"><title>{n}番ピン</title></circle>')
        if on:
            out.append(f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" font-size="11" font-weight="700" '
                       f'fill="#111">{n}</text>')
    out.append("</svg>")
    names = "・".join(f"{n}番" + (f"（{PIN_ROLE[n]}）" if n in PIN_ROLE else "") for n in pins)
    out.append(f'<div class="meta">黄色: {names}。基板を上から見て、40ピンを上の辺に置いた向き（左下が1番）</div></div>')
    return "".join(out)
