# -*- coding: utf-8 -*-
"""自查：主人点名不要的几种句式（2026-11-18，§7.43）。

覆盖：睡吧我守着 / 这话我记下了 / 这话我心里有数 / 你这话是在问别人还是在问自己 /
行，这话我认 / 这话你自己信吗 / 这笔账我记着 / 我听着你往下讲。
只读、不发送：python check_banned_phrases.py [--n 3]
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import wx_bot  # noqa: E402

# 命中即失败（宽一点：同义变体也算，见提示词【这几句别说】）
BANNED = [
    "睡吧", "守着", "看着你睡", "陪你到天亮",
    "我记下了", "记下了", "记着", "这笔账",
    "心里有数",
    "在问别人", "问自己", "问问你自己",
    "这话我认", "我认",
    "你信吗", "你自己信", "信不信",
    "往下讲", "你说，我听着", "我听着你",
]

CASES = [
    ("小明", False, "晚安，我去睡了", "对方去睡 → 不能「我守着」"),
    ("小明", False, "那行吧，就按你说的办", "对方拍板 → 不能记账式回话"),
    ("小明", False, "你昨天是不是又说谎了？", "被质问 → 不能反问「这话你自己信吗」"),
    ("小明", False, "我今天面试黄了，跟HR吵了一架", "对方倾诉 → 不能「我听着你往下讲」"),
    ("小明", False, "我跟你说个事，你别激动哈", "对方要讲事 → 不能主持人口吻"),
]

CFG = wx_bot.load_config(wx_bot.CONFIG_PATH)["ai"]


def main() -> int:
    n = 1 if "--n" not in sys.argv else int(sys.argv[sys.argv.index("--n") + 1])
    bad = 0
    total = 0
    for sender, is_group, text, why in CASES:
        print(f"\n[{sender}] {text}    （{why}）")
        for i in range(n):
            reply = wx_bot.ai_reply(CFG, text, sender, sender, is_group, None, me_name="你的昵称")
            total += 1
            hits = [b for b in BANNED if b in (reply or "")]
            parts = wx_bot.split_for_send(reply, CFG.get("multi_send") or {})
            tag = "沉默" if wx_bot.is_silence(reply) else (" ｜ ".join(parts) or "【空回复】")
            print(f"  #{i + 1} {tag}")
            if hits:
                bad += 1
                print(f"      ✗ 命中禁句：{hits}")
    print()
    print("=" * 46)
    print(f"结果：{total - bad}/{total} 条未出现禁句")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
