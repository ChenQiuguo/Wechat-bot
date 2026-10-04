# -*- coding: utf-8 -*-
"""验证发送者解析修复：群消息必须用「正文前缀 wxid + 本群成员名单」来认人。

用法：python verify_group_sender.py <群名关键字>
其它成员名字一律打码，只显示是否命中了本群名单。
"""
from __future__ import annotations

import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

from wechatauto.db import WeChatDB
from wx_push import SenderResolver

KEY = sys.argv[1] if len(sys.argv) > 1 else ""


def mask(s: str) -> str:
    s = s or ""
    return (s[0] + "*" * (len(s) - 1)) if len(s) > 1 else "*"


db = WeChatDB()
rs = SenderResolver(db)

target = None
for s in db.get_sessions(limit=60):
    if KEY and KEY in (db.get_nickname(s["username"]) or "") and "@chatroom" in s["username"]:
        target = s["username"]
        break
if not target:
    sys.exit(f"没找到含「{KEY}」的群")

roster = rs.roster(target)
print(f"群 {target[:14]}...  本群成员 {len(roster)} 人: {[mask(v) for v in roster.values()]}")
print()

msgs = db.get_messages(target, limit=40)
print(f"最近 {len(msgs)} 条消息的解析结果：")
in_roster = out_roster = 0
for m in msgs:
    is_self, wxid, name = rs.resolve(m, target)
    content = str(m.get("content") or "").replace("\n", " ")[:22]
    if is_self:
        verdict = "自己"
    elif wxid in roster:
        verdict = "✅ 命中本群名单"
        in_roster += 1
    else:
        verdict = "⚠ 不在本群名单"
        out_roster += 1
    pref = rs._prefix_wxid(m.get("content"))
    print(f"  sender_id={m.get('sender_id'):<3} 前缀={'有' if pref else '无'} "
          f"→ {mask(wxid):<14} {mask(name):<10} {verdict}  {content}")
print()
print(f"命中本群名单: {in_roster} 条   不在名单: {out_roster} 条")
