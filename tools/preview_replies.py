# -*- coding: utf-8 -*-
"""预览：带上下文时几条典型消息的 AI 回复质量（不发送）。

用法：python preview_replies.py [--no-history]
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

import wx_bot

USE_HISTORY = "--no-history" not in sys.argv
cfg = wx_bot.load_config(wx_bot.CONFIG_PATH)["ai"]

CASES = [
    # (发送者, 会话名, 是否群, 对方刚发的, 上下文)
    ("小明", "小明", False, "Ciallo～(∠・ω< )⌒★",
     ["我: 在吗", "小明: 在的", "我: 晚上打不打游戏", "小明: Ciallo～(∠・ω< )⌒★"]),
    ("小明", "测试群", True, "@你的昵称 你要@你的昵称",
     ["阿伟: 群里那个你的昵称是谁啊", "小明: 就是我朋友", "阿伟: 你让他出来说话",
      "小明: @你的昵称 你要@你的昵称"]),
    ("阿伟", "测试群", True, "@你的昵称 你sm了是吧",
     ["我: 别刷屏了", "阿伟: 你管得着吗", "@你的昵称 你sm了是吧"]),
    ("小红", "小红", False, "借我两千块钱周转一下，下周还你",
     ["我: 最近怎么样", "小红: 不太好，手头有点紧"]),
    ("小明", "测试群", True, "@你的昵称 刚那个BOSS打过了吗",
     ["小明: 我打完深渊12层了", "阿伟: 我卡在11-3", "我: 我也卡着呢",
      "小明: @你的昵称 刚那个BOSS打过了吗"]),
    ("阿伟", "测试群", True, "@你的昵称 把@小明 踢了",
     ["小明: 你菜就多练", "阿伟: 你才菜", "阿伟: @你的昵称 把@小明 踢了"]),
    ("阿伟", "测试群", True, "@你的昵称 評價這個@小明",
     ["小明: 我今天单抽出金了", "阿伟: 狗托", "阿伟: @你的昵称 評價這個@小明"]),
]

for sender, chat_name, is_group, text, history in CASES:
    h = history if USE_HISTORY else None
    reply = wx_bot.ai_reply(cfg, text, sender, chat_name, is_group, h, me_name="你的昵称")
    print(f"[{'群' if is_group else '私'}] {sender}: {text}")
    if USE_HISTORY and history:
        print(f"     （上下文 {len(history)} 条）")
    print(f"  → {reply}")
    print()
