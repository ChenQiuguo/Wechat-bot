# -*- coding: utf-8 -*-
"""机器人状态总线：bot 写文件，GUI 读文件，跨进程通信。

bot 侧用法：
    from bot_state import BUS
    BUS.set_status(online=True, account="你的昵称")
    BUS.event("recv", chat="群A", sender="张三", text="hello")

GUI 侧用法：
    from bot_state import BotStateReader
    reader = BotStateReader()
    status = reader.status()
    events = reader.drain()
"""
from __future__ import annotations

import json
import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(BASE_DIR, "bot_status.json")
EVENTS_PATH = os.path.join(BASE_DIR, "bot_events.jsonl")

_MAX_EVENTS_FILE = 2000      # events 文件最多保留行数


class BotStateBus:
    """bot 侧：写 status.json + 追加 events.jsonl。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._status = {
            "online": False,
            "running": False,
            "account": "",
            "account_wxid": "",
            "self_id": None,
            "rhythm": "",
            "dry_run": False,
            "last_msg_time": 0.0,
            "reply_count_today": 0,
            "started_at": 0.0,
            "pid": os.getpid(),
            "updated_at": time.time(),
        }

    def set_status(self, **kwargs):
        with self._lock:
            self._status.update(kwargs)
            self._status["updated_at"] = time.time()
            self._flush_status()

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def event(self, kind: str, **fields):
        ev = {"kind": kind, "ts": time.time(), **fields}
        line = json.dumps(ev, ensure_ascii=False) + "\n"
        try:
            # 追加写，带文件锁（Windows 上用 msvcrt）
            import msvcrt
            fd = os.open(EVENTS_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                os.lseek(fd, 0, os.SEEK_END)
                os.write(fd, line.encode("utf-8"))
            finally:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                os.close(fd)
        except Exception:
            # 降级：不加锁直接追加
            try:
                with open(EVENTS_PATH, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass
        # 截断：超过上限时重写最后 N 行
        self._trim_events()

    def _flush_status(self):
        tmp = STATUS_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._status, f, ensure_ascii=False)
            os.replace(tmp, STATUS_PATH)
        except Exception:
            pass

    def _trim_events(self):
        try:
            if not os.path.exists(EVENTS_PATH):
                return
            size = os.path.getsize(EVENTS_PATH)
            if size < 512 * 1024:       # 512KB 以下不处理
                return
            with open(EVENTS_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > _MAX_EVENTS_FILE:
                keep = lines[-_MAX_EVENTS_FILE:]
                with open(EVENTS_PATH, "w", encoding="utf-8") as f:
                    f.writelines(keep)
        except Exception:
            pass


class BotStateReader:
    """GUI 侧：读 status.json + 增量读 events.jsonl。"""

    def __init__(self):
        self._events_offset = 0
        self._status_cache: dict = {}
        self._status_mtime = 0.0

    def status(self) -> dict:
        try:
            mtime = os.path.getmtime(STATUS_PATH)
            if mtime != self._status_mtime:
                with open(STATUS_PATH, encoding="utf-8") as f:
                    self._status_cache = json.load(f)
                self._status_mtime = mtime
        except Exception:
            pass
        return dict(self._status_cache)

    def drain(self) -> list:
        events = []
        try:
            size = os.path.getsize(EVENTS_PATH)
            if size < self._events_offset:
                self._events_offset = 0        # 文件被截断了
            if size > self._events_offset:
                with open(EVENTS_PATH, "r", encoding="utf-8") as f:
                    f.seek(self._events_offset)
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                events.append(json.loads(line))
                            except Exception:
                                pass
                    self._events_offset = f.tell()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return events


# 全局单例（bot 侧用）
BUS = BotStateBus()
