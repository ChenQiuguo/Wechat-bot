# -*- coding: utf-8 -*-
"""端到端验证：走打了补丁的发送路径发一条真消息，同时确认剪贴板没被动过。

⚠️ 会真的往群里发一条消息（内容写明是测试），所以默认要显式加 --send。
   不加 --send 时只做「输入进输入框 + 校验 + 清空」，不发。

用法：
  python probe_no_clipboard_send.py                 # 只测输入，不发
  python probe_no_clipboard_send.py --send          # 真发一条
  python probe_no_clipboard_send.py --send --chat 测试群
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

import wx_bot
import kb_input

DO_SEND = "--send" in sys.argv
CHAT = ""
for i, a in enumerate(sys.argv):
    if a == "--chat" and i + 1 < len(sys.argv):
        CHAT = sys.argv[i + 1]

cfg = wx_bot.load_config(wx_bot.CONFIG_PATH)
ic = wx_bot.input_cfg(cfg)
print(f"input 配置：{ic}")
print(f"生效模式：{wx_bot.install_input_patch(ic)}")

import pyperclip
before = pyperclip.paste()
print(f"\n剪贴板（发送前）= {before!r}")

from wechatauto.db import WeChatDB
from wechatauto.guia import WeChatGUI

db = WeChatDB()
gui = WeChatGUI()
wx_bot.wire_gui(gui)      # 登记给输入补丁（连发复用输入框要用）
if not CHAT:
    try:
        for c in (db.list_message_chats() or []):
            u = c.get("username") or ""
            if u and "@chatroom" in u:
                CHAT = db.get_nickname(u) or u
                break
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 选会话失败：{e}")
if not CHAT:
    print("[错误] 没找到目标群，用 --chat 群名 指定")
    sys.exit(1)
print(f"目标群：{CHAT}")

TEXT = "测试一下新的输入方式，这条不用剪贴板发的"
if DO_SEND:
    # 按机器人的真实顺序：先用会话列表点开，再连发（和一句一条一样）
    names = wx_bot.resolve_display_names(db, "", CHAT)
    opened, why = False, ""
    for n in names:
        opened, why = wx_bot.open_chat_via_session_list(gui, n)
        if opened:
            break
    print(f"用会话列表打开：{opened}（{why}）")
    t_all = time.time()
    for i, t in enumerate((TEXT, TEXT + "（第二条）"), 1):
        t0 = time.time()
        resp = gui.send_msg(t, None, False)
        ok = bool(getattr(resp, "is_success", None)) or \
            (isinstance(resp, dict) and resp.get("status") == "成功")
        print(f"  第{i}条 {'✅' if ok else '❌'} {time.time() - t0:.2f}s | {resp}")
    print(f"  两条合计 {time.time() - t_all:.2f}s")
else:
    print(f"只测输入（不发）：{TEXT!r}")
    if not gui.open_chat(CHAT):
        print("[错误] 打不开会话")
        sys.exit(1)
    time.sleep(1.0)
    uia = gui._get_uia()
    ctrl = uia._chat_input() if uia else None
    if ctrl is None:
        print("[错误] 没找到输入框控件")
        sys.exit(1)
    ok = kb_input.type_into(ctrl, TEXT, float(ic.get("char_delay", 0.012)))
    print(f"  type_into 返回：{ok}")
    print(f"  输入框现在的内容：{kb_input.get_value(ctrl)!r}")
    print(f"  清空：{kb_input.clear(ctrl)}")
    print(f"  清空后内容：{kb_input.get_value(ctrl)!r}")

time.sleep(1.5)
after = pyperclip.paste()
print(f"\n剪贴板（操作后）= {after!r}")
print(f"剪贴板全程未被改动 => {'✅ 是' if after == before else '❌ 否（被动过了）'}")
print(f"Unicode 注入尝试次数 = {kb_input.type_attempts}")
