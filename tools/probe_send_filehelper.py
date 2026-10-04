# -*- coding: utf-8 -*-
"""测试发送只走「文件传输助手」——绝不打扰任何真人会话。

文件传输助手(self→self)在 `skip_chats` 里，机器人不会回复它，
所以拿它当沙盒是安全的。

用法：
  python probe_send_filehelper.py                 # 只测输入，不发
  python probe_send_filehelper.py --send          # 真发一条到文件传输助手
"""
from __future__ import annotations

import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

import kb_input
import wx_bot
from wechatauto.db import WeChatDB
from wechatauto.guia import WeChatGUI

DO_SEND = "--send" in sys.argv
cfg = wx_bot.load_config(wx_bot.CONFIG_PATH)
ic = wx_bot.input_cfg(cfg)
print(f"input 配置：{ic}")
print(f"生效模式：{wx_bot.install_input_patch(ic)}\n")

import pyperclip
before = pyperclip.paste()
print(f"剪贴板（操作前）= {before!r}\n")

db = WeChatDB()
gui = WeChatGUI()
wx_bot.wire_gui(gui)

# 文件传输助手：username 固定是 filehelper
CHAT_U, CHAT_N = "filehelper", "文件传输助手"
print("=== 1. 会话列表里有没有它 ===")
names = wx_bot.resolve_display_names(db, CHAT_U, CHAT_N)
print(f"  用于匹配会话列表的候选名：{names}")
if names:
    ok, why = wx_bot.open_chat_via_session_list(gui, names[0])
    print(f"  会话列表点击：{ok}（{why}）")
print(f"  当前会话：{gui._get_uia().current_chat() if gui._get_uia() else '?'!r}")

print("\n=== 2. 输入 + 校验（不发）===")
uia = gui._get_uia()
ctrl = uia._chat_input() if uia and uia.ensure_window() else None
if ctrl is None:
    print("  ❌ 找不到输入框控件")
    sys.exit(1)
TEXT = "自测：新的输入方式（不走剪贴板）"
if DO_SEND:
    TEXT += " + 真发送"
ok_t = kb_input.type_into(ctrl, TEXT)
print(f"  type_into={ok_t}  读到={kb_input.get_value(ctrl)!r}")
if not DO_SEND:
    kb_input.clear(ctrl)
    print(f"  已清空：{kb_input.get_value(ctrl)!r}")

print("\n=== 3. 发送（走机器人同一条路径）===")
if DO_SEND:
    # 和机器人一样：先试会话列表点击，失败回退搜索框
    via = ""
    opened = False
    for n in names:
        opened, why = wx_bot.open_chat_via_session_list(gui, n)
        if opened:
            via = "列表点击"
            break
    t0 = time.time()
    resp = gui.send_msg(TEXT, None if opened else CHAT_N, False)
    good = bool(getattr(resp, "is_success", None)) or \
        (isinstance(resp, dict) and resp.get("status") == "成功")
    print(f"  {'✅' if good else '❌'} 耗时 {time.time() - t0:.2f}s | "
          f"路径={via or '搜索回退'} | resp={resp}")
else:
    print("  （未加 --send，跳过）")

time.sleep(1.0)
after = pyperclip.paste()
print(f"\n剪贴板（操作后）= {after!r}")
print(f"剪贴板未被改动 => {'✅ 是' if after == before else '❌ 否'}")
print(f"Unicode 注入尝试次数 = {kb_input.type_attempts}")
