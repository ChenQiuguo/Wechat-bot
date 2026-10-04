# -*- coding: utf-8 -*-
"""微信 AI 自动回复机器人

规则：
  * 私聊来消息 → 用 AI 生成简短回复并发出
  * 群聊里 @我    → 同上
  * 自己发的消息永不回复（防自杀循环）
  * 每个会话有冷却时间，避免连发刷屏与风控

用法：
  python wx_bot.py                     # 正式运行：监听 + 自动回复
  python wx_bot.py --dry-run           # 只生成回复不发送（安全预览）
  python wx_bot.py --test-reply "你好"  # 测试：把这句话当成私信，走完整 AI+发送流程（发到文件传输助手）
  python wx_bot.py --config other.json

密钥：默认从 ~/.dsh/.credentials.yaml 读取 DEEPSEEK_API_KEY，也支持环境变量 DEEPSEEK_API_KEY 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wechatauto.db import WeChatDB, Listener
from wechatauto.uia_driver import SESSION_LIST_AIDS, _aid_hit, _find_by
from wx_push import SenderResolver, build_payload, clean_content, fmt_time, _as_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
REPLY_LOG = os.path.join(BASE_DIR, "bot_replies.log")
LOCK_FILE = os.path.join(BASE_DIR, "bot.lock")
# API Key 的查找位置（按顺序；环境变量 DEEPSEEK_API_KEY 优先级最高）
KEY_FILE = os.path.join(BASE_DIR, "apikey.txt")
KEY_PATHS = [
    KEY_FILE,
    os.path.expanduser("~/.dsh/.credentials.yaml"),   # DeepSeek Harness 的凭据文件（可选）
]

# 这些跳过原因很常见，静默处理，避免刷屏
QUIET_REASONS = {
    "自己发的", "系统会话", "群里没@我", "私聊未开启", "群聊未开启", "系统消息",
}

# 微信的系统提示（不是人说的话），不该回复
SYSTEM_TEXTS = (
    "我通过了你的朋友验证请求", "以上是打招呼的内容", "你已添加了", "对方开启了朋友验证",
    "开启了朋友验证", "撤回了一条消息", "邀请你加入了群聊", "移出了群聊",
    "该消息类型暂不能展示", "你已成功加入", "群主已开启", "你已被移出",
)


# --------------------------------------------------------------------------
# 配置与密钥
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return False


def acquire_single_instance_lock():
    """保证同时只跑一个实例（两个进程会抢解密缓存，第二个必然报错）。

    返回 True 表示拿到锁；False 表示已有实例在跑。
    """
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            old = 0
            try:
                with open(LOCK_FILE, encoding="utf-8") as f:
                    old = int((f.read() or "0").strip() or 0)
            except Exception:
                old = 0
            if old and _pid_alive(old):
                return False
            try:                       # 陈旧锁（上次异常退出留下的）→ 清掉重试
                os.remove(LOCK_FILE)
            except OSError:
                return False
    return False


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def load_api_key() -> str:
    """按顺序找 API Key：环境变量 → 本目录 apikey.txt → 其它凭据文件。

    密钥只从这些位置读取，不会写进本项目任何文件。
    """
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    for path in KEY_PATHS:
        try:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        m = re.search(r"DEEPSEEK_API_KEY:\s*[\"']?([A-Za-z0-9\-_.]+)", text)
        if m:
            return m.group(1)
        if path == KEY_FILE:
            first = (text.strip().splitlines() or [""])[0].strip()
            if first.startswith("sk-"):
                return first
    return ""


# --------------------------------------------------------------------------
# AI 回复
# --------------------------------------------------------------------------

def build_history(db: WeChatDB, resolver: "SenderResolver", chat_username: str,
                  limit: int = 8, exclude_local_id=None) -> list:
    """取该会话最近若干条消息作为上下文（跳过媒体占位，排除当前这条）。"""
    try:
        msgs = db.get_messages(chat_username, limit=limit + 2)
    except Exception:
        return []
    lines = []
    for m in msgs:
        if exclude_local_id is not None and m.get("local_id") == exclude_local_id:
            continue
        try:
            is_self, _wxid, name = resolver.resolve(m)
        except Exception:
            is_self, name = False, "未知"
        text = clean_content(_as_text(m.get("content")), m.get("type", ""))
        if not text or text.startswith("["):
            continue  # 媒体消息只占位，对理解语境没帮助
        lines.append(f"{'我' if is_self else name}: {text}")
    return lines[-limit:]


def ai_reply(cfg: dict, content: str, sender: str, chat_name: str, is_group: bool,
             history=None, me_name: str = "") -> str:
    key = load_api_key()
    if not key:
        return ""

    who = f"{sender}（在群「{chat_name}」里）" if is_group else sender

    ident = ""
    if me_name:
        ident = (f"你的微信昵称是「{me_name}」，你就是本人，用第一人称说话；"
                 f"别人写 @{me_name} 就是在叫你。\n\n")

    ctx = ""
    if history:
        ctx = "最近的聊天记录（最后一条就是刚收到的）：\n" + "\n".join(history) + "\n\n"
    user_prompt = f"{ident}{ctx}{who} 刚发来：{content}"

    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": cfg["system_prompt"]},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": cfg.get("max_tokens", 60),
        "temperature": cfg.get("temperature", 1.2),
        "stream": False,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 20)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        text = text.strip("「」\"'“”").strip()
        # 句末不加句号（模型偶尔仍会加，这里兜底剥掉；问号/感叹号/省略号保留）
        text = text.rstrip("。.").rstrip()
        return text[:60]
    except Exception as e:  # noqa: BLE001
        print(f"[AI 失败] {type(e).__name__}: {e}", flush=True)
        return ""


# --------------------------------------------------------------------------
# 回复策略
# --------------------------------------------------------------------------

class ReplyPolicy:
    def __init__(self, cfg: dict, resolver: SenderResolver, db: WeChatDB):
        self.cfg = cfg
        self.resolver = resolver
        self.db = db
        self.trig = cfg["trigger"]
        self.lim = cfg["limits"]
        self.skip_users = set(cfg.get("skip_chats", []))
        self.skip_names = set(cfg.get("skip_chat_names", []))
        self._last_reply: dict = {}
        self._lock = threading.Lock()
        self._recent: list = []
        self._count_today = 0
        self._day = time.strftime("%Y-%m-%d")
        self.at_names = list(self.trig.get("at_names") or [])
        me = db.get_self_info() or {}
        for n in (me.get("nick_name"),):
            if n and n not in self.at_names:
                self.at_names.append(n)

    def _bump_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._count_today = 0

    def check(self, payload: dict) -> tuple:
        """返回 (是否回复, 原因)。"""
        if payload["is_self"]:
            return False, "自己发的"
        if payload["chat_username"] in self.skip_users:
            return False, "系统会话"
        if payload["chat_name"] in self.skip_names:
            return False, "系统会话"
        # 微信的系统提示（好友验证通过、打招呼内容等）不是人说的话，别回
        content = (payload.get("content") or "").strip()
        if len(content) <= 60 and any(t in content for t in SYSTEM_TEXTS):
            return False, "系统消息"
        if payload["type"] not in self.trig.get("types", ["文本"]):
            return False, f"非文本({payload['type']})"

        age = time.time() - float(payload["timestamp"] or 0)
        if age > self.trig.get("max_age_seconds", 120):
            return False, f"消息过旧({int(age)}s)"

        if payload["is_group"]:
            if not self.trig.get("group_at", True):
                return False, "群聊未开启"
            text = payload["content"]
            hit = next((n for n in self.at_names if f"@{n}" in text), None)
            if not hit:
                return False, "群里没@我"
        elif not self.trig.get("private", True):
            return False, "私聊未开启"

        now = time.time()
        with self._lock:
            self._bump_day()
            last = self._last_reply.get(payload["chat_username"], 0)
            cd = self.lim.get("per_chat_cooldown", 45)
            if now - last < cd:
                return False, f"冷却中({int(cd - (now - last))}s)"
            self._recent = [t for t in self._recent if now - t < 60]
            if len(self._recent) >= self.lim.get("max_replies_per_minute", 6):
                return False, "触发每分钟上限"
            if self._count_today >= self.lim.get("max_replies_per_day", 300):
                return False, "触发每日上限"
        return True, "ok"

    def mark(self, chat_username: str):
        now = time.time()
        with self._lock:
            self._bump_day()
            self._last_reply[chat_username] = now
            self._recent.append(now)
            self._count_today += 1


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def log_line(line: str):
    print(line, flush=True)
    with open(REPLY_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _looks_like_wxid(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("wxid_") or s.startswith("gh_") or s.endswith("@chatroom")


def resolve_display_names(db, chat_username: str, chat_name: str) -> list:
    """收集能拿去和「会话列表」匹配的显示名候选。

    会话列表里显示的是昵称/备注，不是 wxid。如果昵称解析失败（拿到的是
    wxid），拿它去匹配永远匹配不上——所以这里多做一次数据库反查。
    """
    cands = []

    def add(n):
        n = (n or "").strip()
        if n and not _looks_like_wxid(n) and n not in cands:
            cands.append(n)

    add(chat_name)
    if cands or db is None:
        return cands

    for probe in (chat_username, chat_name):
        probe = (probe or "").strip()
        if not probe:
            continue
        try:
            add(db.get_nickname(probe) or "")
        except Exception:
            pass
        try:
            for h in (db.search_contact(probe) or [])[:3]:
                add(h.get("nick_name") or h.get("remark") or "")
        except Exception:
            pass
    return cands


def open_chat_via_session_list(gui, name: str, attempts: int = 2) -> tuple:
    """用 UIA 读左侧会话列表并直接点开目标会话。

    返回 (是否成功, 原因)。原因会写进日志，方便诊断为什么会回退到搜索。
    """
    target = (name or "").strip()
    if not target:
        return False, "目标名为空"
    if _looks_like_wxid(target):
        return False, "目标名是 wxid（列表里是昵称，匹配不上）"

    last = "未知原因"
    for i in range(max(1, attempts)):
        try:
            uia = gui._get_uia()  # noqa: SLF001
            if uia is None:
                last = "UIA 不可用"
            elif not uia.ensure_window():
                # 微信重启后热激活标记会归零；ensure_window 内部会重新激活，
                # 但首次可能赶不上，所以循环里再给它一次机会
                last = "UIA 树未就绪（热激活中）"
            else:
                node = _find_by(uia._win,  # noqa: SLF001
                                lambda c: _aid_hit(getattr(c, "AutomationId", ""), SESSION_LIST_AIDS))
                if node is None:
                    last = "找不到会话列表控件"
                else:
                    exact, fuzzy = [], []
                    for item in node.GetChildren():
                        first = (item.Name or "").split("\n")[0].strip()
                        if first == target:
                            exact.append(item)
                        elif first and (first.startswith(target) or target.startswith(first)):
                            fuzzy.append(item)
                    chosen = exact or fuzzy
                    if not chosen:
                        last = f"列表里没有「{target}」"
                    elif len(chosen) > 1:
                        last = f"「{target}」命中 {len(chosen)} 个会话，不猜"
                    else:
                        rect = chosen[0].BoundingRectangle
                        uia._click_at((rect.left + rect.right) // 2,  # noqa: SLF001
                                      (rect.top + rect.bottom) // 2)
                        time.sleep(0.5)
                        cur = uia.current_chat() or ""
                        if cur and (cur.startswith(target) or target in cur):
                            return True, "OK"
                        last = f"点开后当前会话是「{cur[:16]}」，不是目标"
        except Exception as e:  # noqa: BLE001
            last = f"异常 {type(e).__name__}: {e}"
        if i + 1 < attempts:
            time.sleep(1.0)
    return False, last


def do_send_impl(args, get_gui, send_lock, db, text, chat_username, chat_name):
    """实际发送：优先「会话列表点击」，失败回退「搜索框」（并把原因写进日志）。"""
    with send_lock:
        t0 = time.time()
        try:
            gui = get_gui()
            names = resolve_display_names(db, chat_username, chat_name)
            opened, why = False, "没有可用的显示名"
            for n in names:
                opened, why = open_chat_via_session_list(gui, n)
                if opened:
                    break
            if opened:
                resp = gui.send_msg(text, None, False)   # 已确认打开 → 跳过打开步骤
                via = "列表点击"
            else:
                who = names[0] if names else (chat_name or chat_username)
                resp = gui.send_msg(text, who, False)     # 回退：搜索框
                via = f"搜索回退[{why}]"
            ok = bool(getattr(resp, "is_success", None))
            if not ok and isinstance(resp, dict):
                ok = resp.get("status") == "成功"
            log_line(f"{'[已发送]' if ok else '[发送异常]'} → {chat_name}: {text} "
                     f"| {via} {time.time() - t0:.1f}s | resp={resp}")
            return ok
        except Exception as e:  # noqa: BLE001
            log_line(f"[发送失败] {chat_name}: {type(e).__name__}: {e}")
            return False


def main():
    ap = argparse.ArgumentParser(description="微信 AI 自动回复机器人")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--dry-run", action="store_true", help="只生成回复，不发送")
    ap.add_argument("--test-reply", default="", help="测试：把该文本当私信走完整流程（发到文件传输助手）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    os.environ["WECHATAUTO_RHYTHM"] = str(cfg.get("rhythm", "fast"))

    if not acquire_single_instance_lock():
        print("[已在运行] 机器人已经启动过了，不要再开一个——两个进程会抢微信的")
        print("           解密缓存，第二个必然报 PermissionError。")
        print(f"           要重启：先关掉原来那个窗口；若确认已经没在跑，删掉 {LOCK_FILE} 再试。")
        return 1
    import atexit
    atexit.register(release_lock)

    db = WeChatDB()
    resolver = SenderResolver(db)
    info = db.get_self_info()
    policy = ReplyPolicy(cfg, resolver, db)

    if not load_api_key():
        print("[错误] 没拿到 DEEPSEEK_API_KEY（环境变量或 ~/.dsh/.credentials.yaml）")
        return 1

    # 发送端：只初始化一次，避免每次发送都重新校准布局
    sender = {"gui": None}
    send_lock = threading.Lock()

    def get_gui():
        if sender["gui"] is None:
            from wechatauto.guia import WeChatGUI
            sender["gui"] = WeChatGUI()
        return sender["gui"]

    def do_send(text: str, chat_username: str, chat_name: str) -> bool:
        if args.dry_run:
            log_line(f"[DRY-RUN] 本应回复「{chat_name}」: {text}")
            return True
        return do_send_impl(args, get_gui, send_lock, db, text, chat_username, chat_name)

    # --test-reply：模拟一条私信
    if args.test_reply:
        fake = {
            "time": fmt_time(time.time()), "timestamp": time.time(),
            "chat_username": "filehelper", "chat_name": "文件传输助手",
            "is_group": False, "sender_wxid": "wxid_test", "sender": "测试好友",
            "is_self": False, "type": "文本", "content": args.test_reply,
        }
        print(f"模拟私信：{args.test_reply}")
        reply = ai_reply(cfg["ai"], fake["content"], fake["sender"], fake["chat_name"], False,
                         me_name=info.get("nick_name") or "")
        print(f"AI 生成：{reply}")
        if reply:
            do_send(reply, fake["chat_username"], fake["chat_name"])
        return 0

    print(f"账号：{info.get('nick_name')} ({info.get('username')})")
    print(f"策略：私聊={cfg['trigger']['private']} 群@={cfg['trigger']['group_at']} "
          f"@{policy.at_names} 冷却={cfg['limits']['per_chat_cooldown']}s 节奏={cfg.get('rhythm')}")
    print(f"模式：{'DRY-RUN（不发送）' if args.dry_run else '正式（会真的发送）'}")
    print(f"回复日志 → {REPLY_LOG}\n", flush=True)

    lst = Listener(db, interval=1.0)

    def on_msg(msg: dict, _lst: Listener):
        payload = build_payload(db, resolver, msg)
        ok, reason = policy.check(payload)
        if not ok:
            # 常见情况静默（否则群里每条消息都刷一行），只报异常原因
            if reason not in QUIET_REASONS and not reason.startswith("非文本"):
                print(f"  · 跳过 {payload['chat_name']} | {reason}", flush=True)
            return
        log_line(f"[{payload['time']}] 收到 {payload['chat_name']} | {payload['sender']}: {payload['content']}")
        history = build_history(
            db, resolver, payload["chat_username"],
            int(cfg["ai"].get("history_limit", 8)), msg.get("local_id"),
        )
        reply = ai_reply(cfg["ai"], payload["content"], payload["sender"], payload["chat_name"],
                         payload["is_group"], history, me_name=info.get("nick_name") or "")
        if not reply:
            log_line("  AI 未生成回复，跳过")
            return
        policy.mark(payload["chat_username"])
        do_send(reply, payload["chat_username"], payload["chat_name"])

    lst.add_all(on_msg, discover=True)
    lst.start()
    print("机器人运行中（Ctrl+C 停止）...", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        lst.stop()
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
