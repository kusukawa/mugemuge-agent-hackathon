"""承認カードの「どこに・何を」: 40ピンの番号、相手の部品、コマンドの意味。エージェントの説明は別の欄。"""
import asyncio
import re

import yaml
from test_web import Cloud

from services.web.explain import explain, pins_svg

M = yaml.safe_load(open("examples/manifest.pi-grove.yaml"))


def test_i2c_command_on_unknown_address_names_pins_and_meaning():
    ex = explain({"op": "i2c.read", "args": {"bus": 1, "addr": 0x44, "reg": 0x3780, "reg_width": 16, "len": 6}}, M, [])
    assert ex["where"] == ["Raspberry Pi の I2C 端子（40ピンの 3番＝SDA・5番＝SCL。GPIO2/GPIO3）"] and ex["pins"] == [3, 5]
    it = ex["items"][0]
    assert "まだ登録されていない" in it["target"] and "SHT30" in it["target"]
    assert it["what"] == "コマンド 0x3780 を送り 6 バイト読む"
    # 未登録なので「SHT30 なら」と条件付き
    assert it["meaning"] == "SHT30 なら「シリアル番号の読み出し（読むだけ。設定は変わらない）」"


def test_bound_part_command_split_into_reg_and_data():
    ex = explain({"op": "i2c.write", "args": {"bus": 1, "addr": 0x44, "reg": 0x22, "hex": "36"}}, M,
                 [{"bus": 1, "addr": 0x44, "part": "SHT30"}])
    assert ex["items"][0]["target"] == "アドレス 0x44（登録済み: SHT30）"
    assert ex["items"][0]["meaning"] == "定期計測を始める（2回/秒・高精度）"


def test_gpio_camera_and_jobs():
    ex = explain({"op": "gpio.write", "args": {"line": 17, "value": 1}}, M, [])
    assert ex["where"] == ["Raspberry Pi の GPIO17（40ピンの 11番。マニフェストでの名前: relay-1）"] and ex["pins"] == [11]
    ex = explain({"op": "camera.capture", "args": {"camera": "cam0"}}, M, [])
    assert ex["where"][0].startswith("USB ポートのカメラ「卓上カメラ」") and ex["pins"] == []
    ex = explain({"op": "log.start", "args": {"name": "x", "reads": [
        {"name": "env", "bus": 1, "addr": 0x76, "reg": 0xF7, "len": 6}], "photo": {"camera": "cam0"}}}, M,
        [{"bus": 1, "addr": 0x76, "part": "BMP280"}])
    assert len(ex["where"]) == 2 and ex["items"][0]["meaning"] == "測定値（気圧・気温）を読む"
    virt = {**M, "device": {**M["device"], "board": "virtual"}}
    assert explain({"op": "gpio.read", "args": {"line": 27}}, virt, [])["where"][0].startswith(
        "仮想デバイス（Raspberry Pi と同じ配置）の GPIO27（40ピンの 13番")
    svg = pins_svg([3, 5])
    assert svg.count('fill="#fab219"') == 2 and "黄色: 3番（SDA）・5番（SCL）" in svg and "<script" not in svg
    assert pins_svg([]) == ""


def test_card_shows_facts_and_agent_purpose_separately(manifest, signing_key, tmp_path):
    """エージェントの purpose は保存され、カードでは確認済みの内容とは別の欄に、エスケープして出る。"""
    async def go():
        async with Cloud(manifest, signing_key, tmp_path) as c:
            r = await c.agent.exec("gpio.write", {"line": 17, "value": 1, "hold_ms": 500})
            from cloudrig import client
            async with client(c.web.url) as w:
                body = {**r["error"]["request"], "run_id": "r", "purpose": "リレーを試す <b>ため</b>\n" + "x" * 300}
                aid = (await w.post("/api/approvals", json=body)).json()["approval_id"]
            assert len(c.store.approvals[aid]["purpose"]) == 200 and "\n" not in c.store.approvals[aid]["purpose"]
            assert c.store.approvals[aid]["explain"]["pins"] == [11]     # 求められた時点の説明を残す
            async with c.browser() as b:
                card = (await b.get(f"/ui/approvals/{aid}")).json()
                assert card["explain"]["pins"] == [11] and card["purpose"].startswith("リレーを試す <b>ため</b>")
                assert "11" in card["pins_svg"]
                html = (await b.get("/")).text
                assert "エージェントの説明（確認されていません）" in html and "&lt;b&gt;ため&lt;/b&gt;" in html
                assert "40ピンの 11番" in html and "詳細（生の引数）" in html
                assert re.search(r"facts.*claim", html, re.S)
    asyncio.run(go())
