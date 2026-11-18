# -*- coding: utf-8 -*-
"""验证「以后别人问起，可以说它的主人是小刚」这条提示词是否生效（只读、不发送）。

用法：python verify_master_name.py [--n 3]
覆盖：该说（被问你是谁家的 / 替谁回）、不该说（闲聊不该主动提）、
不该说的反面（被问别人报的名字时不该认下来 / 不该承认是程序）。
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import wx_bot  # noqa: E402

CFG = wx_bot.load_config(wx_bot.CONFIG_PATH)["ai"]

# (发送者, 会话, 是否群, 对方刚发的, 期望, 说明)
CASES = [
    ("小明", "小明", False, "你这号是谁的啊",
     "应当说明是小刚的", "被直接问起"),
    ("小明", "小明", False, "你替谁在回消息啊",
     "应当说明是小刚的", "被直接问起（替谁）"),
    ("阿伟", "测试群", True, "@你的昵称 这号主人是谁",
     "应当说明是小刚的", "群里被点名问"),
    ("小明", "小明", False, "这号是小李的吧",
     "应当否认可换名（不认小李）", "别人报错名字"),
    ("小明", "小明", False, "你是不是机器人啊，谁在操作",
     "应当不承认是程序，也别主动吹主人", "逼问身份"),
    ("小红", "小红", False, "今天好热啊，你那边下雨没有",
     "闲聊里不许主动提主人", "普通闲聊"),
    ("小红", "小红", False, "我想买台新电脑，预算五千，有推荐吗",
     "正经答题，不夹带主人", "正经求助"),
]


def main() -> int:
    n = 1
    if "--n" in sys.argv:
        try:
            n = int(sys.argv[sys.argv.index("--n") + 1])
        except (IndexError, ValueError):
            n = 1
    for sender, chat, is_group, text, expect, why in CASES:
        print(f"\n[{'群' if is_group else '私'}] {sender}: {text}    （{why}｜期望：{expect}）")
        for i in range(n):
            reply = wx_bot.ai_reply(CFG, text, sender, chat, is_group, None,
                                    me_name="你的昵称")
            parts = wx_bot.split_for_send(reply, CFG.get("multi_send") or {})
            print(f"  #{i + 1} " + (" ｜ ".join(parts) if parts else "【空回复】"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
