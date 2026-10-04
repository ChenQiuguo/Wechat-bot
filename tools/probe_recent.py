# -*- coding: utf-8 -*-
"""只读：打印某个会话最近 N 条消息（给「机器人为什么会说那句话」查证据用）。

用法：
  python probe_recent.py --chat 某个群 --limit 30
  python probe_recent.py --chat 某个群 --grep 测试
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

from wechatauto.db import WeChatDB                      # noqa: E402
from wx_push import SenderResolver, fmt_time, clean_content, _as_text   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required=True, help="昵称/备注/wxid/群名")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--grep", default="", help="只看含该关键词的消息")
    a = ap.parse_args()

    db = WeChatDB()
    res = SenderResolver(db)
    cu = a.chat
    if "@chatroom" not in cu and not cu.startswith("wxid_"):
        cu = db.group_name_to_id(cu) or db.username_by_nickname(cu) or cu
    name = db.get_nickname(cu) or cu
    msgs = db.get_messages(cu, limit=max(a.limit, 50))
    print(f"会话：{name} ({cu})  取到 {len(msgs)} 条（新→旧）\n")
    shown = 0
    for m in msgs:
        is_self, _w, who = res.resolve(m, cu)
        t = clean_content(_as_text(m.get("content")), m.get("type", ""))
        if a.grep and a.grep not in t:
            continue
        tag = "我" if is_self else who
        print(f"[{fmt_time(m.get('create_time') or 0)}] #{m.get('local_id')} "
              f"{m.get('type')} | {tag}: {t[:200]}")
        shown += 1
        if shown >= a.limit:
            break
    print(f"\n显示 {shown} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
