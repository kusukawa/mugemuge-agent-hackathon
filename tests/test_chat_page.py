"""チャット画面の「はじめに」: 会話が空のときは、押すだけで始められる選択肢を出す（いきなり文字を打たせない）。"""
import json
import re

from services.web.chat_page import START, chat_page


def cfg(html: str) -> dict:
    return json.loads(re.search(r'<script type="application/json" id="cfg">(.*?)</script>', html, re.S).group(1))


def test_start_choices_are_embedded_and_first_step_is_identification():
    c = cfg(chat_page(device_id="kusupi5", chat_id="c1", device_name="卓上", virtual=False, csrf="x", uid="u",
                      trace_project=""))
    assert c["start"] == START and "何がつながっているか" in c["start"][0]["text"]
    assert not any("安全装置" in t["label"] for t in c["start"])       # 実機にはリレーがないかもしれない


def test_virtual_device_also_offers_the_safety_test():
    c = cfg(chat_page(device_id="virt-1", chat_id="c1", device_name="仮想", virtual=True, csrf="x", uid="u",
                      trace_project=""))
    assert c["virtual"] and any("安全装置" in t["label"] for t in c["start"])
