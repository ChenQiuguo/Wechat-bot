# -*- coding: utf-8 -*-
"""只读：打印你自己（本账号）发过的话，用来提炼「自己的口吻」。

不带任何姓名常量，默认自动挑一个「自己发言最多」的会话。
用法：
  python probe_my_voice.py                    # 自动挑会话，打印 30 条
  python probe_my_voice.py --chat 某个群 --limit 50
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
    ap.add_argument("--chat", default="", help="不填就自动挑「自己发言最多」的会话")
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()

    db = WeChatDB()
    res = SenderResolver(db)
    cu = a.chat
    if cu and "@chatroom" not in cu and not cu.startswith("wxid_"):
        cu = db.group_name_to_id(cu) or db.username_by_nickname(cu) or cu

    if not cu:
        best, best_n = "", -1
        for s in db.get_sessions(limit=60):
            u = s.get("username") or ""
            if not u or u == "filehelper":
                continue
            try:
                msgs = db.get_messages(u, limit=200)
            except Exception:
                continue
            n = 0
            for m in msgs:
                try:
                    if res.resolve(m, u)[0]:
                        n += 1
                except Exception:
                    pass
            if n > best_n:
                best, best_n = u, n
        cu = best
        print(f"（自动挑选：自己发言最多的会话 = {db.get_nickname(cu) or cu}，{best_n} 条）\n")

    msgs = db.get_messages(cu, limit=200)
    mine = []
    for m in msgs:
        try:
            if not res.resolve(m, cu)[0]:
                continue
        except Exception:
            continue
        t = clean_content(_as_text(m.get("content")), m.get("type", ""))
        if t and not t.startswith("["):
            mine.append((fmt_time(m.get("create_time") or 0), t))
    mine = mine[:a.limit]
    print(f"会话：{db.get_nickname(cu) or cu}   我自己发的消息 {len(mine)} 条（新→旧）\n")
    for ts, t in mine:
        print(f"[{ts}] {t[:180]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
