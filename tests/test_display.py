"""表示（display.draw）: エージェントは内容だけを送り、描画と書き込みはデバイスが行う。判定は gate に集約。"""
import asyncio

from conftest import T0_MS, check, manifest_variant
from test_executor import Rig, code

from mugemuge_agentd.gate import Gate
from mugemuge_agentd.hw.display import _FONT, SH1107_INIT, Framebuffer, glyph
from mugemuge_agentd.hw.i2c import sh1107
from mugemuge_agentd.policy import Binding

OLED = 0x3C
DRAW = {"bus": 1, "addr": OLED, "items": [{"text": "23.4℃", "x": 0, "y": 0, "size": 2}]}


def sh1107_binding(part="SH1107"):
    return Binding(bus=1, addr=OLED, part=part, fingerprint={"reg": 0, "len": 1, "hex": "40", "recheck": False},
                   write_allow_regs=[], last_verified_ms=T0_MS)


def allow_display(raw):
    for b in raw["i2c"]["buses"]:
        b.setdefault("bound", {})["display"] = "allow"


def test_font_covers_printable_ascii():
    assert len(_FONT) == 95 * 5
    assert glyph("A") == bytes.fromhex("7e1111117e") and glyph("あ") == glyph("?")


def test_framebuffer_text_scales_and_clips():
    fb = Framebuffer(128, 128)
    fb.text(0, 0, "I", 2)                  # 「I」の縦棒は3列目（00 41 7f 41 00）
    assert fb.get(4, 0) and fb.get(5, 13) and not fb.get(0, 0)
    fb.text(125, 125, "W")                 # はみ出しても落ちない
    assert sum(map(sum, fb.pages)) > 0


def test_policy_requires_a_display_binding_and_manifest_permission(manifest, raw_manifest, state):
    g = Gate(manifest, state, now_ms=T0_MS)
    v = check(g, "display.draw", DRAW)
    assert v.error_code == "policy_denied" and v.detail["reason"] == "display.not_bound"
    state.bindings[(1, OLED)] = sh1107_binding("INA226")
    assert check(g, "display.draw", DRAW).detail["reason"] == "display.unsupported_part"
    state.bindings[(1, OLED)] = sh1107_binding()
    v = check(g, "display.draw", DRAW)             # マニフェストに書いていなければ毎回承認
    assert v.error_code == "approval_required" and "23.4℃" in v.request["summary"]
    g2 = Gate(manifest_variant(raw_manifest, allow_display), state, now_ms=T0_MS)
    assert check(g2, "display.draw", DRAW).decision == "allow"


def test_draw_on_fake_sh1107_writes_init_and_16_pages(raw_manifest, state, tmp_path):
    rig = Rig(manifest_variant(raw_manifest, allow_display), state, tmp_path)
    dev = rig.i2c.devices[1][OLED] = sh1107()
    state.bindings[(1, OLED)] = sh1107_binding()

    async def go():
        r = await rig.run("display.draw", DRAW)
        assert r["ok"], r
        assert r["data"] == {"drawn": 1, "bytes": len(SH1107_INIT) + 16 * (3 + 128)}
        assert dev.writes[0] == (0x00, SH1107_INIT)
        assert [w[0] for w in dev.writes[1:]] == [0x00, 0x40] * 16
        assert dev.writes[1][1] == bytes([0xB0, 0x00, 0x10]) and len(dev.writes[2][1]) == 128
        assert any(any(w[1]) for w in dev.writes[2::2])          # 何か描かれている
        # clear: false は前回の画面に重ねる
        before = sum(map(sum, rig.ex._screens[(1, OLED)].pages))
        r = await rig.run("display.draw", {"bus": 1, "addr": OLED, "clear": False, "items": [{"text": "x", "y": 100}]})
        assert r["ok"] and sum(map(sum, rig.ex._screens[(1, OLED)].pages)) > before
        # 上限（既定 2枚/秒、バースト 4）を超えると rate_limited
        codes = [code(await rig.run("display.draw", DRAW)) for _ in range(4)]
        assert "rate_limited" in codes
    asyncio.run(go())


def test_draw_is_blocked_during_estop(raw_manifest, state, tmp_path):
    rig = Rig(manifest_variant(raw_manifest, allow_display), state, tmp_path)
    rig.i2c.devices[1][OLED] = sh1107()
    state.bindings[(1, OLED)] = sh1107_binding()

    async def go():
        await rig.run("sys.estop", {})
        assert code(await rig.run("display.draw", DRAW)) == "estop_active"
    asyncio.run(go())


def test_agent_tool_maps_to_the_op_and_card_explains_it():
    from services.agent.device_tools import op_and_args
    from services.agent.parts import catalog_for
    from services.web.explain import explain
    op, a = op_and_args("display_draw", {"bus": 1, "addr": 60, "items": [{"text": "T 23.4°C", "size": 2.0}],
                                         "clear": True})
    assert op == "display.draw" and a == {"bus": 1, "addr": 60, "items": [{"text": "T 23.4°C", "size": 2}]}
    cands = catalog_for(0x3C)["candidates"]
    assert [c["part"] for c in cands] == ["SH1107", "SH1107_64x128"]
    cand = cands[0]
    assert cand["bind"]["fingerprint"]["recheck"] is False
    ex = explain({"op": op, "args": a}, {}, [{"bus": 1, "addr": 60, "part": "SH1107"}])
    assert "T 23.4°C" in ex["items"][0]["what"]


def test_64x128_panel_writes_from_column_32(raw_manifest, state, tmp_path):
    rig = Rig(manifest_variant(raw_manifest, allow_display), state, tmp_path)
    dev = rig.i2c.devices[1][OLED] = sh1107()
    state.bindings[(1, OLED)] = sh1107_binding("SH1107_64x128")

    async def go():
        r = await rig.run("display.draw", DRAW)
        assert r["ok"] and r["data"]["bytes"] == len(SH1107_INIT) + 16 * (3 + 64)
        assert dev.writes[1][1] == bytes([0xB0, 0x00, 0x12]) and len(dev.writes[2][1]) == 64   # 列 32 から64バイト
    asyncio.run(go())
