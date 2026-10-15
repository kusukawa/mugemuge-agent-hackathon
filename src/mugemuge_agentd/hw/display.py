"""表示デバイス（PROTOCOL.md 5.1節 display.draw）。

エージェントは「どこに何を出すか」だけを宣言し、文字の描画と I2C への書き込みはここで行う
（LLM にビットマップや書き込みの列を作らせない）。I2C の読み書きは I2CBus を通すので、
テストと仮想デバイスでは FakeI2C の上でそのまま動く。
"""
from __future__ import annotations

from ..policy import DISPLAY_PARTS
from .i2c import I2CBus

# 5x7 の ASCII フォント（0x20〜0x7E）。1文字5バイト、列ごと、下位ビットが上
_FONT = bytes.fromhex(
    "0000000000" "00005f0000" "0007000700" "147f147f14" "242a7f2a12" "2313086462" "3649552250" "0005030000"
    "001c224100" "0041221c00" "082a1c2a08" "08083e0808" "0050300000" "0808080808" "0060600000" "2010080402"
    "3e5149453e" "00427f4000" "4261514946" "2141454b31" "1814127f10" "2745454539" "3c4a494930" "0171090503"
    "3649494936" "064949291e" "0036360000" "0056360000" "0814224100" "1414141414" "4122140800" "0201510906"
    "324979413e" "7e1111117e" "7f49494936" "3e41414122" "7f4141221c" "7f49494941" "7f09090101" "3e41415132"
    "7f0808087f" "00417f4100" "2040413f01" "7f08142241" "7f40404040" "7f0204027f" "7f0408107f" "3e4141413e"
    "7f09090906" "3e4151215e" "7f09192946" "4649494931" "01017f0101" "3f4040403f" "1f2040201f" "7f2018207f"
    "6314081463" "0304780403" "6151494543" "007f414100" "0204081020" "0041417f00" "0402010204" "4040404040"
    "0001020400" "2054545478" "7f48444438" "3844444420" "384444487f" "3854545418" "087e090102" "081454543c"
    "7f08040478" "00447d4000" "2040443d00" "007f102844" "00417f4000" "7c04180478" "7c08040478" "3844444438"
    "7c14141408" "081414187c" "7c08040408" "4854545420" "043f444020" "3c4040207c" "1c2040201c" "3c4030403c"
    "4428102844" "0c5050503c" "4464544c44" "0008364100" "00007f0000" "0041360800" "0201020402"
)
_DEGREE = bytes.fromhex("0006090906")
_UNKNOWN = _FONT[(ord("?") - 0x20) * 5:(ord("?") - 0x20) * 5 + 5]


def glyph(ch: str) -> bytes:
    """1文字の5列。表にない文字は「?」（℃ は呼び出し側で °C に直す）。"""
    if ch == "°":
        return _DEGREE
    c = ord(ch)
    if 0x20 <= c <= 0x7E:
        return _FONT[(c - 0x20) * 5:(c - 0x20) * 5 + 5]
    return _UNKNOWN


class Framebuffer:
    """1ビットの画面。pages[p][x] の bit (y % 8) が (x, y) の画素（SSD1306/SH110x と同じ並び）。"""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.pages = [bytearray(width) for _ in range(height // 8)]

    def pixel(self, x: int, y: int, on: bool = True) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            if on:
                self.pages[y // 8][x] |= 1 << (y % 8)
            else:
                self.pages[y // 8][x] &= ~(1 << (y % 8)) & 0xFF

    def get(self, x: int, y: int) -> bool:
        return bool(self.pages[y // 8][x] >> (y % 8) & 1)

    def text(self, x: int, y: int, s: str, size: int = 1) -> None:
        """左上 (x, y) から文字列を描く。1文字は (6×size)×(8×size) 画素。はみ出した部分は切る。"""
        s = s.replace("℃", "°C")
        for i, ch in enumerate(s):
            for col, bits in enumerate(glyph(ch)):
                for row in range(7):
                    if bits >> row & 1:
                        for dx in range(size):
                            for dy in range(size):
                                self.pixel(x + (i * 6 + col) * size + dx, y + row * size + dy)


# SH1107（128x128 と 64x128 で共通。64x128 はマルチプレクス 128 のまま、書き込む列だけをずらす）。制御バイト 0x00 はコマンドの列、0x40 は表示データの列
SH1107_INIT = bytes([
    0xAE,              # 表示 OFF
    0xD5, 0x51,        # クロック
    0x20,              # ページアドレッシング
    0x81, 0x4F,        # コントラスト
    0xAD, 0x8A,        # DC-DC
    0xA0, 0xC0,        # 左右・上下の向き
    0xDC, 0x00,        # 開始ライン
    0xD3, 0x00,        # 表示オフセット
    0xD9, 0x22,        # プリチャージ
    0xDB, 0x35,        # VCOM
    0xA8, 0x7F,        # マルチプレクス比 128
    0xA4, 0xA6,        # RAM の内容を表示、白黒反転なし
    0xAF,              # 表示 ON
])

CHUNK = 128            # 1回のデータ書き込みのバイト数（ページ1本分）


def draw(i2c: I2CBus, bus: int, addr: int, part: str, items: list[dict], clear: bool = True,
         fb: Framebuffer | None = None) -> dict:
    """items（{"text","x","y","size"}）を描いて画面に送る。毎回初期化も送る（電源の入れ直しに強くするため）。"""
    w, h, col0 = DISPLAY_PARTS[part]
    fb = fb if fb is not None and not clear else Framebuffer(w, h)
    for it in items:
        fb.text(it.get("x", 0), it.get("y", 0), it["text"], it.get("size", 1))
    i2c.write(bus, addr, 0x00, SH1107_INIT)
    written = len(SH1107_INIT)
    for p, page in enumerate(fb.pages):
        i2c.write(bus, addr, 0x00, bytes([0xB0 | p, col0 & 0x0F, 0x10 | col0 >> 4]))   # ページ p、列 col0 から
        for i in range(0, len(page), CHUNK):
            i2c.write(bus, addr, 0x40, bytes(page[i:i + CHUNK]))
        written += 3 + len(page)
    return {"bytes": written, "fb": fb}
