# -*- coding: utf-8 -*-
"""会话别名表 chat_alias.json：裸 ID → 显示名。

为什么需要：面板（`bot_gui`）按 `chat` 字段精确分组，同一个对话如果一次拿到昵称、
一次拿到裸 ID（联系人库瞬时查不到时 `wx_push.build_payload` 会回退），列表里就会
裂成两条（§7.29）。

这个模块做两件事：
1. 读写别名表（`wx_push` 运行时自己也会写，这里主要用于启动时补漏）；
2. ``repair(db)``：扫 `bot_events.jsonl` 里出现过的裸 ID，用联系人库把显示名补进别名表。
   **在机器人启动时调用**——别名表是运行时文件，被清掉/写坏后能自己长回来。
"""
from __future__ import annotations

import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALIAS_PATH = os.path.join(BASE_DIR, "chat_alias.json")
EVENTS_PATH = os.path.join(BASE_DIR, "bot_events.jsonl")

# 裸 ID 的形态：wxid_xxx（个人）或 xxxxx@chatroom（群）
RAW_ID = re.compile(r"^(wxid_[0-9A-Za-z_\-]+|\d+@chatroom)$")


def is_raw(value: str) -> bool:
    return bool(value) and bool(RAW_ID.match(str(value).strip()))


def load() -> dict:
    try:
        with open(ALIAS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except Exception:
        pass
    return {}


def save(mapping: dict) -> None:
    """原子写；先把磁盘内容读回来合并，别抹掉别的进程刚写的条目。"""
    merged = load()
    merged.update({str(k): str(v) for k, v in mapping.items() if k and v})
    tmp = ALIAS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ALIAS_PATH)


def raw_ids(events_path: str = EVENTS_PATH) -> set:
    """从事件流里收集所有裸 ID（chat / sender 两个字段）。"""
    found = set()
    try:
        with open(events_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                for key in ("chat", "sender"):
                    v = e.get(key)
                    if is_raw(v):
                        found.add(str(v).strip())
    except FileNotFoundError:
        pass
    return found


def repair(db, events_path: str = EVENTS_PATH, quiet: bool = False) -> dict:
    """把事件流里没别名的裸 ID 用联系人库解析出来并写回。返回补上的 {id: name}。"""
    aliases = load()
    missing = [r for r in sorted(raw_ids(events_path)) if not aliases.get(r)]
    if not missing:
        return {}
    fixed = {}
    for rid in missing:
        try:
            name = db.get_nickname(rid)
        except Exception:
            name = ""
        name = (name or "").strip()
        if name and name != rid:
            aliases[rid] = name
            fixed[rid] = name
    if fixed:
        save(aliases)
    if fixed and not quiet:
        for rid, name in fixed.items():
            print(f"  [会话别名] {rid[:12]}… → {name}", flush=True)
    return fixed


if __name__ == "__main__":
    # 手动补漏：python chat_alias.py（机器人跑着时可能抢不到数据库，失败不影响）
    try:
        from wx_push import WeChatDB  # noqa: WPS433
        fixed = repair(WeChatDB())
        print(f"补上 {len(fixed)} 条" if fixed else "没有需要补的裸 ID")
    except Exception as e:  # noqa: BLE001
        print(f"失败：{type(e).__name__}: {e}")
