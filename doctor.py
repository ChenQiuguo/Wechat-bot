# -*- coding: utf-8 -*-
"""自检：确认密钥提取 / 数据库解密 / 会话列表都能读到。

用法：python doctor.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


def main():
    from wechatauto.db import WeChatDB

    print("1) 连接数据库（首次需从微信进程内存提取密钥，可能要几秒）...")
    db = WeChatDB()

    info = db.get_self_info()
    print("   账号信息:", info.get("nick_name"), "/", info.get("username"))

    sessions = db.get_sessions(limit=15)
    print(f"2) 会话列表: 共 {len(sessions)} 个（显示最近 15 个）")
    for s in sessions:
        name = db.get_nickname(s["username"]) or s["username"]
        summary = (s.get("summary") or "").replace("\n", " ")[:30]
        print(f"   {name:<20} 未读={s.get('unread')}  {summary}")

    if sessions:
        who = sessions[0]["username"]
        msgs = db.get_messages(who, limit=3)
        print(f"3) 读取「{db.get_nickname(who) or who}」最近 {len(msgs)} 条消息:")
        for m in msgs:
            content = m.get("content")
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            print(f"   [{m.get('type')}] {str(content)[:60]}")

    print("\n✅ 自检通过：密钥提取、数据库解密、消息读取全部正常")


if __name__ == "__main__":
    main()
