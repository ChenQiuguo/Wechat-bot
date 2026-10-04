# -*- coding: utf-8 -*-
"""账号自检：确认机器人到底在操作哪个号，以及「自己」的判定有没有错位。

为什么需要它：多账号时上游库按「数据库最后被改动」挑账号，不是按「当前登录」，
而 `sender_id` 又是**按账号编号**的。换号后如果残留旧状态，机器人会把新号的
「自己发的消息」当成别人发的，于是**回复自己**。

用法：
  python check_account.py            # 只看账号与自身判定
  python check_account.py --chats    # 再看每个会话的「自己」判定是否自相矛盾
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

from wechatauto.db import WeChatDB, list_accounts


def main():
    ap = argparse.ArgumentParser(description="微信账号自检")
    ap.add_argument("--chats", action="store_true", help="逐会话检查自身判定")
    args = ap.parse_args()

    print("=== 本机登录过的微信账号（按数据库最近改动排序）===")
    acts = list_accounts() or []
    if not acts:
        print("  （没扫到账号目录）")
    for i, a in enumerate(acts):
        mark = " ← 上游库会挑这个（最近有改动）" if i == 0 else ""
        import datetime
        t = datetime.datetime.fromtimestamp(a.get("last_activity") or 0)
        print(f"  {a.get('wxid')}  最后活动 {t:%Y-%m-%d %H:%M}{mark}")

    db = WeChatDB()
    info = db.get_self_info() or {}
    me = info.get("username") or ""
    print(f"\n=== 本次连接到的账号 ===")
    print(f"  昵称：{info.get('nick_name')}     wxid：{me}")

    # 「自己」的判定：filehelper 必然全是自己发的
    msgs = []
    try:
        msgs = db.get_messages("filehelper", limit=200) or []
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 读 filehelper 失败：{type(e).__name__}: {e}")
    ids = Counter(m.get("sender_id") for m in msgs if m.get("sender_id") is not None)
    print(f"\n=== 「自己」的判定依据（filehelper，{len(msgs)} 条）===")
    if not ids:
        print("  ⚠ filehelper 里没有消息，无法据此判定自己 → 有误判风险")
    else:
        top, n = ids.most_common(1)[0]
        print(f"  推导出的 self_id = {top}（占 {n}/{sum(ids.values())} 条）")
        if len(ids) > 1:
            print(f"  ⚠ 出现了多个 sender_id：{dict(ids)}——可能混了别人的消息，判定不可靠")

    if not args.chats:
        print("\n（加 --chats 可逐会话检查「自己」判定是否自相矛盾）")
        return 0

    print("\n=== 逐会话检查：sender_id → wxid 的映射是否唯一 ===")
    chats = db.list_message_chats() or []
    chats.sort(key=lambda c: -(c.get("message_count") or 0))
    import re
    fu = re.compile(r'fromusername\s*=\s*"([^"]+)"')
    for c in chats[:12]:
        u = c.get("username") or ""
        rows = db.get_messages(u, limit=60) or []
        m2w = {}
        conflict = False
        for r in rows:
            m = fu.search(str(r.get("content") or ""))
            if not m:
                continue
            sid = r.get("sender_id")
            w = m.group(1)
            if sid in m2w and m2w[sid] != w:
                conflict = True
            m2w.setdefault(sid, w)
        tag = "⚠ 同一 id 映射到多个 wxid（跨账号数据混在一起）" if conflict else "ok"
        print(f"  {(c.get('name') or '')[:20]:<22} 消息 {c.get('message_count') or 0:>5}  "
              f"{'群' if '@chatroom' in u else '私聊'}  {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
