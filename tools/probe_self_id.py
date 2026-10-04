# -*- coding: utf-8 -*-
"""探针2：找出本机「自己」在消息表里的 real_sender_id。"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from wechatauto.db import WeChatDB

db = WeChatDB()
me = db.get_self_info()["username"]
print("本机 wxid:", me)

idx = db._sender_id_index()
print("SenderName2Id 映射条数:", len(idx))
self_ids = [k for k, v in idx.items() if v == me]
print("映射表中等于本机 wxid 的 rowid:", self_ids)

# 文件传输助手：里面所有消息必然都是自己发的 → 用它反推自己的 rowid
msgs = db.get_messages("filehelper", limit=20)
ids = sorted({m["sender_id"] for m in msgs if m["sender_id"]})
print(f"文件传输助手 {len(msgs)} 条消息中出现的 sender_id: {ids}")
