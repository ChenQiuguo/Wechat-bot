# -*- coding: utf-8 -*-
"""验证：用新脚本的解析逻辑处理真实历史消息，检查发送者识别与内容清洗。"""
from __future__ import annotations

import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

from wechatauto.db import WeChatDB
from wx_push import SenderResolver, build_payload

db = WeChatDB()
resolver = SenderResolver(db)
print(f"自身 sender_id = {resolver.self_id} | 本机 wxid = {resolver.me}")
print(f"映射表 = {resolver.index}")
print()

for s in db.get_sessions(limit=5):
    user = s["username"]
    msgs = db.get_messages(user, limit=8)
    if not msgs:
        continue
    print(f"===== {db.get_nickname(user) or user} =====")
    for m in msgs:
        payload = build_payload(db, resolver, m)
        print(
            f"  自己={payload['is_self']!s:<5} 发送者={payload['sender']:<22} "
            f"[{payload['type']}] {payload['content'][:45]}"
        )
    print()
