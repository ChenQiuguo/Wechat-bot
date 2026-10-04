# -*- coding: utf-8 -*-
"""实测：打开会话后，键盘焦点是不是已经在输入框上（即能否省掉「点一下输入框」）。

对比三组，每组都**只往输入框打字、不按回车、打完清空**：
  A. 只 open_chat，直接注入（最少动作）
  B. open_chat + 点击会话列表里的那个会话行，再注入
  C. open_chat + 点击输入框（现在线上用的做法），再注入

另外测「连续切换会话」时 A 是否稳定（切完群马上打），因为这正是
机器人连发多条时的真实场景。

用法：python probe_focus_after_open.py
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
from wechatauto.uia_driver import SESSION_LIST_AIDS, _aid_hit, _find_by

cfg = wx_bot.load_config(wx_bot.CONFIG_PATH)
db = WeChatDB()
gui = WeChatGUI()

IC = wx_bot.input_cfg(cfg)
print(f"input 配置：{IC}\n")


def targets(limit=2):
    out = []
    try:
        for c in (db.list_message_chats() or []):
            u = c.get("username") or ""
            if u and "@chatroom" in u:
                n = db.get_nickname(u) or u
                if n not in out:
                    out.append(n)
            if len(out) >= limit:
                break
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 选会话失败：{e}")
    return out


def uia_ctrl():
    uia = gui._get_uia()
    if uia is None or not uia.ensure_window():
        return None
    return uia._chat_input()


def click_session_row(name):
    """点会话列表里那一行（UIA 路径，和机器人打开会话用的是同一套）。"""
    uia = gui._get_uia()
    if uia is None:
        return False
    node = _find_by(uia._win, lambda c: _aid_hit(getattr(c, "AutomationId", ""), SESSION_LIST_AIDS))
    if node is None:
        return False
    for item in node.GetChildren():
        first = (item.Name or "").split("\n")[0].strip()
        if first == name:
            r = item.BoundingRectangle
            uia._click_at((r.left + r.right) // 2, (r.top + r.bottom) // 2)
            time.sleep(0.25)
            return True
    return False


def trial(label, name, do_click_row=False, do_click_input=False):
    """返回 (是否注入成功, 输入框读到的内容, 耗时秒)。"""
    if not gui.open_chat(name):
        print(f"  {label}: ❌ 会话打不开")
        return False, None, 0.0
    t0 = time.time()
    if do_click_row:
        click_session_row(name)
    if do_click_input:
        box = gui.get_input_box()
        if box:
            x0, y0, x1, y1 = box
            gui.wx_click(gui.origin_x + (x0 + x1) // 2,
                         gui.origin_y + y0 + min(24, (y1 - y0) // 4))
            time.sleep(0.4)
    ctrl = uia_ctrl()
    if ctrl is None:
        print(f"  {label}: ❌ 找不到输入框控件")
        return False, None, time.time() - t0
    txt = f"焦点测试{label[:12]}"
    ok = kb_input.type_into(ctrl, txt, float(IC.get("char_delay", 0.012)))
    got = kb_input.get_value(ctrl)
    kb_input.clear(ctrl)
    dt = time.time() - t0
    print(f"  {label}: {'✅ 落进输入框' if ok else '❌ 没落进去'}  读到={got!r}  耗时={dt:.2f}s")
    return ok, got, dt


def main():
    names = targets(2)
    if not names:
        print("[错误] 没找到可测的群")
        return 1
    print(f"测试目标：{names}\n")

    print("=== 单次打开后直接注入（对比三种做法）===")
    for name in names[:1]:
        print(f"[{name}]")
        trial("A 仅open", name)
        trial("B 点会话行", name, do_click_row=True)
        trial("C 点输入框", name, do_click_input=True)

    print("\n=== 连续切换会话后立即注入（机器人连发场景，做法 A）===")
    if len(names) >= 2:
        a, b = names[0], names[1]
        ok_all = True
        for i in range(3):
            for nm in (a, b):
                ok, got, dt = trial(f"A 切换{i + 1}", nm)
                ok_all = ok_all and ok
        print(f"\n  连续切换 6 次全部落进输入框 => {'✅ 是' if ok_all else '❌ 否'}")
    else:
        print("  （只有一个群，跳过）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
