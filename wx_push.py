# -*- coding: utf-8 -*-
"""微信消息监听 + 推送（基于 wechatauto-replica 的本地数据库轮询）

前置条件：
  1. 本机微信(4.x)已登录在线，且保持在线；
  2. 已安装 wechatauto-replica（pip install wechatauto-replica）；
  3. 同一时间只运行一个使用本库的进程（解密缓存会被独占）。

用法：
  python wx_push.py                                # 监听全部会话：打印 + 写日志
  python wx_push.py --chats 张三,工作群             # 只监听指定会话（昵称/备注/wxid/群名均可）
  python wx_push.py --skip-self                    # 忽略自己发的消息
  python wx_push.py --webhook http://127.0.0.1:9000/wechat   # 同时 POST JSON 到该地址
  python wx_push.py --jsonl wx_messages.jsonl      # 同时追加结构化消息到 JSONL 文件

推送出去的 JSON 结构：
  {
    "time": "2026-10-04 10:32:23",       # 本地时间
    "timestamp": 1791081143,             # Unix 秒
    "chat_username": "wxid_xxx" | "1234@chatroom",
    "chat_name": "对方昵称或群名",
    "is_group": true|false,
    "sender_wxid": "wxid_yyy",           # 群里谁发的（个聊即对方）
    "sender": "发送者备注或昵称",          # 自己发的为 "我"
    "is_self": true|false,
    "type": "文本|图片|语音|文件/链接/卡片|动画表情|...",   # 库给的中文类型标签
    "content": "消息正文"
  }

实现要点（都经过本机实测校正）：
  * 「自己发的」不能按 sender_id==2 判断——本机实测自身 rowid 是 3，而 id=2 是好友。
    这里从「文件传输助手」（必然全是自己发的）反推自身 rowid，并优先用 wxid 比对。
  * 库在 db.py 里写死了 "sender_id != 2 才解析用户名"，本机因此漏解析 id=2 的好友；
    这里绕过该判断，直接从 SenderName2Id 映射表解析。
  * 群消息正文常带 "wxid_xxx:" 前缀、媒体消息正文是 XML，推送前做清洗。

⚠️ 仅监听（只读本地数据库），不注入、不改微信文件。请保持低频使用，降低风控风险。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from wechatauto.db import WeChatDB, Listener

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_messages.log")

# 群消息正文里的发送者前缀，如 "wxid_abc123:\n正文" 或 "1234@chatroom:\n正文"
_SENDER_PREFIX_RE = re.compile(r"^(wxid_[0-9A-Za-z_\-]+|[0-9A-Za-z_\-]+@chatroom):\s*\n?")
_XML_RE = re.compile(r"^\s*<\?xml|^\s*<msg[\s>]")


class SenderResolver:
    """把消息里的 sender_id 解析成「谁发的」，并正确识别自己。"""

    def __init__(self, db: WeChatDB):
        self.db = db
        self.me = (db.get_self_info() or {}).get("username", "") or ""
        # 绕过库里 "sender_id != 2" 的判断，拿到完整的 id→wxid 映射
        try:
            self.index = dict(db._sender_id_index())  # noqa: SLF001
        except Exception:
            self.index = {}
        self.self_id = self._learn_self_id()

    def _learn_self_id(self):
        """文件传输助手是「自己和自己」的会话，里面的消息必然都是自己发的。"""
        try:
            msgs = self.db.get_messages("filehelper", limit=200)
            ids = [m.get("sender_id") for m in msgs if m.get("sender_id")]
            if ids:
                return Counter(ids).most_common(1)[0][0]
        except Exception:
            pass
        return 2  # 库里其它机器上的约定

    def resolve(self, msg: dict) -> tuple:
        """返回 (是否自己发的, 发送者 wxid, 显示名)。"""
        sid = msg.get("sender_id") or 0
        wxid = (msg.get("sender_username") or "").strip()
        if not wxid and sid:
            wxid = self.index.get(int(sid), "")

        if wxid and self.me and wxid == self.me:
            return True, wxid, "我"
        if sid and sid == self.self_id:
            return True, self.me or "", "我"
        if self.me and wxid == "" and sid in (0, self.self_id):
            return True, self.me, "我"

        name = ""
        if wxid:
            try:
                name = self.db.get_nickname(wxid) or ""
            except Exception:
                name = ""
            if name == wxid:  # 库里查不到时会原样返回 wxid，此时退回 wxid 显示
                name = wxid
        if not name:
            name = f"未知用户(id={sid})"
        return False, wxid, name


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _as_text(content):
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return "" if content is None else str(content)


def clean_content(content: str, mtype: str) -> str:
    """清洗正文：去掉群消息的发送者前缀；媒体类消息的 XML 换成简短占位。"""
    text = content.strip()
    if not text:
        return f"[{mtype}]"
    stripped = _SENDER_PREFIX_RE.sub("", text, count=1)
    if stripped:
        text = stripped.strip()
    if _XML_RE.match(text):
        return f"[{mtype}]"
    return text


def build_payload(db: WeChatDB, resolver: SenderResolver, msg: dict) -> dict:
    username = msg.get("username") or ""
    is_group = "@chatroom" in username
    is_self, sender_wxid, sender = resolver.resolve(msg)
    try:
        chat_name = db.get_nickname(username) or username
    except Exception:
        chat_name = username
    if chat_name == username and not username:
        chat_name = "(未知会话)"

    mtype = msg.get("type", "未知")
    return {
        "time": fmt_time(msg.get("create_time", time.time())),
        "timestamp": msg.get("create_time", time.time()),
        "chat_username": username,
        "chat_name": chat_name,
        "is_group": is_group,
        "sender_wxid": sender_wxid,
        "sender": sender,
        "is_self": is_self,
        "type": mtype,
        "content": clean_content(_as_text(msg.get("content")), mtype),
    }


def post_webhook(url: str, payload: dict, timeout: float = 5.0, retries: int = 2):
    """POST JSON 到 webhook，失败重试几次。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err = None
    for attempt in range(1 + retries):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    print(f"[推送失败] webhook {url}: {last_err}", flush=True)
    return None


def make_pusher(args, log_fh, jsonl_fh):
    def push(payload: dict):
        line = (
            f"[{payload['time']}] {payload['chat_name']}"
            f"{'(群)' if payload['is_group'] else ''} | {payload['sender']}"
            f" ({payload['type']}) {payload['content']}"
        )
        print(line, flush=True)
        log_fh.write(line + "\n")
        log_fh.flush()
        if jsonl_fh is not None:
            jsonl_fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            jsonl_fh.flush()
        if args.webhook:
            post_webhook(args.webhook, payload)
    return push


def resolve_chats(db, sessions, raws):
    resolved = []
    sessions_by_user = {s.get("username"): s for s in sessions}
    for raw in raws:
        if raw in sessions_by_user:
            resolved.append(raw)
            continue
        hits = db.search_contact(raw)
        if hits:
            resolved.append(hits[0]["username"])
            print(f"  「{raw}」→ {hits[0]['username']}")
        else:
            print(f"  ⚠ 找不到会话「{raw}」，按原始值尝试（可能监听不到）")
            resolved.append(raw)
    return resolved


def main():
    ap = argparse.ArgumentParser(description="微信消息监听+推送")
    ap.add_argument("--chats", default="", help="只监听指定会话，逗号分隔（昵称/备注/wxid/群名）")
    ap.add_argument("--skip-self", action="store_true", help="忽略自己发的消息")
    ap.add_argument("--webhook", default="", help="同时 POST JSON 到该 URL")
    ap.add_argument("--jsonl", default="", help="同时把结构化消息追加到该 JSONL 文件")
    ap.add_argument("--log", default=LOG_FILE, help="日志文件路径（默认脚本目录下 wechat_messages.log）")
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒数（默认 1.0）")
    args = ap.parse_args()

    db = WeChatDB()
    info = db.get_self_info()
    resolver = SenderResolver(db)
    print(f"账号：{info.get('nick_name')} ({info.get('username')})")
    print(f"自身 sender_id = {resolver.self_id}；已解析他人映射 {len(resolver.index)} 条")

    sessions = db.get_sessions(limit=1000)
    print(f"发现 {len(sessions)} 个会话")

    lst = Listener(db, interval=args.interval)
    only_chats = [c.strip() for c in args.chats.split(",") if c.strip()]

    log_fh = open(args.log, "a", encoding="utf-8")  # noqa: SIM115
    jsonl_fh = open(args.jsonl, "a", encoding="utf-8") if args.jsonl else None  # noqa: SIM115
    push = make_pusher(args, log_fh, jsonl_fh)
    skip_self = args.skip_self

    def on_msg(msg: dict, _lst: Listener):
        payload = build_payload(db, resolver, msg)
        if skip_self and payload["is_self"]:
            return
        push(payload)

    if only_chats:
        for name in resolve_chats(db, sessions, only_chats):
            lst.add_listener(name, on_msg)
            print(f"  监听：{name}")
    else:
        lst.add_all(on_msg, discover=True)
        print("  全局监听：已注册现有会话，运行中自动发现新会话")

    print(f"开始监听（Ctrl+C 停止），日志 → {args.log}", flush=True)
    lst.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        lst.stop()
        log_fh.close()
        if jsonl_fh is not None:
            jsonl_fh.close()
        print("\n已停止监听。")


if __name__ == "__main__":
    main()
