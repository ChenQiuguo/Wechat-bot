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
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wechatauto.db import WeChatDB, Listener
from wechatauto.uia_driver import SESSION_LIST_AIDS, _aid_hit, _find_by
import kb_input
import memory_store
import image_in
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
    "群里图片不回", "图片理解未开启", "图片取不到(本机无缓存)",
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


def _pid_alive(pid: int, started: float = 0.0) -> bool:
    """这个 pid 是不是**我们那个**进程。

    只看 pid 会被**复用**骗到：机器人挂掉后 Windows 可能把同一个 pid 分给微信
    或别的程序，残留锁就看起来「还在运行」，于是机器人再也起不来。
    所以锁里额外记进程启动时间，对不上就当陈旧锁。
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return False
    if not psutil.pid_exists(pid):
        return False
    if not started:
        return True
    try:
        return abs(psutil.Process(pid).create_time() - float(started)) < 2.0
    except Exception:  # noqa: BLE001
        return False


def _self_start_time() -> float:
    try:
        import psutil
        return psutil.Process(os.getpid()).create_time()
    except Exception:  # noqa: BLE001
        return 0.0


def acquire_single_instance_lock():
    """保证同时只跑一个实例（两个进程会抢解密缓存，第二个必然报错）。

    返回 True 表示拿到锁；False 表示已有实例在跑。
    锁内容为 ``"<pid> <进程启动时间>"``。
    """
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"{os.getpid()} {_self_start_time()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            old_pid, old_start = 0, 0.0
            try:
                with open(LOCK_FILE, encoding="utf-8") as f:
                    parts = (f.read() or "").split()
                old_pid = int(parts[0]) if parts else 0
                old_start = float(parts[1]) if len(parts) > 1 else 0.0
            except Exception:  # noqa: BLE001
                old_pid, old_start = 0, 0.0
            if old_pid and _pid_alive(old_pid, old_start):
                return False
            try:                       # 陈旧锁（上次异常退出 / pid 被复用）→ 清掉重试
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
    """取该会话最近若干条消息作为上下文。

    纯文本才喂给模型；媒体消息只留一个占位说明（图片的具体内容只喂当前这条，
    不然历史里每张图都要解密 + 上千 token）。
    """
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
        mtype = m.get("type", "")
        text = clean_content(_as_text(m.get("content")), mtype)
        who = "我" if is_self else name
        if not text or text.startswith("["):
            if mtype:
                lines.append(f"{who}: [发了一张图片]" if mtype == "图片"
                             else f"{who}: [{mtype}]")
            continue
        lines.append(f"{who}: {text}")
    return lines[-limit:]


def _chat_call(cfg: dict, system_prompt: str, user_prompt: str, *, thinking: bool,
               max_tokens: int, timeout: float, temperature: float = 1.2,
               reasoning_effort: str = "high", api_key: str = "",
               images=None) -> str:
    """调一次 chat/completions，返回正文（失败返回空串）。

    ``images`` 非空时走视觉输入：user 消息的 content 变成内容块数组，
    图片按 base64 data URL 内联（官方限制：单图 ≤32MiB、请求体 ≤48MiB，
    **图片只能放在 user 消息里**，system 里带图会 400）。
    """
    key = api_key or load_api_key()
    if not key:
        return ""
    user_content = user_prompt
    if images:
        blocks = [{"type": "text", "text": user_prompt}]
        for data, mime in images:
            blocks.append(image_in.image_block(data, mime,
                                               cfg.get("image", {}).get("detail", "low")))
        user_content = blocks
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "stream": False,
    }
    if thinking:
        # 思考模式：先出思维链再出答案；注意该模式下 temperature 会被忽略
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = reasoning_effort
    else:
        # 必须显式 disabled：deepseek-flash 不传 thinking 时默认自己开思考，
        # 思维链会把 max_tokens 吃光、content 返回空串（二选一的判定会静默全错）。
        body["thinking"] = {"type": "disabled"}
        body["temperature"] = temperature
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[AI 失败] {type(e).__name__}: {e}", flush=True)
        return ""
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    out = (msg.get("content") or "").strip()
    if not out:
        reasoning = str(msg.get("reasoning_content") or "")
        if reasoning:
            print(f"[AI 空回复] 思维链吃掉了 max_tokens（{reasoning[:40]!r}…），"
                  f"需要显式 thinking=disabled 或调大 max_tokens", flush=True)
    return out


def _with_key(ai_cfg: dict, api_key: str) -> dict:
    """把 api_key 塞进 cfg，供 ai_reply 透传给 _chat_call（避免每步重读凭据文件）。"""
    c = dict(ai_cfg)
    if api_key:
        c["_api_key"] = api_key
    return c


def ai_reply(cfg: dict, content: str, sender: str, chat_name: str, is_group: bool,
             history=None, me_name: str = "", mem_block: str = "",
             api_key: str = "", images=None) -> str:
    api_key = api_key or cfg.get("_api_key", "")

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
    if images:
        # 图片就在这条消息里（内容块紧跟在下面），说清楚模型才知道「刚发来的是图」
        parts.append(f"{who} 刚发来 {'一张图片' if len(images) == 1 else f'{len(images)}张图片'}"
                     f"（图片内容在下面，文字说明可能为空）：\n")
    else:
        parts.append(f"{who} 刚发来：{content}")
    user_prompt = "".join(parts)

    text = _chat_call(cfg, cfg["system_prompt"], user_prompt,
                      thinking=bool(cfg.get("thinking", False)),
                      max_tokens=cfg.get("max_tokens", 400),
                      timeout=cfg.get("timeout", 20),
                      temperature=cfg.get("temperature", 1.2),
                      reasoning_effort=cfg.get("reasoning_effort", "high"),
                      api_key=api_key, images=images)
    if not text:
        return ""
    text = text.strip("「」\"'“”").strip()
    # 句末不加句号（模型偶尔仍会加，这里兜底剥掉；问号/感叹号/省略号保留）
    text = text.rstrip("。.").rstrip()
    # 长度上限可配置：默认 220 字，够展开讲清楚，又不至于刷屏
    limit = int(cfg.get("max_chars", 220) or 0)
    if limit > 0:
        text = text[:limit].rstrip()
    return text


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
    jc = cfg.get("judge") or {}
    out = _chat_call(cfg,
                     "你是一个严格的消息归类器，只输出「是」或「否」，不要任何解释。",
                     _judge_user_prompt(me_names, chat_name, sender, text, recent),
                     # 实测坑：deepseek-flash 默认开思考，思维链会把 max_tokens 吃光、
                     # content 返回空串，判定就永远失败。二选一必须显式关掉思考。
                     thinking=False, max_tokens=int(jc.get("max_tokens", 16)),
                     timeout=jc.get("timeout", 12), temperature=0)
    if not out:
        return False
    return out.startswith("是")


# --------------------------------------------------------------------------
# 没人点我，但群里有人问了个我能答的问题 → 主动接一句
# --------------------------------------------------------------------------

# 像「在问问题」的说法，命中才值得花一次判定
_QUESTION_WORDS = (
    "怎么", "怎样", "咋", "如何", "为什么", "为何", "哪个", "哪一个", "哪些", "是什么", "什么是",
    "多少", "多久", "几天", "几点", "哪里", "哪儿", "能不能", "可不可以", "可以吗", "行不行",
    "有没有", "是不是", "对不对", "值不值", "要不要", "会不会", "该不该", "求", "请问",
    "谁知道", "有人知道", "谁知道", "求助", "问一下", "问下", "帮我看", "帮我看看", "教教我",
)


def looks_like_question(text: str) -> bool:
    """粗筛：这条像不像在问问题（真正的判断交给模型，这里只挡掉闲聊）。"""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith(("?", "？")):
        return True
    return any(w in t for w in _QUESTION_WORDS)


def _can_answer_prompt(me_names, chat_name: str, sender: str, text: str, recent) -> str:
    names = "、".join(n for n in dict.fromkeys(me_names) if n)
    lines = []
    if recent:
        lines.append("群里最近的记录（最后一条就是刚收到的这条）：")
        lines.extend(recent)
        lines.append("")
    lines.append(f"刚收到：{sender}: {text}")
    return (
        f"你是「{names}」，在一个微信群里。上面这条消息**没有点你的名**，你在犹豫要不要主动接一句。\n\n"
        + "\n".join(lines) + "\n\n"
        "回答「是」只在这些情况下：\n"
        "1. 对方在问一个**具体问题**（怎么办、为什么、哪个好、报错怎么修、有没有人知道…），"
        "而你**确实有能落地的答案**，接一句有用；\n"
        "2. 对方在求助/求证，群里还没人回答，你答得上；\n"
        "3. 群成员名单/聊天记录里已经有答案线索，你能一句话讲清。\n\n"
        "回答「否」的情况：\n"
        "- 只是闲聊、吐槽、发表情、喊人、跟别人说话；\n"
        "- 问题太宽泛或信息不够，答了只能瞎猜；\n"
        "- 你答不上来，或只能说「不知道」「看情况」这类废话；\n"
        "- **群里已经有人回答了这个问题**，而你没有更准确的补充；\n"
        "- 涉及政治、宗教、时事、他人隐私等红线话题。\n\n"
        "拿不准就答否——**插话插错了比不说话更糟**。只输出一个字：是 或 否。"
    )


def can_answer_question(cfg: dict, me_names, chat_name: str, sender: str,
                        text: str, recent, api_key: str = "") -> bool:
    """判定：这群里没人点我，但我该不该主动接下这个问题。失败返回 False。"""
    jc = cfg.get("judge") or {}
    out = _chat_call(cfg,
                     "你是一个严格的判定器，只输出「是」或「否」，不要任何解释。",
                     _can_answer_prompt(me_names, chat_name, sender, text, recent),
                     thinking=False, max_tokens=int(jc.get("max_tokens", 16)),
                     timeout=jc.get("timeout", 12), temperature=0)
    if not out:
        return False
    return out.startswith("是")


def unsolicited_fill(ai_cfg: dict, me_name: str, chat_name: str, sender: str,
                     text: str, history, mem_block: str = "", api_key: str = "") -> str:
    """没人点我，我主动接这个问题的回答（不发送）。"""
    cfg = dict(ai_cfg)
    cfg["system_prompt"] = (
        ai_cfg["system_prompt"]
        + "\n\n【这次没人点你的名，是你自己主动接话】\n"
          "群里有人问了个你能答的问题，你主动接一句。要求：\n"
          "1. 直给答案或办法，别客套、别说「我来回答一下」这种话，开口就是内容；\n"
          "2. 别 @ 任何人，也别在开头写自己或别人的名字，直接说话；\n"
          "3. 答不上来就别答（宁可不说）；\n"
          "4. 上面所有安全红线照旧生效。"
    )
    ident = ""
    if me_name:
        ident = f"你的微信昵称是「{me_name}」，你就是本人，用第一人称说话。\n\n"
    ctx = ""
    if history:
        ctx = "群里最近的记录（最后一条就是刚收到的）：\n" + "\n".join(history) + "\n\n"
    parts = [ident]
    if mem_block:
        parts.append(mem_block.strip() + "\n\n")
    parts.append(ctx)
    parts.append(f"{sender} 在群里问：{text}")
    return ai_reply(cfg, "".join(parts), sender, chat_name, True,
                    history, me_name=me_name, mem_block=mem_block, api_key=api_key)


# --------------------------------------------------------------------------
# 「没人点我但值得接一句」的闸门
# --------------------------------------------------------------------------

class UnsolicitedPolicy:
    """控制「主动接问题」的频率：只回问题、有冷却、有每日上限。

    与 ReplyPolicy 独立，避免把「被 @ 必回」和「主动插话」两套节奏搅在一起。
    """

    def __init__(self, cfg: dict, db):
        self.cfg = cfg
        self.db = db
        self.u = dict(cfg.get("unsolicited") or {})
        self._last: dict = {}
        self._recent: list = []
        self._count_today = 0
        self._day = time.strftime("%Y-%m-%d")
        self._lock = threading.Lock()
        self.asked = 0          # 真正跑了模型判定的次数（看成本用）
        self.hits = 0
        try:
            self.me_name = (db.get_self_info() or {}).get("nick_name") or ""
        except Exception:  # noqa: BLE001
            self.me_name = ""

    def _bump_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._count_today = today, 0

    def check(self, payload: dict, recent, api_key: str = "") -> tuple:
        """返回 (是否主动接一句, 原因)。"""
        if not self.u.get("enabled", False):
            return False, "主动接话未开启"
        if not payload.get("is_group"):
            return False, "私聊不主动"        # 用户明确要求：私聊永不主动
        if payload.get("is_self"):
            return False, "自己发的"
        text = (payload.get("content") or "").strip()
        if not text:
            return False, "空消息"
        ts = float(payload.get("timestamp") or 0)
        if ts and time.time() - ts > float(self.u.get("max_age_seconds", 180)):
            return False, "消息过旧"
        if not looks_like_question(text):
            return False, "不是在问问题"

        now = time.time()
        with self._lock:
            self._bump_day()
            if self._count_today >= int(self.u.get("max_per_day", 10)):
                return False, "触发每日主动接话上限"
            per_chat = float(self.u.get("per_chat_cooldown", 480))
            last = self._last.get(payload["chat_username"], 0)
            if now - last < per_chat:
                return False, f"这个群刚接过话({int(per_chat - (now - last))}s)"
            self._recent = [t for t in self._recent if now - t < 60]
            if len(self._recent) >= int(self.u.get("max_per_minute", 2)):
                return False, "触发每分钟主动接话上限"

        self.asked += 1
        if not can_answer_question(self.cfg["ai"], self._names(), payload["chat_name"],
                                   payload["sender"], text, recent or [], api_key):
            return False, "判定为不该插话"
        self.hits += 1
        return True, "ok"

    def _names(self) -> list:
        return [self.me_name] if self.me_name else []

    def mark(self, chat_username: str):
        now = time.time()
        with self._lock:
            self._bump_day()
            self._last[chat_username] = now
            self._recent.append(now)
            self._count_today += 1

    def describe(self) -> str:
        return (f"主动接问题：{'开' if self.u.get('enabled') else '关'} "
                f"（群里有人问了能答的问题就接；同群间隔 ≥{int(self.u.get('per_chat_cooldown', 480)) // 60} 分钟，"
                f"每天 ≤{self.u.get('max_per_day', 10)} 次，私聊不主动）")


# --------------------------------------------------------------------------
# 不用剪贴板发送（用户开了剪贴板同步，粘贴会把回复同步到其它设备）
# --------------------------------------------------------------------------

def input_cfg(cfg: dict) -> dict:
    ic = dict(cfg.get("input") or {})
    ic.setdefault("method", "unicode")          # unicode | auto | clipboard
    ic.setdefault("char_delay", 0.012)
    ic.setdefault("fallback_clipboard", True)   # 注入失败时才允许碰剪贴板（auto 模式）
    ic.setdefault("click_input", False)         # 打开会话后焦点已在输入框，默认不点
    return ic


_GUI_REF = {"gui": None}      # 当前进程里那个 WeChatGUI 实例（连发复用输入框要用）


def wire_gui(gui) -> None:
    """把 WeChatGUI 实例登记进来，补丁才能复用它的 _last_input_box。"""
    _GUI_REF["gui"] = gui


def install_input_patch(ic: dict) -> str:
    """给上游库的发送路径打补丁：优先 Unicode 注入，绕开剪贴板。

    覆盖两条路径：
      * ``WeChatUIA.send_text`` → 内部 ``_paste_into``（UIA 快路径，实际在跑的那条）
      * ``WeChatGUI.input_text`` → OCR 回退路径的剪贴板粘贴
    method='clipboard' 时完全不打补丁。返回人话描述的生效模式。
    """
    mode = str(ic.get("method", "unicode")).lower()
    if mode == "clipboard":
        kb_input.set_unicode_typing(False)
        return "剪贴板粘贴（未启用 Unicode 注入）"
    kb_input.set_unicode_typing(True)
    allow_cb = bool(ic.get("fallback_clipboard", True)) and mode != "unicode"
    delay = float(ic.get("char_delay", 0.012) or 0.012)
    click_first = bool(ic.get("click_input", False))

    from wechatauto.guia import WeChatGUI
    from wechatauto.uia_driver import WeChatUIA

    def _patch_paste_into():
        orig = WeChatUIA._paste_into
        if getattr(orig, "_dsh_unicode", False):
            return                                    # 已经打过，别套娃

        def _paste_into(self, ctrl, text, clear=True):
            stats_before = kb_input.type_attempts

            def _remember_box():
                # 让连发的第 2、3 条能走 guia 的 fast 路径（跳过重复的焦点/会话检查）。
                # 值必须是**当前会话名**：guia 拿它和 who 比对，对不上就不会用这条快路径。
                try:
                    inst = _GUI_REF.get("gui")
                    owner = self.current_chat()
                    if inst is not None and owner:
                        inst._last_input_box = owner
                except Exception:  # noqa: BLE001
                    pass

            # 实测：打开会话后键盘焦点已经在输入框上，直接注入即可（1.2s）；
            # 点输入框要走 OCR 探测输入框位置，实测慢到 17s，所以默认不点。
            try:
                if kb_input.type_into(ctrl, text, delay):
                    _remember_box()
                    return
            except Exception as e:  # noqa: BLE001
                print(f"  [输入] Unicode 注入异常：{type(e).__name__}: {e}", flush=True)
            # 焦点没落在输入框（用户手点过别处等）：UIA 重开一次会话即可把焦点交回输入框。
            # 注意不能调 WeChatGUI.get_input_box()——那是截屏/OCR 探测，实测 17s。
            if not click_first and kb_input.retry_after_refocus(
                    ctrl, text, delay, lambda: self.open_chat(self.current_chat() or "")):
                print("  [输入] 焦点兜底：重开会话后注入成功", flush=True)
                _remember_box()
                return
            if not allow_cb and kb_input.type_attempts != stats_before:
                # 明确要求只用键盘注入：失败就让它失败，别偷偷用剪贴板
                raise RuntimeError("Unicode 注入失败，且已禁止回退剪贴板")
            return orig(self, ctrl, text, clear)

        _paste_into._dsh_unicode = True
        WeChatUIA._paste_into = _paste_into

    def _patch_input_text():
        orig = WeChatGUI.input_text
        if getattr(orig, "_dsh_unicode", False):
            return

        def input_text(self, text, box=None, fast=False):
            tried = False
            uia = self._get_uia()
            ctrl = uia._chat_input() if uia is not None else None
            # 会话刚打开，焦点就在输入框：直接注入。
            # 刻意不调 self.get_input_box()（截屏/OCR，实测 17s）；
            # 只有真的需要「点一下输入框」时才付这个代价。
            if ctrl is not None:
                stats_before = kb_input.type_attempts
                if kb_input.type_into(ctrl, text, delay):
                    self._last_input_box = box or getattr(self, "_last_input_box", None)
                    return True
                tried = tried or (kb_input.type_attempts != stats_before)
                if kb_input.retry_after_refocus(ctrl, text, delay,
                                                lambda: self.focus_input(box or self.get_input_box())):
                    self._last_input_box = box or getattr(self, "_last_input_box", None)
                    return True
            if not allow_cb:
                return False
            b = box or self.get_input_box()
            if not b or not self.focus_input(b):
                return False
            self.set_clipboard(text)
            self._input.key(_VK_A_CTRL[0], ctrl=True)
            self._input.key(0x2E)              # Delete
            self._input.key(_VK_V_CTRL[0], ctrl=True)
            time.sleep(0.8)
            if self._input_box_has_text(b):
                self._last_input_box = b
                return True
            if tried:
                return False
            return orig(self, text, box, fast)   # 让上游自己的重试/拼音兜底接手

        input_text._dsh_unicode = True
        WeChatGUI.input_text = input_text

    _patch_paste_into()
    _patch_input_text()
    tag = f"Unicode 注入（不碰剪贴板{'' if allow_cb else '，禁用剪贴板回退'}）· 逐字 {delay * 1000:.0f}ms"
    return tag + ("· 先点输入框" if click_first else "· 不点输入框（实测焦点已在）")


_VK_A_CTRL = (0x41,)
_VK_V_CTRL = (0x56,)


def _account_identity(db) -> tuple:
    """返回 (wxid, 昵称, self_id)。self_id 由 filehelper 反推，多出来的 id 说明判定可疑。"""
    try:
        info = db.get_self_info() or {}
    except Exception:  # noqa: BLE001
        info = {}
    wxid = info.get("username") or ""
    nick = info.get("nick_name") or ""
    try:
        msgs = db.get_messages("filehelper", limit=200) or []
        ids = [m.get("sender_id") for m in msgs if m.get("sender_id") is not None]
        self_id = Counter(ids).most_common(1)[0][0] if ids else None
        other_ids = len(set(ids)) - 1 if ids else 0
    except Exception as e:  # noqa: BLE001
        # 吞异常会让「自身判定」假装正常，必须报出来
        print(f"  [警告] 推导自身 sender_id 失败：{type(e).__name__}: {e}", flush=True)
        self_id, other_ids = None, 0
    return wxid, nick, self_id, other_ids


def account_guard(db, lock_wxid: str) -> str:
    """每次心跳检查：还在操作启动时那个号吗？

    多账号时上游库按「数据库最近改动」挑账号，换号后可能读到另一个号的库，
    而 sender_id 是按账号编号的——**会把新号的自己发的消息当成别人发的，于是回复自己**。
    返回 "ok" / "switched" / "unknown"。
    """
    try:
        cur = (db.get_self_info() or {}).get("username") or ""
    except Exception:  # noqa: BLE001
        return "unknown"
    if not cur:
        return "unknown"
    return "ok" if cur == lock_wxid else "switched"


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
        self.icfg = image_in.image_cfg(cfg["ai"])
        # image_cfg 返回的是拷贝，把补全后的默认值写回，方便别处（on_msg）读同一份
        cfg["ai"]["image"] = self.icfg
        # 当前消息若是图片，这里挂着 (bytes, mime)；check() 用它决定该不该回
        self.pending_image = None
        # 每个会话最近一条「别人发来的消息」的时间（图片触发规则要用）
        self._last_incoming: dict = {}
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

    def check_image(self, payload: dict) -> tuple:
        """图片消息该不该回（与文本分开的规则）。

        * `image.enabled` 是总开关（默认关），关着就当非文本处理；
        * 图片里没有 @ 也认不出「叫名字」，所以群里是否接图由 `trigger.group_image`
          决定（默认不接，避免群里每张图都烧一次视觉 token）；
        * 私聊沿用 `trigger.private`，但可以额外要求「我在最近 N 分钟内说过话」
          （`image.private_require_recent`），免得半年没聊的人甩张图就被回。
        """
        ic = self.icfg
        if not ic.get("enabled"):
            return False, "图片理解未开启"
        if not self.pending_image:
            return False, "图片取不到(本机无缓存)"
        if payload["is_group"]:
            if not self.trig.get("group_image", False):
                return False, "群里图片不回"
        elif not self.trig.get("private", True):
            return False, "私聊未开启"
        if ic.get("private_require_recent") and not payload["is_group"]:
            # 「近期聊过」= 这个会话最近有**人**发过消息（不看我有没有回过）
            win = float(ic.get("recent_window_seconds", 1800))
            last = self._last_incoming.get(payload["chat_username"], 0)
            if not last or time.time() - last > win:
                return False, "并非近期聊过(图片不主动搭话)"
        if payload["is_group"]:
            # 群里图片按「这个群最近还在聊」限流：只接正在进行的对话，不给死群配图
            win = float(ic.get("group_recent_seconds", 0) or 0)
            if win > 0:
                last = self._last_incoming.get(payload["chat_username"], 0)
                if not last or time.time() - last > win:
                    return False, f"这个群 {int(win)} 秒内没人说话(群里图片不主动搭话)"
        return True, "ok"

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

        if payload["type"] == "图片":
            # 图片没有正文，@ 不进来，所以触发规则单独一套
            ok, why = self.check_image(payload)
            if not ok:
                return False, why

        age = time.time() - float(payload["timestamp"] or 0)
        if age > self.trig.get("max_age_seconds", 120):
            return False, f"消息过旧({int(age)}s)"

        if payload["type"] == "图片":
            # 图片没有正文、@ 也进不来：该不该回已经由 check_image 单独判过了，
            # 这里必须跳过下面那套「认名字 / @」的群聊闸门（否则永远卡在「群里没@我」）
            pass
        elif payload["is_group"]:
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

_SENT_END = "。！？!?…~"
_SENT_SOFT = "，,；;、"


def split_sentences(raw: str, max_chars: int = 0, max_parts: int = 0) -> list:
    """按句号/问号/感叹号/省略号把一段话切成几句，一句一条发。

    设计要点：
      * 连着的句末符号（如「？！」）留在同一句，不拆成两条；
      * 被换行分开的短句算独立一句（模型用换行分点时就派上用场）；
      * 太长的句子（超过 max_chars）再按逗号切，避免一条消息过长；
      * 超过 max_parts 的部分合并到最后一条，宁可一条长点也不刷屏。
    """
    text = (raw or "").strip()
    if not text:
        return []
    chunks: list = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        start, i, n = 0, 0, len(line)
        while i < n:
            if line[i] in _SENT_END:
                # 连着的句末符号算同一句：「？！」「……」「！！」都不拆开
                while i < n and line[i] in _SENT_END:
                    i += 1
                seg = line[start:i].strip()
                if seg:
                    chunks.append(seg)
                start = i
                continue
            i += 1
        seg = line[start:].strip()
        if seg:
            chunks.append(seg)

    limit = int(max_chars or 0)
    if limit > 0:
        out: list = []
        for c in chunks:
            buf = c
            while len(buf) > limit:
                # 在 [limit, 2*limit) 里找最后一个停顿符：不早于 limit 切（否则切太碎），
                # 又别拖太重（所以最多放宽到 2*limit）。窗口内没有就整句留着。
                cut = max(buf.rfind(p, limit, limit * 2) for p in _SENT_SOFT)
                if cut < 0:
                    break
                out.append(buf[: cut + 1].strip())
                buf = buf[cut + 1:].strip()
            if buf:
                out.append(buf)
        chunks = out

    if max_parts and len(chunks) > max_parts:
        head = chunks[: max_parts - 1]
        head.append("".join(chunks[max_parts - 1:]))
        chunks = head
    return chunks


def parse_multi(raw, separator: str = "|||", max_parts: int = 0) -> list:
    """按分隔符切分（显式分条模式）。separator 为空 → 整段一条，多行保留。"""
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

def split_for_send(raw: str, msc: dict) -> list:
    """按配置把一段回复切成要分开发的几条。

    ``msc`` 就是 ``ai.multi_send`` 那一坨：
      * ``mode: "sentence"``（默认）→ 按句子切，一句一条，更像真人打字；
      * ``mode: "separator"`` → 只在模型写了分隔符（默认 ``|||``）的地方切；
      * 分隔符无论哪种模式都**优先**当硬分界：模型想合并短句时写 ``|||`` 就行。
    """
    msc = dict(msc or {})
    on = msc.get("enabled", True)
    sep = (msc.get("separator", "|||") or "") if on else ""
    mode = (msc.get("mode") or "sentence") if on else "off"
    mx = int(msc.get("max_messages", 4) or 0)
    if mode == "off" or not on:
        return parse_multi(raw, "", 0)
    parts = parse_multi(raw, sep, 0) if sep else [(raw or "").strip()]
    out: list = []
    for p in parts:
        if mode == "sentence":
            out.extend(split_sentences(p, int(msc.get("max_chars_per_message", 90) or 0), 0))
        else:
            out.append(p)
    out = [t for t in out if t]
    if mx and len(out) > mx:
        head = out[: mx - 1]
        head.append("".join(out[mx - 1:]))
        out = head
    return out


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


def test_image_file(path: str, question: str, cfg: dict, me_name: str, api_key: str) -> int:
    """用一张本地图片走一遍「看图 + 生成回复」（不发消息）。只读，安全。"""
    with open(path, "rb") as f:
        data = f.read()
    mime = image_in.sniff_mime(data[:16])
    if not mime:
        print(f"⚠ 不是标准图片格式（API 只收 JPEG/PNG/GIF/WebP）：{path}")
        return 1
    print(f"图片：{path}（{len(data)//1024}KB {mime}）")
    reply = ai_reply(cfg["ai"], question, "测试好友", "测试会话", False,
                     history=[f"测试好友: {question}"] if question else None,
                     me_name=me_name, api_key=api_key, images=[(data, mime)])
    ms = dict(cfg["ai"].get("multi_send") or {})
    texts = split_for_send(reply, ms)
    print(f"AI 生成 {len(texts)} 条：")
    for i, t in enumerate(texts, 1):
        print(f"  ({i}) {t}")
    return 0 if texts else 1


def main():
    # ---- 只读诊断模式：必须在拿单实例锁之前处理，机器人跑着也能用 ----
    # （--test-image 一张图 → 看它看不看得懂，默认不发）
    if "--test-image" in sys.argv:
        ip = argparse.ArgumentParser(description="试一下机器人看不看得懂一张图片（默认只打印）")
        ip.add_argument("--config", default=CONFIG_PATH)
        ip.add_argument("--test-image", default="")
        ip.add_argument("--question", default="", help="配一句话一起发（可选）")
        ip.add_argument("--send", action="store_true",
                        help="把生成的回复发到「文件传输助手」（测试专用）")
        ia, _unknown = ip.parse_known_args()
        icfg = load_config(ia.config)
        db0 = WeChatDB()
        res0 = SenderResolver(db0)
        ikey = load_api_key()
        me0 = (db0.get_self_info() or {}).get("nick_name") or ""
        target = (ia.test_image or "").strip()
        question = ia.question or "嗯？"
        if os.path.isfile(target):
            rc = test_image_file(target, question, icfg, me0, ikey)
            if rc or not ia.send:
                return rc
            # --send 时重跑一次拿回复文本（只在文件传输助手里发，测试专用）
            with open(target, "rb") as f:
                data = f.read()
            reply = ai_reply(icfg["ai"], question, "测试好友", "测试会话", False,
                             history=[f"测试好友: {question}"], me_name=me0,
                             api_key=ikey, images=[(data, image_in.sniff_mime(data[:16]))])
            texts = split_for_send(reply, dict(icfg["ai"].get("multi_send") or {}))
            if texts:
                box = {"gui": None}

                def _g():
                    if box["gui"] is None:
                        from wechatauto.guia import WeChatGUI
                        box["gui"] = WeChatGUI()
                    return box["gui"]

                tgt = "filehelper"
                ok = send_multi_impl(_g, threading.Lock(), db0, texts, tgt,
                                     db0.get_nickname(tgt) or tgt)[0]
                print(f"发送到「文件传输助手」：{'成功' if ok else '失败'}")
            return 0
        # 不是文件路径 → 当成「会话里最近一张图」：--test-image 某个群
        cu = target
        if cu and not _looks_like_wxid(cu):
            try:
                cu = db0.group_name_to_id(cu) or db0.username_by_nickname(cu) or cu
            except Exception:  # noqa: BLE001
                cu = target
        if not cu:
            print("⚠ 用 --test-image 指定一张本地图片路径，或一个会话（取它最近一张图）")
            return 1
        rows = db0.get_image_rows(cu, limit=1)
        if not rows:
            print(f"会话「{db0.get_nickname(cu) or cu}」里没有图片消息")
            return 1
        lid = rows[0].get("local_id")
        odir = os.path.join(BASE_DIR, "media")
        data, mime = image_in.read_image(db0, cu, lid, _as_text(rows[0].get("content")),
                                        save_dir=odir, log=print)
        if not data:
            print("这张图本机没有可用的缓存（没在微信里点开过），换一张或先在微信里看一眼")
            return 1
        name = db0.get_nickname(cu) or cu
        print(f"会话「{name}」最近一张图 #{lid}：{len(data)//1024}KB {mime}")
        reply = ai_reply(icfg["ai"], question, "群友", name, "@chatroom" in (cu or ""),
                         history=None, me_name=me0, api_key=ikey, images=[(data, mime)])
        texts = split_for_send(reply, dict(icfg["ai"].get("multi_send") or {}))
        print(f"AI 生成 {len(texts)} 条：")
        for i, t in enumerate(texts, 1):
            print(f"  ({i}) {t}")
        return 0

    # ---- 只读诊断模式：必须在拿单实例锁之前处理，机器人跑着也能用 ----
    # （--can-answer "<群里的话>" --chat 群名 → 看这句话会不会被主动接）
    if "--can-answer" in sys.argv:
        qp = argparse.ArgumentParser(description="试一下群里这句话会不会被主动接")
        qp.add_argument("--config", default=CONFIG_PATH)
        qp.add_argument("--can-answer", required=True)
        qp.add_argument("--chat", default="", help="群名或群 wxid")
        qp.add_argument("--can-answer-send", action="store_true", help="真的发出去")
        qa, _unknown = qp.parse_known_args()
        qcfg = load_config(qa.config)
        db0 = WeChatDB()
        res0 = SenderResolver(db0)
        qkey = load_api_key()
        me0 = (db0.get_self_info() or {}).get("nick_name") or ""
        cu = (qa.chat or "").strip()
        if cu and not _looks_like_wxid(cu):
            try:
                cu = db0.group_name_to_id(cu) or db0.username_by_nickname(cu) or cu
            except Exception:  # noqa: BLE001
                cu = qa.chat.strip()
        if not cu:
            cu = "filehelper"          # 默认沙盒：文件传输助手，绝不发到真人会话
        chat_name = db0.get_nickname(cu) or cu
        hist = build_history(db0, res0, cu, int(qcfg["ai"].get("history_limit", 8)))
        print(f"会话「{chat_name}」收到：{qa.can_answer}")
        print(f"  粗筛像是问题：{looks_like_question(qa.can_answer)}")
        if qa.can_answer_send:
            print(f"  ⚠ --can-answer-send 已开，判定为「接」时会真的发到「{chat_name}」")
        if looks_like_question(qa.can_answer):
            qai = _with_key(qcfg["ai"], qkey)
            hit = can_answer_question(qai, [me0], chat_name, "群友", qa.can_answer, hist,
                                      api_key=qkey)
            print(f"  判定该主动接：{hit}")
            if hit:
                reply = unsolicited_fill(qai, me0, chat_name, "群友", qa.can_answer, hist,
                                         api_key=qkey)
                ms = dict(qcfg["ai"].get("multi_send") or {})
                texts = split_for_send(reply, ms)
                print(f"  生成 {len(texts)} 条：")
                for i, t in enumerate(texts, 1):
                    print(f"     ({i}) {t}")
                if qa.can_answer_send:
                    if cu == "filehelper":
                        box = {"gui": None}

                        def _gui():
                            if box["gui"] is None:
                                from wechatauto.guia import WeChatGUI
                                box["gui"] = WeChatGUI()
                            return box["gui"]

                        send_multi_impl(_gui, threading.Lock(), db0, texts, cu, chat_name)
                    else:
                        # 测试消息不许发到任何真人会话（用户明确要求）
                        print(f"  ⚠ 拒绝发送：测试只允许发到「文件传输助手」，"
                              f"当前目标是「{chat_name}」")
        return 0

    ap = argparse.ArgumentParser(description="微信 AI 自动回复机器人")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--dry-run", action="store_true", help="只生成回复，不发送")
    ap.add_argument("--test-reply", default="", help="测试：把该文本当私信走完整流程（发到文件传输助手）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    os.environ["WECHATAUTO_RHYTHM"] = str(cfg.get("rhythm", "fast"))

    # 打补丁：发送时优先 Unicode 注入，绕开剪贴板（用户开了剪贴板同步）
    input_mode = "(未启用)"
    try:
        input_mode = install_input_patch(input_cfg(cfg))
    except Exception as e:  # noqa: BLE001
        input_mode = f"补丁失败（{type(e).__name__}: {e}）→ 仍走剪贴板"

    if not acquire_single_instance_lock():
        print("[已在运行] 机器人已经启动过了，不要再开一个——两个进程会抢微信的")
        print("           解密缓存，第二个必然报 PermissionError。")
        print(f"           要重启：双击 restart_bot.bat（它会先停旧实例再启动）；")
        print(f"           若确认没有别的实例在跑，删掉 {LOCK_FILE} 再试。")
        return 1
    import atexit
    atexit.register(release_lock)

    db = WeChatDB()
    resolver = SenderResolver(db)
    info = db.get_self_info()
    lock_wxid, lock_nick, lock_self_id, extra_ids = _account_identity(db)

    api_key = load_api_key()
    if not api_key:
        print("[错误] 没拿到 DEEPSEEK_API_KEY（环境变量或 ~/.dsh/.credentials.yaml）")
        return 1

    policy = ReplyPolicy(cfg, resolver, db)
    policy.api_key = api_key
    uns = UnsolicitedPolicy(cfg, db)
    uns.api_key = api_key

    # 发送端：只初始化一次，避免每次发送都重新校准布局
    sender = {"gui": None}
    send_lock = threading.Lock()

    def get_gui():
        if sender["gui"] is None:
            from wechatauto.guia import WeChatGUI
            sender["gui"] = WeChatGUI()
            wire_gui(sender["gui"])        # 登记给输入补丁，连发才能复用输入框
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
    print(f"自身 sender_id：{lock_self_id}"
          f"{'（⚠ filehelper 里出现多个 id，自身判定可能不准）' if extra_ids else ''}")
    if extra_ids:
        print("  ⚠ 建议退出重登微信、或跑 check_account.py 复核，否则可能回复自己")
    try:                                    # 多账号时把另一个号也报出来，便于确认没挑错
        from wechatauto.db import list_accounts
        acts = list_accounts() or []
        others = [a.get("wxid") for a in acts if a.get("wxid") and a.get("wxid") != lock_wxid]
        if others:
            print(f"⚠ 本机还有其它微信账号：{'、'.join(others)}")
            print("  机器人只操作上面这个账号；换号后必须重启机器人（配置不热加载，账号也不重选）")
    except Exception:  # noqa: BLE001
        pass
    print(f"策略：私聊={cfg['trigger']['private']} 群@={cfg['trigger']['group_at']} "
          f"@{policy.at_names} 冷却={cfg['limits']['per_chat_cooldown']}s 节奏={cfg.get('rhythm')}")
    if cfg["trigger"].get("context_judge"):
        print(f"群内额外触发：提到「{'、'.join(policy.name_hits)}」即回；"
              f"话题相关时再用一次轻量判定（回看 {cfg['trigger'].get('context_lookback', 8)} 条）")
    print(uns.describe())
    _ic = image_in.image_cfg(cfg["ai"])
    if _ic.get("enabled"):
        print(f"看图：开（私聊={'回' if cfg['trigger'].get('private') else '不回'}，"
              f"群图={'回' if cfg['trigger'].get('group_image') else '不回'}，"
              f"detail={_ic.get('detail')}，单张≤{int(_ic.get('max_bytes') or 0)//1024//1024}MB，"
              f"缓存目录={_ic.get('save_dir') or os.path.join(BASE_DIR, 'media')}）")
    else:
        print("看图：关（config.json → ai.image.enabled 打开后，机器人才能看到图片内容）")
    print(f"模式：{'DRY-RUN（不发送）' if args.dry_run else '正式（会真的发送）'}")
    print(f"输入方式：{input_mode}")
    # 上游节奏层：档位决定写动作间隔/突发上限，误用默认档会莫名其妙等几十秒
    try:
        from wechatauto import rhythm
        _prof = rhythm.profile()      # 会按需加载并把档位落盘到 ~/.wechatauto/rhythm.json
        print(f"节奏档位：{getattr(_prof, 'name', '?')}"
              f"（写动作间隔 {getattr(_prof, 'gap', '?')}s，"
              f"窗口 {getattr(_prof, 'window', '?')}s 内上限 {getattr(_prof, 'burst', '?')} 次，"
              f"撞上限冷却 {getattr(_prof, 'cooloff', '?')}s）")
    except Exception as e:  # noqa: BLE001
        print(f"节奏档位：读取失败（{type(e).__name__}: {e}）")
    print(f"回复日志 → {REPLY_LOG}\n", flush=True)

    lst = Listener(db, interval=1.0)

    def build_mem_block(payload: dict) -> str:
        """记忆档案 + 本群成员名单（两块都注入，避免认错人/编名字）。"""
        block = ""
        if cfg["ai"].get("memory", {}).get("enabled", True):
            try:
                block = memory_store.prompt_block(memory_store.load(payload["chat_username"]))
            except Exception as e:  # noqa: BLE001
                print(f"  [读取记忆失败] {type(e).__name__}: {e}", flush=True)
            if block:
                print(f"  · 已注入 {payload['chat_name']} 的记忆档案", flush=True)
        if payload["is_group"]:
            try:
                roster = resolver.roster(payload["chat_username"])
                names = [n for w, n in roster.items() if n and w != resolver.me]
                if names:
                    block += ("\n\n【本群成员（当前昵称）】" + "、".join(names[:60])
                              + "\n称呼别人只能用这些名字或聊天记录里出现过的名字，"
                                "绝不要自己编名字；不确定是谁就直接问。")
                    print(f"  · 已注入本群 {len(names)} 位成员名单", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  [读取群成员失败] {type(e).__name__}: {e}", flush=True)
        return block

    mem = image_in.ImageMemory()
    img_dir = image_in.image_cfg(cfg["ai"]).get("save_dir") or os.path.join(BASE_DIR, "media")

    def fetch_image(payload: dict, local_id):
        """取出这条图片消息的字节（带缓存）；拿不到返回 None。"""
        if local_id is None:
            return None
        hit = mem.get(payload["chat_username"], local_id)
        if hit:
            return hit
        ic = image_in.image_cfg(cfg["ai"])
        data, mime = image_in.read_image(db, payload["chat_username"], local_id, "",
                                         save_dir=img_dir, log=print)
        if not data:
            return None
        if len(data) > int(ic.get("max_bytes") or image_in.MAX_BYTES_DEFAULT):
            print(f"  · 图片 #{local_id} 太大（{len(data)//1024}KB），跳过", flush=True)
            return None
        item = (data, mime)
        mem.put(payload["chat_username"], local_id, item)
        return item

    def on_msg(msg: dict, _lst: Listener):
        payload = build_payload(db, resolver, msg)
        if not payload["is_self"]:
            policy._last_incoming[payload["chat_username"]] = time.time()
        # 图片消息：先取图（policy.check 要用它判断该不该回，生成回复时直接复用）
        local_id = msg.get("local_id")
        policy.pending_image = None
        if payload["type"] == "图片" and image_in.image_cfg(cfg["ai"]).get("enabled"):
            policy.pending_image = fetch_image(payload, local_id)
            if policy.pending_image:
                print(f"  · 已读取 {payload['chat_name']} 的图片 #{local_id}"
                      f"（{len(policy.pending_image[0])//1024}KB {policy.pending_image[1]}）",
                      flush=True)
        # 上下文只取一次（排除刚收到的这条，别重复喂给模型）；三处判定/生成共用
        history = build_history(
            db, resolver, payload["chat_username"],
            int(cfg["ai"].get("history_limit", 8)), msg.get("local_id"),
        )
        ok, reason = policy.check(payload, history)
        unsolicited = False
        if not ok:
            # 没人点我，但群里有人问了个我能答的问题 → 主动接一句
            ok, ureason = uns.check(payload, history, uns.api_key)
            if not ok:
                # 常见情况静默（否则群里每条消息都刷一行），只报异常原因
                extra = "" if ureason in ("不是在问问题", "主动接话未开启", "私聊不主动") \
                    else " / 主动接话：" + ureason
                if reason not in QUIET_REASONS and not reason.startswith("非文本"):
                    print(f"  · 跳过 {payload['chat_name']} | {reason}{extra}", flush=True)
                return
            unsolicited = True
            log_line(f"[{payload['time']}] 主动接话 {payload['chat_name']} | "
                     f"{payload['sender']} 问：{payload['content']}")

        ms = dict(cfg["ai"].get("multi_send") or {})
        me_name = info.get("nick_name") or ""

        if unsolicited:
            reply = unsolicited_fill(cfg["ai"], me_name, payload["chat_name"],
                                     payload["sender"], payload["content"], history,
                                     build_mem_block(payload), api_key=api_key)
        else:
            log_line(f"[{payload['time']}] 收到 {payload['chat_name']} | "
                     f"{payload['sender']}: {payload['content']}")
            reply = ai_reply(cfg["ai"], payload["content"], payload["sender"], payload["chat_name"],
                             payload["is_group"], history, me_name=me_name,
                             mem_block=build_mem_block(payload), api_key=api_key,
                             images=[policy.pending_image] if policy.pending_image else None)
        if not reply:
            log_line("  AI 未生成回复，跳过")
            return
        # 一句一条发（模式见 ai.multi_send）；想整段一条就把 enabled 关掉
        texts = split_for_send(reply, ms)
        policy.mark(payload["chat_username"])
        if unsolicited:
            uns.mark(payload["chat_username"])
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

    lst.add_all(on_msg, discover=True)
    lst.start()
    print("机器人运行中（Ctrl+C 停止）...", flush=True)
    last_check = time.time()
    try:
        while True:
            time.sleep(1)
            # 换号守卫：多账号时上游可能读到另一个号的库，self_id 会错位 → 会回复自己。
            # 一旦发现账号变了，立刻停手，宁可不出声也不误发。
            if time.time() - last_check >= 60:
                last_check = time.time()
                state = account_guard(db, lock_wxid)
                if state == "switched":
                    log_line(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠ 检测到微信登录账号已变化"
                             f"（启动时 {lock_nick}/{lock_wxid}）→ 立刻停止，避免认错人、回复自己")
                    print("\n[!] 登录账号变了，机器人已退出。请重新双击 restart_bot.bat 启动。")
                    break
                if state == "unknown":
                    print("  · 账号状态读不到（微信可能已退出登录），继续观察", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        lst.stop()
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
