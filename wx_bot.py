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
import memory_store
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
             history=None, me_name: str = "", mem_block: str = "") -> str:
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
    parts = [ident]
    if mem_block:
        parts.append(mem_block.strip() + "\n\n")
    parts.append(ctx)
    parts.append(f"{who} 刚发来：{content}")
    user_prompt = "".join(parts)

    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": cfg["system_prompt"]},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": cfg.get("max_tokens", 400),
        "stream": False,
    }
    if cfg.get("thinking", False):
        # 思考模式：先出思维链再出答案；注意该模式下 temperature 会被忽略
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = cfg.get("reasoning_effort", "high")
    else:
        body["temperature"] = cfg.get("temperature", 1.2)
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
        # 长度上限可配置：默认 220 字，够展开讲清楚，又不至于刷屏
        limit = int(cfg.get("max_chars", 220) or 0)
        if limit > 0:
            text = text[:limit].rstrip()
        return text
    except Exception as e:  # noqa: BLE001
        print(f"[AI 失败] {type(e).__name__}: {e}", flush=True)
        return ""


# --------------------------------------------------------------------------
# 「@我 / 提到我 / 话题轮到我」判定
# --------------------------------------------------------------------------

# 消息里这些词说明对方在叫某人、或在问句，才值得让模型判一次「是不是在叫我」
_ADDRESS_WORDS = ("你", "您", "问下", "问一下", "请教", "我说", "我觉得", "帮我",
                  "回答", "说句话", "说话", "出来", "在吗", "在么", "搞啥", "干嘛", "干啥")


def _mentions_name(text: str, names, name_owner: dict) -> bool:
    """正文里是否出现了「我的名字」，且这个名字不是别人正在被叫的名字。

    实测群正文形如 ``wxid_xxx:\\n正文``，@ 前的文本是 '@'，
    所以直接子串匹配即可（不要求前面有 @）。
    """
    t = text or ""
    for n in names:
        n = (n or "").strip()
        if not n or n not in t:
            continue
        # 群里还有别人叫这个名字（也含该串）时不认，避免张冠李戴
        others = [o for o in name_owner.get(n, ()) if o != n]
        if others:
            stripped = t
            for o in sorted(others, key=len, reverse=True):
                stripped = stripped.replace(o, "")
            if n not in stripped:
                continue
        return True
    return False


def _judge_user_prompt(me_names, chat_name: str, sender: str, text: str, recent) -> str:
    names = "、".join(n for n in dict.fromkeys(me_names) if n)
    lines = []
    if recent:
        lines.append("最近的群聊记录（最后一条就是刚收到的这条）：")
        lines.extend(recent)
        lines.append("")
    lines.append(f"刚收到的一条：{sender}: {text}")
    return (
        f"你在判断微信群「{chat_name}」里刚收到的这条消息是不是在说「{names}」（即本人）。\n\n"
        + "\n".join(lines) + "\n\n"
        "规则：\n"
        f"1. 对方 @ 了{names}、直接叫这个名字、或用「你」对着这个名字的主人说话 → 是；\n"
        f"2. 上文在谈论{names}，这条接着问关于这个人的事 → 是。**代词也算**："
        "上文刚提到这个名字，这条里的「他/她/他自己」指的就是这个人；\n"
        f"3. 只是顺口提到这个名字、别人之间对话把这个人当第三方案例或比较对象、"
        f"或者原话就是在说「跟{names}没关系」 → 否；\n"
        "4. 拿不准就答否。\n\n"
        "只输出一个字：是 或 否。"
    )


def judge_targeted(cfg: dict, me_names, chat_name: str, sender: str,
                   text: str, recent, api_key: str = "") -> bool:
    """便宜的一次判定：这条消息该不该由我回应。失败一律返回 False（宁可不回）。"""
    key = api_key or load_api_key()
    if not key:
        return False
    jc = cfg.get("judge") or {}
    body = {
        "model": jc.get("model") or cfg.get("model", "deepseek-flash"),
        "messages": [
            {"role": "system", "content": "你是一个严格的消息归类器，只输出「是」或「否」，不要任何解释。"},
            {"role": "user", "content": _judge_user_prompt(me_names, chat_name, sender, text, recent)},
        ],
        "max_tokens": int(jc.get("max_tokens", 16)),
        # 实测坑：deepseek-flash 默认开思考，思维链会把 max_tokens 吃光、
        # content 返回空串，判定就永远失败。判定这种二选一必须显式关掉思考。
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=jc.get("timeout", 12)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = (data["choices"][0]["message"]["content"] or "").strip()
        if not out:
            print("  [相关判定] 返回为空，按「无关」处理（检查 judge.max_tokens / thinking）", flush=True)
            return False
        return out.startswith("是")
    except Exception as e:  # noqa: BLE001
        print(f"  [相关判定失败] {type(e).__name__}: {e}", flush=True)
        return False


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
        self.name_hits: list = []
        self.api_key = ""
        self.judge_calls = 0
        self._last_judge: dict = {}
        self._judge_cache: dict = {}
        me = {}
        try:
            me = db.get_self_info() or {}
        except Exception as e:  # noqa: BLE001
            print(f"  [警告] 取自身昵称失败：{type(e).__name__}: {e}", flush=True)
        for n in (me.get("nick_name"),):
            if n and n not in self.at_names:
                self.at_names.append(n)
        # 没被 @ 也可能是在叫我：昵称/别名 + 别人平时怎么称呼我
        for n in list(self.trig.get("name_hits") or []) + self.at_names:
            if isinstance(n, str) and n and n not in self.name_hits:
                self.name_hits.append(n)

    def _bump_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._count_today = 0

    def _context_judge(self, payload: dict, recent) -> bool:
        """用一次便宜的模型调用判断这条群消息是不是在说我（结果缓存，避免重复判定）。"""
        if not self.trig.get("context_judge", False):
            return False
        recent = list(recent or [])
        lookback = int(self.trig.get("context_lookback", 8))
        now = time.time()
        cd = float(self.trig.get("judge_cooldown", 8))
        with self._lock:
            last = self._last_judge.get(payload["chat_username"], 0)
            if now - last < cd:
                return False
            key = (payload["chat_username"], payload.get("content", ""))
            cached = self._judge_cache.get(key)
            if cached is not None:
                return cached
        self._last_judge[payload["chat_username"]] = now
        self.judge_calls += 1
        hit = judge_targeted(self.cfg["ai"], self.name_hits, payload["chat_name"],
                             payload["sender"], payload["content"],
                             recent[-lookback:], self.api_key)
        with self._lock:
            if len(self._judge_cache) > 500:
                self._judge_cache.clear()
            self._judge_cache[key] = hit
        return hit

    def check(self, payload: dict, recent=None) -> tuple:
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
            name_owner: dict = {}
            try:
                for nm in self.resolver.roster(payload["chat_username"]).values():
                    nm = (nm or "").strip()
                    if nm:
                        name_owner.setdefault(nm, []).append(nm)
            except Exception:  # noqa: BLE001
                name_owner = {}
            # ① 直接 @ 我：最高优先，一定回（哪怕正文里还 @ 了别人）
            at_me = next((n for n in self.at_names if f"@{n}" in text), None) or \
                next((n for n in self.name_hits if f"@{n}" in text), None)
            if not at_me:
                # ② 只是「可能跟我有关」才值得花一次便宜的判定；
                #    光出现我的名字不算——可能是顺口提到，或原话就跟我无关
                cand = _mentions_name(text, self.name_hits, name_owner) \
                    or _mentions_name(" ".join(recent or []), self.name_hits, name_owner) \
                    or any(w in text for w in _ADDRESS_WORDS) \
                    or text.endswith(("?", "？"))
                if not (cand and self._context_judge(payload, recent)):
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


# --------------------------------------------------------------------------
# 主动开话题
# --------------------------------------------------------------------------

def parse_multi(raw, separator: str = "|||", max_parts: int = 0) -> list:
    """把 AI 输出切成要分开发送的几条。

    默认不开分隔符（separator 为空）→ 整段作为一条发出，多行保留在单条里。
    开了分隔符：按它切，条数有上限，丢弃空白段。
    """
    text = (raw or "").strip()
    if not text:
        return []
    if not separator:
        return [text]
    parts = [p.strip() for p in text.split(separator)]
    parts = [p for p in parts if p]
    if max_parts and len(parts) > max_parts:
        # 超出上限就合并尾巴，宁可一条长点，也别刷屏
        head = parts[: max_parts - 1]
        head.append(separator.join(parts[max_parts - 1:]).replace(separator, " "))
        parts = head
    return parts


def build_proactive_cfg(cfg: dict) -> dict:
    """主动开话题的配置 + 专用提示词覆盖（安全红线原样保留）。"""
    p = dict(cfg.get("proactive") or {})
    if not (p.get("prompt") or "").strip():
        p["prompt"] = (
            cfg["ai"]["system_prompt"]
            + "\n\n【这次不是回复，是主动开个话头】\n"
              "群里已经安静了一会儿，你想主动说句话把话头接起来。要求：\n"
              "1. 接上最近的话题，或说一件你这边的新鲜事/一个具体的问题，别空喊「有人在吗」；\n"
              "2. 一两句就够，别长篇大论，别说教；\n"
              "3. 别重复最近自己说过的话，也别把上面聊过的内容复述一遍；\n"
              "4. 上面所有安全红线照旧生效。"
        )
    return p


def proactive_reply(cfg: dict, db, resolver, chat_username: str, chat_name: str,
                    me_name: str = "", mem_block: str = "", recent_said=None) -> str:
    """生成一条主动消息（不发送）。"""
    ai = dict(cfg["ai"])
    history = build_history(db, resolver, chat_username, int(ai.get("history_limit", 8)))
    p = build_proactive_cfg(cfg)
    ai["system_prompt"] = p["prompt"]
    if recent_said:
        mem_block = (mem_block or "") + "\n\n【你最近主动说过的话，别重复】" + " / ".join(recent_said[-3:])
    who = f"群「{chat_name}」现在没人说话"
    return ai_reply(ai, "（现在没人发消息，你主动起个话头）", who, chat_name, True,
                    history, me_name=me_name, mem_block=mem_block)


def in_quiet_hours(cfg: dict, now=None) -> bool:
    """现在是否处于免打扰时段（跨零点也支持）。"""
    span = (cfg.get("proactive") or {}).get("quiet_hours") or []
    if len(span) != 2:
        return False
    t = time.localtime(now or time.time())
    hm = t.tm_hour * 60 + t.tm_min
    a, b = int(span[0]) * 60, int(span[1]) * 60
    return (a <= hm < b) if a <= b else (hm >= a or hm < b)


def _chat_idle_seconds(db, chat_username: str) -> float:
    """该会话最后一条消息距今多少秒（读不到就返回 0，即不主动）。"""
    try:
        msgs = db.get_messages(chat_username, limit=1) or []
        if not msgs:
            return 0.0
        ts = float(msgs[0].get("create_time") or 0)
        return max(0.0, time.time() - ts) if ts else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


class ProactiveScheduler:
    """空闲很久时主动找个话头。只对显式列进 proactive.chats 的群生效。"""

    def __init__(self, cfg: dict, db, resolver, policy, send_multi, get_gui=None):
        self.cfg = cfg
        self.db = db
        self.resolver = resolver
        self.policy = policy
        self.send_multi = send_multi
        self.p = dict(cfg.get("proactive") or {})
        self.chats = [c for c in (self.p.get("chats") or []) if isinstance(c, str) and c.strip()]
        self._day = time.strftime("%Y-%m-%d")
        self._count_today = 0
        self._last_proactive: dict = {}
        self._recent_said: dict = {}
        self._lock = threading.Lock()
        try:
            self.me_name = (db.get_self_info() or {}).get("nick_name") or ""
        except Exception:  # noqa: BLE001
            self.me_name = ""

    # ---- 闸门 ----
    def _bump_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._count_today = today, 0

    def why_not(self, chat_username: str, now: float = None) -> str:
        """判断这个会话现在能不能主动说话，不能则返回原因。"""
        now = now or time.time()
        if not self.p.get("enabled", False):
            return "主动开话题未开启"
        if not self.chats:
            return "没配置 proactive.chats（不知道去哪个群说话）"
        if in_quiet_hours(self.cfg, now):
            return "免打扰时段"
        with self._lock:
            self._bump_day()
            if self._count_today >= self.p.get("max_per_day", 6):
                return "触发每日主动上限"
            if now - self._last_proactive.get(chat_username, 0) < self.p.get("min_gap_per_chat", 3600):
                return "这个群刚主动过"
        idle = _chat_idle_seconds(self.db, chat_username)
        if idle < self.p.get("min_idle_minutes", 40) * 60:
            return f"群里刚说过话（空闲 {int(idle / 60)} 分钟）"
        return "ok"

    def note_activity(self, chat_username: str):
        """有人说话了 → 这个群重新计时，别再去开话头。"""
        with self._lock:
            self._last_proactive[chat_username] = 0.0

    def _touch(self, chat_username: str):
        """记一次尝试（哪怕没发出去）——否则调度循环会每 60 秒重试同一个群。"""
        with self._lock:
            self._last_proactive[chat_username] = time.time()

    def _mark(self, chat_username: str, said: str):
        with self._lock:
            self._bump_day()
            self._count_today += 1
            self._last_proactive[chat_username] = time.time()
            self._recent_said.setdefault(chat_username, []).append(said)
            self._recent_said[chat_username] = self._recent_said[chat_username][-5:]

    # ---- 主循环 ----
    def run_once(self, chat_username: str = "", force: bool = False, dry_run: bool = False) -> bool:
        """挑一个群说一句话。force=True 时跳过空闲/间隔判断（手动测试用）。"""
        if chat_username:
            if not force:
                why = self.why_not(chat_username)
                if why != "ok":
                    print(f"  · 不主动：{why}", flush=True)
                    return False
            targets = [chat_username]
        else:
            targets = [c for c in self.chats if force or self.why_not(c) == "ok"]
            if targets:
                # 最冷清的先来
                targets.sort(key=lambda c: -_chat_idle_seconds(self.db, c))
        if not targets:
            return False
        for cu in targets:
            try:
                chat_name = self.db.get_nickname(cu) or cu
            except Exception:  # noqa: BLE001
                chat_name = cu
            mem_block = ""
            if self.cfg["ai"].get("memory", {}).get("enabled", True):
                try:
                    mem_block = memory_store.prompt_block(memory_store.load(cu))
                except Exception:  # noqa: BLE001
                    mem_block = ""
            self._touch(cu)          # 先记账，失败也不至于 60 秒后又来一次
            try:
                raw = proactive_reply(self.cfg, self.db, self.resolver, cu, chat_name,
                                      self.me_name, mem_block,
                                      self._recent_said.get(cu))
            except Exception as e:  # noqa: BLE001
                print(f"  [主动生成失败] {type(e).__name__}: {e}", flush=True)
                continue
            texts = parse_multi(raw, (self.p.get("separator") or ""),
                                int(self.p.get("max_messages", 2)))
            if not texts:
                print("  · 主动生成结果为空，跳过", flush=True)
                continue
            idle_min = int(_chat_idle_seconds(self.db, cu) / 60)
            log_line(f"[主动开话题] {chat_name}（空闲 {idle_min} 分钟）: {' ｜ '.join(texts)}")
            if dry_run:
                return True
            ok, _n = self.send_multi(texts, cu, chat_name)
            if ok:
                self._mark(cu, texts[0])
                try:
                    self.policy.mark(cu)
                except Exception:  # noqa: BLE001
                    pass
                return True
        return False

    def loop(self, stop_event: threading.Event, dry_run: bool = False):
        interval = float(self.p.get("check_interval", 60))
        while not stop_event.wait(interval):
            try:
                # 一轮最多主动一次，别连环轰炸
                self.run_once(dry_run=dry_run)
            except Exception as e:  # noqa: BLE001
                print(f"  [主动调度异常] {type(e).__name__}: {e}", flush=True)


def _resp_ok(resp) -> bool:
    ok = bool(getattr(resp, "is_success", None))
    if not ok and isinstance(resp, dict):
        ok = resp.get("status") == "成功"
    return ok


def send_multi_impl(get_gui, send_lock, db, texts, chat_username, chat_name,
                    gap: float = 1.0) -> tuple:
    """把若干条消息依次发给同一会话：只打开一次会话，之后连发。

    返回 (是否全部发出, 已发出的条数)。
    """
    texts = [t for t in (texts or []) if (t or "").strip()]
    if not texts:
        return True, 0
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
            who = names[0] if names else (chat_name or chat_username)
            if opened:
                via = "列表点击"
            else:
                via = f"搜索回退[{why}]"
            sent = 0
            for i, text in enumerate(texts):
                if not (opened or i == 0):
                    break        # 会话没打开成功，只有第一条能靠搜索框补
                resp = gui.send_msg(text, None if opened else who, False)
                ok = _resp_ok(resp)
                sent += 1 if ok else 0
                log_line(f"{'[已发送]' if ok else '[发送异常]'} → {chat_name}: {text} "
                         f"| {via}{'' if len(texts) == 1 else f' 第{i + 1}/{len(texts)}条'} "
                         f"{time.time() - t0:.1f}s | resp={resp}")
                if not ok:
                    return False, sent
                if i + 1 < len(texts):
                    # 连发之间留点间隔，别撞上游库的节奏闸
                    time.sleep(max(0.0, gap))
            return True, sent
        except Exception as e:  # noqa: BLE001
            log_line(f"[发送失败] {chat_name}: {type(e).__name__}: {e}")
            return False, 0


def do_send_impl(args, get_gui, send_lock, db, text, chat_username, chat_name):
    """兼容旧调用：发一条。"""
    return send_multi_impl(get_gui, send_lock, db, [text], chat_username, chat_name)[0]


def main():
    ap = argparse.ArgumentParser(description="微信 AI 自动回复机器人")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--dry-run", action="store_true", help="只生成回复，不发送")
    ap.add_argument("--test-reply", default="", help="测试：把该文本当私信走完整流程（发到文件传输助手）")
    ap.add_argument("--proactive-now", default=None, nargs="?", const="",
                    help="立刻试一条主动消息（可跟群名；默认只生成不发送）")
    ap.add_argument("--proactive-send", action="store_true",
                    help="配合 --proactive-now：真的发出去")
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

    api_key = load_api_key()
    if not api_key:
        print("[错误] 没拿到 DEEPSEEK_API_KEY（环境变量或 ~/.dsh/.credentials.yaml）")
        return 1

    policy = ReplyPolicy(cfg, resolver, db)
    policy.api_key = api_key

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

    def send_multi(texts, chat_username: str, chat_name: str) -> tuple:
        """一次发多条（同一个会话只打开一次）。"""
        texts = [t for t in (texts or []) if (t or "").strip()]
        if args.dry_run:
            for t in texts:
                log_line(f"[DRY-RUN] 本应发送「{chat_name}」: {t}")
            return True, len(texts)
        return send_multi_impl(get_gui, send_lock, db, texts, chat_username, chat_name)

    sched = ProactiveScheduler(cfg, db, resolver, policy, send_multi)

    # --proactive-now [群名]：立刻试一条主动消息（默认只生成不发送）
    if args.proactive_now is not None:
        who = (args.proactive_now or "").strip()
        cu = who
        if who and not _looks_like_wxid(who):
            try:
                cu = db.username_by_nickname(who) or db.group_name_to_id(who) or who
            except Exception:  # noqa: BLE001
                cu = who
        if not cu:
            cu = (sched.chats or [""])[0]
        if not cu:
            print("[错误] 没指定群，且 config 里 proactive.chats 是空的")
            return 1
        chat_name = db.get_nickname(cu) or cu
        print(f"主动开话题试跑 → {chat_name}（空闲 {int(_chat_idle_seconds(db, cu) / 60)} 分钟）")
        sched.run_once(cu, force=True, dry_run=not args.proactive_send)
        return 0

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
    if cfg["trigger"].get("context_judge"):
        print(f"群内额外触发：提到「{'、'.join(policy.name_hits)}」即回；"
              f"话题相关时再用一次轻量判定（回看 {cfg['trigger'].get('context_lookback', 8)} 条）")
    print(f"模式：{'DRY-RUN（不发送）' if args.dry_run else '正式（会真的发送）'}")
    print(f"回复日志 → {REPLY_LOG}\n", flush=True)

    lst = Listener(db, interval=1.0)

    def on_msg(msg: dict, _lst: Listener):
        payload = build_payload(db, resolver, msg)
        # 上下文先取好：既给「是不是在说我」判定用，也给正式生成回复用
        history = build_history(
            db, resolver, payload["chat_username"],
            int(cfg["ai"].get("history_limit", 8)), msg.get("local_id"),
        )
        ok, reason = policy.check(payload, history)
        if not ok:
            # 常见情况静默（否则群里每条消息都刷一行），只报异常原因
            if reason not in QUIET_REASONS and not reason.startswith("非文本"):
                print(f"  · 跳过 {payload['chat_name']} | {reason}", flush=True)
            return
        log_line(f"[{payload['time']}] 收到 {payload['chat_name']} | {payload['sender']}: {payload['content']}")
        # 注入该会话的记忆档案（没有就是空串）
        mem_block = ""
        if cfg["ai"].get("memory", {}).get("enabled", True):
            try:
                mem_block = memory_store.prompt_block(memory_store.load(payload["chat_username"]))
            except Exception as e:  # noqa: BLE001
                print(f"  [读取记忆失败] {type(e).__name__}: {e}", flush=True)
        if mem_block:
            print(f"  · 已注入 {payload['chat_name']} 的记忆档案", flush=True)

        # 群聊：注入「本群成员名单」，避免认错人 / 编造名字
        if payload["is_group"]:
            try:
                roster = resolver.roster(payload["chat_username"])
                names = [n for w, n in roster.items() if n and w != resolver.me]
                if names:
                    mem_block += ("\n\n【本群成员（当前昵称）】" + "、".join(names[:60])
                                  + "\n称呼别人只能用这些名字或聊天记录里出现过的名字，"
                                    "绝不要自己编名字；不确定是谁就直接问。")
                    print(f"  · 已注入本群 {len(names)} 位成员名单", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  [读取群成员失败] {type(e).__name__}: {e}", flush=True)

        reply = ai_reply(cfg["ai"], payload["content"], payload["sender"], payload["chat_name"],
                         payload["is_group"], history, me_name=info.get("nick_name") or "",
                         mem_block=mem_block)
        if not reply:
            log_line("  AI 未生成回复，跳过")
            return
        # 一次可以说好几句：开了分隔符就分条发，否则整段一条（多行保留）
        ms = dict(cfg["ai"].get("multi_send") or {})
        sep = ms.get("separator", "|||") if ms.get("enabled", True) else ""
        texts = parse_multi(reply, sep, int(ms.get("max_messages", 3)))
        policy.mark(payload["chat_username"])
        if sched is not None:
            sched.note_activity(payload["chat_username"])   # 人在说话，别去主动开话题
        if send_multi(texts, payload["chat_username"], payload["chat_name"])[0]:
            # 异步更新记忆档案（不阻塞、不影响回复延迟）
            if cfg["ai"].get("memory", {}).get("enabled", True):
                lines = list(history or [])
                lines.append(f"{payload['sender']}: {payload['content']}")
                lines.append("我: " + " ".join(texts))
                threading.Thread(
                    target=memory_store.update,
                    args=(payload["chat_username"], payload["chat_name"], lines,
                          cfg["ai"], load_api_key(), cfg["ai"].get("memory", {})),
                    daemon=True,
                ).start()

    # 主动开话题：后台线程，只在列进 proactive.chats 的群里说话
    stop_event = threading.Event()
    if sched.chats:
        print(f"主动开话题：{'、'.join(sched.chats)} "
              f"（空闲 ≥{sched.p.get('min_idle_minutes', 40)} 分钟才开口，"
              f"每天最多 {sched.p.get('max_per_day', 6)} 次"
              f"{'，已开启' if sched.p.get('enabled', False) else '，未开启（enabled=false）'}）")
        if sched.p.get("enabled", False):
            threading.Thread(target=sched.loop, args=(stop_event, args.dry_run),
                             daemon=True).start()

    lst.add_all(on_msg, discover=True)
    lst.start()
    print("机器人运行中（Ctrl+C 停止）...", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        lst.stop()
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
