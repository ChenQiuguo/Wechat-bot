# -*- coding: utf-8 -*-
"""探针：验证「本机收到的图片」能不能读到并解密（只读，不发送、不改微信）。

用法：
  python probe_image_read.py                 # 看最近有图的会话，各试几张
  python probe_image_read.py --chat 某个群    # 只看指定会话（昵称/wxid/群名）
  python probe_image_read.py --limit 5       # 每个会话试几张

⚠️ 只读数据库 + 读本地 .dat 缓存；不驱动界面、不发消息。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

from wechatauto.db import WeChatDB                      # noqa: E402
from wechatauto.media import MediaDownloader            # noqa: E402

# 解出来的图写到「当前工作目录」下，避免在源码目录里堆一堆图
OUT_DIR = os.path.join(os.getcwd(), "media", "probe")


def magic(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    if data[:4] == b"wxgf":
        return "wxgf(微信动画表情容器，需转码)"
    if data[:4] == b"WXAM":
        return "WXAM(HEVC)"
    return "未知(%s...)" % data[:8].hex()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default="", help="只看这个会话")
    ap.add_argument("--limit", type=int, default=3, help="每个会话试几张（默认 3）")
    args = ap.parse_args()

    t0 = time.time()
    db = WeChatDB()
    md = MediaDownloader(db)
    print(f"账号：{(db.get_self_info() or {}).get('nick_name')} ({db.wxid})")
    print(f"cfg_dword={'有' if getattr(db, 'cfg_dword', None) else '无'}  "
          f"account_dir 存在={os.path.isdir(db.account_dir)}")

    targets = []
    if args.chat:
        hits = db.search_contact(args.chat) or []
        targets = [h["username"] for h in hits[:3]]
        if not targets:
            print(f"⚠ 找不到会话「{args.chat}」")
            return 1
    else:
        for s in db.get_sessions(limit=80):
            u = s.get("username") or ""
            if not u:
                continue
            try:
                rows = db.get_image_rows(u, limit=1)
            except Exception:
                continue
            if rows:
                targets.append(u)
            if len(targets) >= 6:
                break

    print(f"有图片消息的会话：{len(targets)} 个（耗时 {time.time()-t0:.1f}s）\n")
    ok = fail = 0
    for u in targets:
        try:
            name = db.get_nickname(u) or u
        except Exception:
            name = u
        rows = db.get_image_rows(u, limit=args.limit)
        print(f"—— {name} ({u}) 图片消息 {len(rows)} 条")
        for r in rows:
            lid = r.get("local_id")
            ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("create_time") or 0))
            path = None
            err = ""
            try:
                path = md.download_image(u, lid, save_dir=OUT_DIR, tier="full")
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            if not path:
                print(f"   {ts} #{lid}: ✗ 本机没有可用的 .dat（或密钥不可用）{err}")
                fail += 1
                continue
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                head = f.read(16)
            print(f"   {ts} #{lid}: ✅ {magic(head)} {size//1024}KB → {os.path.basename(path)}")
            ok += 1
        print()
    print(f"结果：解密成功 {ok} 张，失败 {fail} 张；输出目录 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
