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
import memory_vec
import chat_alias
import image_in
from wx_push import SenderResolver, build_payload, clean_content, fmt_time, _as_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
REPLY_LOG = os.path.join(BASE_DIR, "bot_replies.log")
LOCK_FILE = os.path.join(BASE_DIR, "bot.lock")
KEY_FILE = os.path.join(BASE_DIR, "apikey.txt")
CRED_PATH = os.path.expanduser("~/.dsh/.credentials.yaml")

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
    """按顺序找：环境变量 → 本目录 apikey.txt → DSH 凭据文件。

    密钥不写入本项目任何代码文件（apikey.txt 在 .gitignore 里，
    build_package.py 的白名单也不带它）。
    """
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    # 本地/发布版一致的读取位置：源码目录下的 apikey.txt（一行一个 key，# 开头是注释）
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and line.startswith("sk-"):
                    return line
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 读取 apikey.txt 失败: {e}")
    try:
        with open(CRED_PATH, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"DEEPSEEK_API_KEY:\s*[\"']?([A-Za-z0-9\-_.]+)", text)
        if m:
            return m.group(1)
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 读取凭据文件失败: {e}")
    return ""


# --------------------------------------------------------------------------
# AI 回复
# --------------------------------------------------------------------------

def build_history(db: WeChatDB, resolver: "SenderResolver", chat_username: str,
                  limit: int = 8, exclude_local_id=None, max_age_minutes: int = 0,
                  min_recent: int = 3, max_tokens: int = 0) -> list:
    """取该会话最近若干条消息作为上下文。

    纯文本才喂给模型；媒体消息只留一个占位说明（图片的具体内容只喂当前这条，
    不然历史里每张图都要解密 + 上千 token）。

    ``max_age_minutes`` > 0 时**只保留这个时间窗内的消息**：群安静的时候「最近 20 条」
    能一路翻回几小时前，把无关的旧话（比如主人自己调试时发的测试消息）也喂进去，
    模型会顺着那些旧话接茬。窗口内至少要留 ``min_recent`` 条，不够就放宽成「最近几条」，
    免得把上下文清空导致判定失准。

    ``max_tokens`` > 0 时再按**token 预算**从新往旧截：MiMo v2.6 窗口是 1M，
    预算配到百万级就等于「这个会话能翻到的历史全带上」，但不会把请求撑爆。
    估算用「1 个字符 ≈ 1 token」（中文偏保守），宁少勿多。
    """
    try:
        msgs = db.get_messages(chat_username, limit=limit + 2)
    except Exception:
        return []
    lines = []
    fresh = []
    now = time.time()
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
            if not mtype:
                continue
            line = f"{who}: [发了一张图片]" if mtype == "图片" else f"{who}: [{mtype}]"
        else:
            line = f"{who}: {text}"
        lines.append(line)
        if max_age_minutes and (now - float(m.get("create_time") or 0)) <= max_age_minutes * 60:
            fresh.append(line)
    if max_age_minutes:
        # 窗口内太少就退回「最近几条」，别把上下文清空
        lines = fresh if len(fresh) >= int(min_recent) else lines[-max(int(min_recent), 1):]
    lines = lines[-limit:]
    if max_tokens and max_tokens > 0:
        budget = int(max_tokens)
        picked, used = [], 0
        for line in reversed(lines):
            cost = len(line) + 1
            if picked and used + cost > budget:
                break
            picked.append(line)
            used += cost
        picked.reverse()
        if len(picked) < int(min_recent):
            picked = lines[-max(int(min_recent), 1):]
        lines = picked
    return lines


def _chat_call(cfg: dict, system_prompt: str, user_prompt: str, *, thinking: bool,
               max_tokens: int, timeout: float, temperature: float = 1.2,
               reasoning_effort: str = "high", api_key: str = "",
               images=None, chat_meta: dict = None) -> str:
    """调一次 chat/completions，返回正文（失败返回空串）。

    ``images`` 非空时走视觉输入：user 消息的 content 变成内容块数组，
    图片按 base64 data URL 内联（官方限制：单图 ≤32MiB、请求体 ≤48MiB，
    **图片只能放在 user 消息里**，system 里带图会 400）。

    ``chat_meta``（可选）：{"chat": 会话名, "sender": 发送者} —— 有它时，
    模型返回的思维链（reasoning_content）会作为 "thinking" 事件推给 GUI 界面展示。
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
        # DeepSeek 用 max_tokens；MiMo（OpenAI 兼容）用 max_completion_tokens，
        # 两者语义相同（思考+正文总长），按 provider 二选一发送，另一个不传。
        ("max_completion_tokens" if (cfg.get("provider") or "").lower() == "mimo" else "max_tokens"): max_tokens,
        "stream": False,
    }
    if thinking:
        # 思考模式：先出思维链再出答案
        body["thinking"] = {"type": "enabled"}
        # reasoning_effort：DeepSeek 走顶层参数；MiMo 官方 OpenAI 兼容文档没列它，
        # 但实测 chat 端点收（2026-10-25 实测 401/400 都没有，返回正常），
        # 且配置里 reasoning_effort=low 表示用户要求「推理等级用 low」，所以两家都发。
        body["reasoning_effort"] = reasoning_effort
    else:
        # 必须显式 disabled：deepseek-flash / mimo-v2.6-* 不传 thinking 时默认自己开思考，
        # 思维链会把 max_tokens 吃光、content 返回空串（二选一的判定会静默全错）。
        body["thinking"] = {"type": "disabled"}
        body["temperature"] = temperature

    # 联网搜索（MiMo web_search 工具）：只在 provider=mimo 且配置开启时挂上。
    # 由模型自己判断要不要搜（force_search=false），搜到的内容会以 annotations 返回，
    # 正文里模型会自己引用。判定类调用（judge）不挂——它们只要一个字，不需要联网。
    ws = cfg.get("web_search") or {}
    if ws.get("enabled") and (cfg.get("provider") or "").lower() == "mimo":
        body["tools"] = [{
            "type": "web_search",
            "max_keyword": int(ws.get("max_keyword", 3)),
            "force_search": bool(ws.get("force_search", False)),
            "limit": int(ws.get("limit", 3)),
        }]
        body["tool_choice"] = "auto"

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
        # 调用失败 = 这条消息不会有人回。写进日志，否则无窗口启动时无从查证
        log_line(f"[AI 失败] {type(e).__name__}: {e}")
        return ""
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    out = (msg.get("content") or "").strip()

    # 把思维链推给 GUI（界面「思考过程」区展示）。只在带 chat_meta 的正式回复调用时推，
    # 判定类调用（judge，thinking=False）本来就没有思维链。
    reasoning = str(msg.get("reasoning_content") or "").strip()
    if reasoning and chat_meta:
        try:
            BUS.event("thinking", chat=chat_meta.get("chat", ""),
                      sender=chat_meta.get("sender", ""), text=reasoning)
        except Exception:  # noqa: BLE001
            pass  # 推送失败绝不能影响回复主流程

    if not out:
        if reasoning:
            log_line(f"[AI 空回复] 思维链吃掉了 max_tokens（{reasoning[:40]!r}…），"
                     f"需要显式 thinking=disabled 或调大 max_tokens")
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
        # 只交代「显示名 ↔ 别人叫你」的映射；你是谁由 system_prompt（人设）说了算，
        # 这里不再断言「你就是本人[显示名]」——否则会和人设打架（2026-11-02 起人设可切换）
        ident = (f"你的微信昵称显示为「{me_name}」，别人写 @{me_name} 就是在叫你，"
                 f"用第一人称说话，别把自己当第三方。\n\n")

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
                      api_key=api_key, images=images,
                      chat_meta={"chat": chat_name, "sender": sender})
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
        ident = (f"你的微信昵称显示为「{me_name}」，别人叫这个名字就是在叫你，"
                 f"用第一人称说话，别把自己当第三方。\n\n")
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

    "unknown"（读不到）常见诱因是数据库连接的偶发抖动，**一次读不到不代表微信掉线**，
    所以调用方（主循环）要求连续多次 unknown 才告警 —— 见 GUARD_UNKNOWN_STREAK。
    """
    try:
        cur = (db.get_self_info() or {}).get("username") or ""
    except Exception:  # noqa: BLE001
        return "unknown"
    if not cur:
        return "unknown"
    return "ok" if cur == lock_wxid else "switched"


# 账号状态连续读不到多少次（每次心跳 60s）才认定「真出问题了」并告警。
# 单次抖动/连接重建时 get_self_info 偶发异常是已知现象（2026-10-25 观察到
# 连续 13 次误报、期间机器人功能完全正常），所以要攒够 streak 才值得打扰用户。
GUARD_UNKNOWN_STREAK = 3


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
        # (群, 发送者) → 她最近一次回这个人的时间。「接着聊」判定要用
        self._last_reply_to: dict = {}
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
            # ② 点名：只认「正文里喊了我的名字」（不再花钱跑话题判定）。
            #    `trigger.group_at_only`（默认 true，2026-11-17 用户要求）：
            #    群里只有 @ 我 或 喊我名字 才接，话题判定与「接着聊」一律不走。
            #    关掉（false）就退回老行为：话题相关 + 接着聊也能接，见 §7.5 / §7.18。
            at_only = bool(self.trig.get("group_at_only", True))
            if at_only:
                called = _mentions_name(text, self.name_hits, name_owner)
                if not (at_me or called):
                    return False, "群里没@我/没喊名字"
            elif not at_me:
                # ② 只是「可能跟我有关」才值得花一次便宜的判定；
                #    光出现我的名字不算——可能是顺口提到，或原话就跟我无关
                cand = _mentions_name(text, self.name_hits, name_owner) \
                    or _mentions_name(" ".join(recent or []), self.name_hits, name_owner) \
                    or any(w in text for w in _ADDRESS_WORDS) \
                    or text.endswith(("?", "？"))
                if not (cand and self._context_judge(payload, recent)):
                    # ③.5「接着聊」：她刚刚还在回这个人（同群、窗口内），就别装没看见。
                    # 「okok」这种既没 @ 也不像问句的短句，就是靠这条才接得住。
                    # 注意这里**不 return**：放行后照样要过下面的冷却与每分钟/每日上限。
                    win = float(self.trig.get("group_followup_seconds", 90) or 0)
                    sk = self.sender_key(payload)
                    followup = False
                    if win > 0 and sk:
                        with self._lock:
                            last_to = self._last_reply_to.get((payload["chat_username"], sk), 0)
                        followup = bool(last_to) and (time.time() - last_to <= win)
                    if not followup:
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

    @staticmethod
    def sender_key(payload: dict) -> str:
        """群里认人优先用 wxid：群昵称一天一换，名字靠不住（见 §7.5）。"""
        return str(payload.get("sender_wxid") or payload.get("sender") or "").strip()

    def mark(self, chat_username: str, sender_key: str = ""):
        now = time.time()
        with self._lock:
            self._bump_day()
            self._last_reply[chat_username] = now
            if sender_key:
                # 「接着聊」用：她刚回过谁，谁再说话就接着接
                self._last_reply_to[(chat_username, sender_key)] = now
            self._recent.append(now)
            self._count_today += 1


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

from bot_state import BUS


def log_line(line: str):
    print(line, flush=True)
    with open(REPLY_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    # 顺手推给 GUI：自动识别类型
    if line.startswith("[已发送]") or line.startswith("[发送重试]") or line.startswith("[发送放弃]") or line.startswith("[发送失败]"):
        BUS.event("send", chat="", text=line, ok=line.startswith("[已发送]"))
    elif "⚠" in line:
        BUS.event("warn", text=line)
    elif "错误" in line or "Traceback" in line or "[错误]" in line:
        BUS.event("error", text=line)


def relation_block(ai_cfg: dict, payload: dict) -> str:
    """按人指定关系（`ai.relations`）：命中谁，才对谁用另一套身份认知。

    默认谁都不是开拓者（提示词的记忆段这么写）；只有这里命中的那个人例外。
    匹配键：sender_wxid / sender 昵称 / 会话名，任一命中即可。
    没配 relations（或没命中）时返回空串，行为与以前完全一致。
    """
    rels = (ai_cfg or {}).get("relations") or []
    if not rels:
        return ""
    keys = {str(payload.get(k) or "").strip()
            for k in ("sender_wxid", "sender", "chat_name")}
    keys.discard("")
    for r in rels:
        pats = r.get("match") or []
        if isinstance(pats, str):
            pats = [pats]
        if keys & {str(p).strip() for p in pats if p}:
            return "【关系】" + str(r.get("text") or "").strip()
    return ""


def _looks_like_wxid(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("wxid_") or s.startswith("gh_") or s.endswith("@chatroom")


_ROOM_NAME_CACHE = {}  # chatroom wxid -> 群名（缓存；群里改名后重启 bot 刷新）


def _room_display_name(db, room_wxid: str) -> str:
    """从 contact.db 的 chat_room 表反查群名（get_nickname 只查 contact 表，
    群 wxid 在 contact 表里通常没有行，所以群消息会解析失败、直接放弃发送）。
    """
    if not room_wxid or db is None:
        return ""
    if room_wxid in _ROOM_NAME_CACHE:
        return _ROOM_NAME_CACHE[room_wxid]
    name = ""
    try:
        # 用上游自带的 get_nickname（返回 remark or nick_name or user），
        # 比自己手写 SQL 更稳——remark 优先，找不到才退 nick_name
        name = (db.get_nickname(room_wxid) or "").strip()
    except Exception:
        name = ""
    if name:
        _ROOM_NAME_CACHE[room_wxid] = name
    return name


def _private_display_name(db, user_wxid: str, chat_name: str) -> str:
    """私聊显示名解析：chat_name 已经是昵称就直接用；是 wxid 就反查数据库。"""
    n = (chat_name or "").strip()
    if n and not _looks_like_wxid(n):
        return n
    if db is None:
        return ""
    try:
        return (db.get_nickname(user_wxid) or "").strip()
    except Exception:
        return ""


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
    cu = (chat_username or "").strip()

    # 群聊：chat_username 是 chatroom wxid，专门反查群名
    if cu.endswith("@chatroom"):
        if db is not None:
            add(_room_display_name(db, cu))
        return cands

    # 私聊：chat_name 已经是昵称就直接用；是 wxid 就反查
    if db is not None:
        add(_private_display_name(db, cu, chat_name))

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

    # 兜底：非结尾的超短句（≤4 字）不单独发一条。对方昵称带句号（如「杨。」）时，
    # 模型在句首一挂称呼就会被切成孤儿条（2026-11-01 实测连发 8 次，见 §7.25）；
    # 被问号切飞的「♪」、开头的「：」这类符号碎片也一并吸收。
    if len(chunks) > 1:
        merged = [chunks[0]]
        head_short = len(chunks[0]) <= 4
        for c in chunks[1:]:
            if head_short or len(c) <= 4:
                merged[-1] = merged[-1] + c
                head_short = head_short and len(c) <= 4
            else:
                merged.append(c)
        # 结尾的纯符号碎片（如「…有戏了？♪」被切出的「♪」）并回上一句
        if len(merged) > 1 and not merged[-1].strip("…~♪♡、，。；：!?？！· "):
            merged[-2] = merged[-2] + merged.pop()
        chunks = merged

    # 兜掉聊天记录格式残留：模型偶尔学着上下文写成「名字：…」「我：…」（§7.25），
    # 每条开头的「称呼+冒号」直接剥掉——「某某。：」「某某: 」这类前缀不许出门。
    # 只剥「1~2 个中文字+冒号」，且白名单里的正经标签（原因/办法/步骤…）不剥
    keep_labels = {"原因", "结果", "办法", "方法", "比如", "例如", "注意", "结论",
                   "建议", "问题", "步骤", "例子", "关键", "重点", "信号", "症状",
                   "第二", "第三", "补充", "反正"}

    def _strip_talk_prefix(c: str) -> str:
        m = re.match(r"^([一-鿿。·]{1,2})[：:]\s*", c)
        if m and m.group(1) not in keep_labels:
            return c[m.end():] or c
        return c

    chunks = [_strip_talk_prefix(c) for c in chunks]

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

def is_silence(raw) -> bool:
    """这条回复是不是「这次干脆不回」（提示词里约定：整个回复只输出 [SKIP]）。

    2026-11-18 用户要求：让 AI 自己判断这个话题值不值得回（敏感话题、对方明说别回、
    错发/广告、群里已有人答……），不想回就直接沉默。这里认三种写法：
    纯 ``[SKIP]``、``【SKIP】``、以及包了引号/空白/句号的同一件事；**只认整条**——
    正文里提到「SKIP」不算（免得把正常回复吃掉）。
    """
    t = (raw or "").strip()
    if not t:
        return False
    t = t.strip("「」\"'“”‘’ \t\r\n。.~～")
    return t.upper() in ("[SKIP]", "【SKIP】", "SKIP", "［SKIP］")


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


def _sent_watermark(db, chat_username: str) -> tuple:
    """发送前拍落库水位：会话最后一条的 (sort_seq, local_id)。

    没有水位就不能判断「这条是不是刚发的」——同一段话昨天发过、库里还留着，
    拿它当证据会把「其实没发出去」误判成成功（§7 坑 4 的正解）。
    """
    if db is None:
        return None
    try:
        rows = db.get_messages(chat_username, limit=5)
    except Exception:  # noqa: BLE001
        return None
    seqs = [int(r.get("sort_seq") or 0) for r in rows]
    if not seqs:
        return None
    return {"seq": max(seqs),
            "ids": {(int(r.get("sort_seq") or 0), int(r.get("local_id") or 0))
                    for r in rows}}


def _confirmed_sent(db, chat_username: str, text: str, mark, self_id,
                    tries: int = 3, wait: float = 1.5) -> bool:
    """回读数据库确认这条真的发出去了。

    只认**比水位新**的行，且正文逐字相等。微信落库有延迟，所以轮询几次；
    都查不到就返回 False（宁可当成没发出去，交给上层重发——有水位门槛，
    不会把旧消息当成这次发出去的）。
    """
    if db is None or not text:
        return False
    for i in range(tries):
        try:
            rows = db.get_messages(chat_username, limit=8)
        except Exception:  # noqa: BLE001
            return False
        for m in rows:
            seq = int(m.get("sort_seq") or 0)
            if mark:
                if seq < mark["seq"]:
                    continue
                if (seq, int(m.get("local_id") or 0)) in mark["ids"]:
                    continue
            if self_id and int(m.get("sender_id") or 0) != self_id:
                continue
            content = (m.get("content") or "")
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            if str(content).strip() == text.strip():
                return True
        if i + 1 < tries:
            time.sleep(wait)
    return False


def _short(resp, limit: int = 60) -> str:
    """日志里 resp 太长会把行撑爆（整个 resp 字典）。"""
    s = str(resp).replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _chat_matches(cur: str, targets) -> bool:
    """当前打开会话名是否就是要发的那个（含「某某与某某的聊天」这种独立窗标题）。"""
    cur = (cur or "").strip()
    if not cur:
        return False
    for n in targets or []:
        n = (n or "").strip()
        if not n:
            continue
        if cur == n or cur in n or n in cur:
            return True
    return False


def _window_ok(uia) -> bool:
    try:
        return bool(uia.ensure_window())
    except Exception:  # noqa: BLE001
        return False


def _focus_chat_input(uia) -> bool:
    """把键盘焦点交回输入框——**不点鼠标、不重开会话**。

    用户 2026-11-18 的原话：「输入框已经到了这个对话，就不用再点一遍；重复点还会把输入框点掉」。
    微信 4.x 里再点一次已经是当前高亮的会话行，行为等于「关闭会话」，命中率低得多；
    所以失败时优先走这里：焦点交给输入框控件，再按一次输入框里已有的内容校验一遍。
    """
    try:
        ctrl = uia._chat_input()      # noqa: SLF001 —— 上游私有方法，与补丁里同一处用法
        if ctrl is None:
            return False
        try:
            ctrl.SetFocus()
        except Exception:  # noqa: BLE001
            pass
        try:
            uia._win.SetFocus()       # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.15)
        return True
    except Exception:  # noqa: BLE001
        return False


def _ensure_chat_open(get_gui, names, uia=None) -> tuple:
    """确保目标会话是**当前打开的**会话，返回 (是否就绪, 说明)。

    顺序照用户要求排：已经在目标会话 → 什么都不做；不在 → 才点会话列表；
    都没有可点的名字 → 交给上层走搜索。**绝不因为「不确定」就重复点已打开的会话。**
    """
    gui = get_gui()
    try:
        u = uia if uia is not None else gui._get_uia()  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        return False, f"UIA 不可用（{type(e).__name__}）"
    if u is None:
        return False, "UIA 暂不可用"
    if not _window_ok(u):
        return False, "UIA 树未就绪"
    cur = (u.current_chat() or "")
    if _chat_matches(cur, names):
        return True, "已在当前会话（跳过点击）"
    for n in names or []:
        ok, why = open_chat_via_session_list(gui, n)
        if ok:
            return True, "列表点击"
        last = why
    return False, (last if names else "没有可点击的显示名")


def send_multi_impl(get_gui, send_lock, db, texts, chat_username, chat_name,
                    gap: float = 1.0, verify: bool = True,
                    self_id=None, names=None) -> tuple:
    """把若干条消息依次发给同一会话：只打开一次会话，之后连发。

    返回 (是否全部发出, 已发出的条数)。

    ``verify=True``（默认）时每条发完回读数据库确认。**这不是多余的**：
    UI 路径只看「回车有没有按下去」，不看消息有没有真发出去——焦点丢了
    会静默只填不发，而上层一旦重试就会把已发出去的那句**再发一遍**（§7.20）。

    2026-11-18 的改动（§7.39）：
      * 目标会话**已经是当前会话**时，不再点会话列表（点已高亮的行＝关掉会话，
        输入框会跟着没掉，只能重开）；
      * 单条失败时先试着**把焦点交回输入框**重试，还不行才点会话列表，最后才搜索；
      * ``names`` 可由调用方直接给（批量回复时已经解析过，省一次数据库查询）。
    """
    texts = [t for t in (texts or []) if (t or "").strip()]
    if not texts:
        return True, 0
    with send_lock:
        t0 = time.time()
        sent = 0
        try:
            gui = get_gui()
            names = [n for n in (names or []) if n] or \
                resolve_display_names(db, chat_username, chat_name)
            opened, why = _ensure_chat_open(get_gui, names)
            if not opened and not names:
                # 昵称解析不出来（如私聊只拿到 wxid）：连搜索框这条路都走不通，
                # 上游会白等 75s×3 次。提前放弃，把原因写清楚（§7.20）。
                log_line(f"[发送放弃] {chat_name}: 解析不出显示名（只有 "
                         f"{chat_username}），无法定位会话，跳过发送")
                return False, 0
            who = names[0] if names else (chat_name or chat_username)
            if opened:
                via = why if "已" in why else "列表点击"
            else:
                via = f"搜索回退[{why}]"

            def _inject(text: str) -> tuple:
                """注入一条 → (是否成功, resp 或错误文本)。异常在这里就地吞掉，交给重试。"""
                try:
                    # 会话已经确保了：传 None，别让 guia 再 open_chat 一遍（那是重复点击的来源）
                    resp = gui.send_msg(text, None if opened else who, False)
                    return _resp_ok(resp), resp
                except Exception as e:  # noqa: BLE001
                    return False, f"{type(e).__name__}: {e}"

            def _retry(text: str, mark) -> tuple:
                """失败重试（按顺序、每步各试一次）：
                ① 把焦点交回输入框直接重打——**不动会话**（2026-11-18 新增，用户要求）；
                ② 还不行才点会话列表 / 走搜索；
                ③ 如果会话本来已经是当前的，最后再补一次焦点重试。
                """
                tried_focus = False
                try:
                    u = get_gui()._get_uia()  # noqa: SLF001
                except Exception:  # noqa: BLE001
                    u = None
                if u is not None and _focus_chat_input(u):
                    tried_focus = True
                    ok, resp = _inject(text)
                    if ok and (not verify or _confirmed_sent(
                            db, chat_username, text, mark, self_id)):
                        log_line("  · 重设输入框焦点后重发成功（没点会话）")
                        return True, resp
                rwhy = "会话本来就是当前会话，跳过点击"
                if not opened:
                    reopened, rwhy = _ensure_chat_open(get_gui, names, u)
                    if reopened:
                        time.sleep(0.3)
                        ok, resp = _inject(text)
                        if ok and verify and not _confirmed_sent(
                                db, chat_username, text, mark, self_id):
                            ok, resp = False, f"重试后仍未确认（{_short(resp)}）"
                        if ok:
                            return True, resp
                if not tried_focus and u is not None and _focus_chat_input(u):
                    ok, resp = _inject(text)
                    if ok and (not verify or _confirmed_sent(
                            db, chat_username, text, mark, self_id)):
                        log_line("  · 重设输入框焦点后重发成功（没点会话）")
                        return True, resp
                    rwhy = f"{rwhy}；焦点重试也没成"
                return False, f"重试两次都失败（{rwhy}）"

            for i, text in enumerate(texts):
                if not (opened or i == 0):
                    break        # 会话没打开成功，只有第一条能靠搜索框补
                # 水位必须在任何 UI 动作**之前**拍：一旦发出去，库里已经有新行了
                mark = _sent_watermark(db, chat_username) if verify else None
                ok, resp = _inject(text)
                # UI 说成功不等于真发出去：回读确认，认不出来的当没发出去
                if ok and verify:
                    if not _confirmed_sent(db, chat_username, text, mark, self_id):
                        ok, resp = False, f"回读未确认（{_short(resp)}）"
                        log_line(f"[未确认] {chat_name}: 第{i + 1}/{len(texts)}条 UI 报成功，"
                                 f"但库里查不到，按未发出处理")
                if not ok:
                    log_line(f"[发送重试] {chat_name}: 第{i + 1}/{len(texts)}条失败（{_short(resp)}），"
                             f"先重设焦点再试一次")
                    ok, resp = _retry(text, mark)
                log_line(f"{'[已发送]' if ok else '[发送异常]'} → {chat_name}: {text} "
                         f"| {via}{'' if len(texts) == 1 else f' 第{i + 1}/{len(texts)}条'} "
                         f"{time.time() - t0:.1f}s | resp={_short(resp)}")
                if not ok:
                    log_line(f"[放弃] {chat_name}: 第{i + 1}/{len(texts)}条重试后仍失败，"
                             f"剩下的不发了（已发出 {sent} 条）")
                    return False, sent
                sent += 1
                if i + 1 < len(texts):
                    # 连发之间留点间隔，别撞上游库的节奏闸
                    time.sleep(max(0.0, gap))
            return True, sent
        except Exception as e:  # noqa: BLE001
            log_line(f"[发送失败] {chat_name}: {type(e).__name__}: {e}（已发出 {sent} 条）")
            return False, sent


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
        hist = build_history(db0, res0, cu, int(qcfg["ai"].get("history_limit", 8)),
                             max_tokens=int(qcfg["ai"].get("history_max_tokens", 0) or 0))
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

    # 双开（大小号同时登录）时自行判断用哪个号：上游库默认按「数据库最近改动」挑账号，
    # 但双开下两个号都在写库，可能挑错。这里把所有候选账号的「最近活动时间」比一遍，
    # 取最新的那个（最近在聊天的就是当前活跃号）。挑完后显式传给 WeChatDB，
    # 避免上游启发式选错。
    # 2026-11-01（用户点头加的钉号）：config.json 顶层 `account` 设了就按它选、
    # 跳过活动度启发式——升 1.2.9 当晚活动度把号挑到了大号上（见 §7.26）。
    picked_account = None
    _pin = str(cfg.get("account") or "").strip()
    if _pin:
        try:
            from wechatauto.db import list_accounts as _la_pin
            for _a in _la_pin() or []:
                if _pin in (_a.get("account"), _a.get("wxid")) or \
                        str(_a.get("account") or "").startswith(_pin + "_"):
                    picked_account = _a.get("account")
                    print(f"  配置钉号：{_pin} → {picked_account}（跳过活动度选号）", flush=True)
                    break
            if picked_account is None:
                print(f"  [警告] config.account={_pin} 没匹配到本机账号，退回活动度选号", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [警告] 钉号解析失败，退回活动度选号：{type(e).__name__}: {e}", flush=True)
            picked_account = None

    if picked_account is None:
        try:
            from wechatauto.db import list_accounts as _list_accounts
            _acts = _list_accounts() or []
            if len(_acts) >= 2:
                _acts_sorted = sorted(_acts, key=lambda a: a.get("last_activity") or 0, reverse=True)
                picked_account = _acts_sorted[0].get("account")
                import datetime as _dt
                _names = [f"{a.get('wxid')}({_dt.datetime.fromtimestamp(a.get('last_activity') or 0):%H:%M})"
                          for a in _acts_sorted]
                print(f"  双开检测：{len(_acts)} 个账号，按最近活动排序 → {' > '.join(_names)}", flush=True)
                print(f"  选定账号：{picked_account}（最近活动的那个）", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [警告] 账号活跃度检测失败，退回上游默认选择：{type(e).__name__}: {e}", flush=True)
            picked_account = None

    db = WeChatDB(account=picked_account)
    resolver = SenderResolver(db)
    info = db.get_self_info()
    lock_wxid, lock_nick, lock_self_id, extra_ids = _account_identity(db)

    # 会话别名自愈：chat_alias.json 是运行时文件，被清掉/写坏时面板里同一对话会裂成两条（§7.29）
    try:
        _fixed = chat_alias.repair(db)
        if _fixed:
            print(f"  会话别名：补了 {len(_fixed)} 条裸 ID", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [会话别名] 自愈失败（不影响运行）：{type(e).__name__}: {e}", flush=True)

    api_key = load_api_key()
    if not api_key:
        print("[错误] 没拿到 API Key（环境变量 / apikey.txt / ~/.dsh/.credentials.yaml 三处都没有）")
        return 1
    # 启动时打印本次生效的 provider / base_url / model，便于一眼确认接的是哪家
    print(f"  AI: provider={cfg.get('ai', {}).get('provider')} "
          f"base_url={cfg.get('ai', {}).get('base_url')} "
          f"model={cfg.get('ai', {}).get('model')}", flush=True)
    # 长期记忆（mem0 + 本地向量）：后台预热，别让第一条消息等模型加载
    if memory_vec.enabled(cfg.get("ai", {})):
        _v = memory_vec.vcfg(cfg.get("ai", {}))
        print(f"  长期记忆: 开（mem0 + {_v.get('model') or 'BAAI/bge-small-zh-v1.5'}，"
              f"库={_v.get('dir') or os.path.join(BASE_DIR, 'memory_vec')}，"
              f"infer={bool(_v.get('infer', False))}）", flush=True)
        threading.Thread(target=memory_vec.warmup,
                         args=(cfg.get("ai", {}), api_key), daemon=True).start()
    else:
        print("  长期记忆: 关（config.json → ai.memory.vector.enabled）", flush=True)

    policy = ReplyPolicy(cfg, resolver, db)
    policy.api_key = api_key
    uns = UnsolicitedPolicy(cfg, db)
    uns.api_key = api_key

    # 发送端：只初始化一次，避免每次发送都重新校准布局
    sender = {"gui": None}
    send_lock = threading.Lock()
    # 发送后是否回读数据库确认（§7.20）。关掉会退回「UI 报成功就算成功」，
    # 那样只输入不发送和重复发送都会回来。想临时关掉排查就设 false。
    send_verify = bool((cfg.get("send") or {}).get("verify", True))
    log_line(f"[发送确认] 回读数据库：{'开' if send_verify else '关'}")

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

    def _disp_names(payload: dict) -> list:
        """发送前解析一次会话显示名：批量回复时省掉每个会话重复解库（§7.39）。"""
        try:
            return resolve_display_names(db, payload["chat_username"], payload["chat_name"])
        except Exception:  # noqa: BLE001
            return []

    def send_multi(texts, chat_username: str, chat_name: str, names=None) -> tuple:
        """一次发多条（同一个会话只打开一次；会话已打开时连点击都省掉）。"""
        texts = [t for t in (texts or []) if (t or "").strip()]
        if args.dry_run:
            for t in texts:
                log_line(f"[DRY-RUN] 本应发送「{chat_name}」: {t}")
            return True, len(texts)
        return send_multi_impl(get_gui, send_lock, db, texts, chat_username, chat_name,
                               verify=send_verify, self_id=resolver.self_id,
                               names=names)

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
    log_line(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动 | 账号={info.get('nick_name')} "
             f"({info.get('username')}) self_id={lock_self_id} rhythm={cfg.get('rhythm')} "
             f"私聊={cfg['trigger']['private']} 群@={cfg['trigger']['group_at']}")
    BUS.set_status(
        online=True, running=True,
        account=info.get("nick_name") or "", account_wxid=info.get("username") or "",
        self_id=lock_self_id, rhythm=str(cfg.get("rhythm", "")),
        dry_run=bool(args.dry_run), started_at=time.time(),
        reply_count_today=0,
    )
    BUS.event("system", text=f"启动完成 · 账号={info.get('nick_name')} 节奏={cfg.get('rhythm')}")
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
    print("AI 自主沉默：" + ("开（话题不该接时她会只输出 [SKIP]，程序就不发）"
                         if bool((cfg["ai"].get("silence") or {}).get("enabled", True))
                         else "关"))

    print(f"模式：{'DRY-RUN（不发送）' if args.dry_run else '正式（会真的发送）'}")
    _bc0 = dict(cfg["ai"].get("batching") or {})
    if _bc0.get("enabled"):
        print(f"连发合并：开（同一人 {int(_bc0.get('wait_ms', 1200) or 1200)}ms 内连发多条 → "
              f"合并成一次回复，最多 {int(_bc0.get('max_messages', 5) or 5)} 条，"
              f"回复最多 {cfg['ai'].get('multi_send', {}).get('max_messages', 4)} 条）")
    else:
        print("连发合并：关（ai.batching.enabled=true 打开后，对方一口气发几条只回一次）")
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

    def build_mem_block(payload: dict, history: list | None = None) -> str:
        """关系说明 + 记忆档案 + 长期记忆召回 + 本群成员名单（都注入，避免认错人/编名字）。"""
        block = relation_block(cfg["ai"], payload)
        if block:
            print(f"  · 已注入「{payload['chat_name']}」的关系说明", flush=True)
        # 记忆档案按会话类型分开关：memory.enabled 总开关 + memory.group_enabled 群聊分开关
        # （2026-11-02：群聊不进数据库，只有私聊建档；某好友的私聊档继续开）
        _mcfg = cfg["ai"].get("memory", {}) or {}
        _mem_on = bool(_mcfg.get("enabled", True)) and (
            bool(_mcfg.get("group_enabled", True)) or not payload["is_group"])
        if _mem_on:
            try:
                mb = memory_store.prompt_block(memory_store.load(payload["chat_username"]))
            except Exception as e:  # noqa: BLE001
                mb = ""
                print(f"  [读取记忆失败] {type(e).__name__}: {e}", flush=True)
            if mb:
                # 追加而不是覆盖：旧逻辑 `block = ...` 会把上面刚注入的关系说明顶掉
                block = (block + "\n\n" + mb) if block else mb
                print(f"  · 已注入 {payload['chat_name']} 的记忆档案", flush=True)
        # 长期记忆（mem0 + 本地向量）：按这条消息的语义捞回以前聊过的相关片段。
        # 只在这种会话真的建档时才用（群聊默认不建），且与当前上下文重复的会被丢掉。
        if _mem_on and memory_vec.enabled(cfg["ai"]):
            try:
                vb = memory_vec.block(cfg["ai"], payload["chat_username"],
                                      (payload.get("content") or "")[:200],
                                      history or [])
            except Exception as e:  # noqa: BLE001
                vb = ""
                print(f"  [长期记忆召回失败] {type(e).__name__}: {e}", flush=True)
            if vb:
                block = (block + vb) if block else vb.lstrip("\n")
                print(f"  · 已注入 {payload['chat_name']} 的长期记忆片段", flush=True)
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
        # 整个回调包 try/except：任何未预期异常都不能抛给 Listener，
        # 否则上游会重试 3 次（同一条消息触发 4 次回调）并最终丢弃该消息、
        # 推进水位 —— 表现为「同一条消息被记 4 次」+「后续私聊消息全收不到」（§7.22.2）
        try:
            wait_ms = int(((cfg["ai"].get("batching") or {}).get("wait_ms", 0)) or 0)
            if wait_ms > 0 and _enqueue_for_batch(msg, wait_ms):
                return
            _on_msg_inner(msg, _lst)
        except Exception as e:  # noqa: BLE001
            log_line(f"[回调异常] {type(e).__name__}: {e}（msg.local_id={msg.get('local_id')}）")
            import traceback
            traceback.print_exc()

    # ---- 连发合并（ai.batching，2026-11-18，§7.40）----------------------------
    # 人打字经常一口气发三四条。以前每条都会各回一遍，现在：会话收到第一条后开一个
    # 短窗口，窗口内同一个人再发就并进同一批，窗口结束再**统一回一次**。
    # 三道闸：① 只有真正会触发回复的消息才开窗（不回的消息直接走老流程，不进队列）；
    # ② 窗口内来的是别人或被回复失败，就先把这批立即冲掉；③ 队列满了/等太久一律冲掉，
    # 绝不会让消息卡住不回。
    _batch_lock = threading.RLock()
    _batch_pending: dict = {}       # chat_username -> [msg, ...]（只装「还没处理」的）
    _batch_timer: dict = {}         # chat_username -> threading.Timer

    def _batch_cfg() -> dict:
        bc = dict(cfg["ai"].get("batching") or {})
        bc.setdefault("enabled", False)
        bc.setdefault("wait_ms", 1200)
        bc.setdefault("max_messages", 6)
        bc.setdefault("max_chars", 0)
        return bc

    def _enqueue_for_batch(msg: dict, wait_ms: int) -> bool:
        """排队并返回「已接管」；返回 False 表示这条应该走老流程（立即处理）。

        判断「会不会触发回复」要花一次 policy.check（含可能的话题判定）：以前只在这条
        消息**真的会回**时才开窗，不回的消息原样走老逻辑。但判定调用会花 token，
        所以这里先用一个便宜的预筛（私聊 / 非群 / 被 @ / 喊名字 / 主动接话开着），
        预筛通过才做真判定，判定为否就当场按老逻辑处理掉（不重复判定）。
        """
        if msg.get("is_self") or msg.get("_dsh_inner"):
            return False                 # 自己发的 / 内部回放的消息，直接走老流程
        bc = _batch_cfg()
        chat = msg.get("chat_username") or ""
        if not chat:
            return False
        with _batch_lock:
            pend = _batch_pending.get(chat) or []
            same = (not pend) or _same_sender(pend[0], msg)
            if pend and not same:
                # 换了个人说话：先把上一批冲掉，再单独给这条开窗
                _flush_locked(chat, "换了人说话")
                pend = []
        # 真判定（与老流程同一套 policy.check，不额外花 token）
        payload = build_payload(db, resolver, msg)
        history = build_history(
            db, resolver, payload["chat_username"],
            int(cfg["ai"].get("history_limit", 8)), msg.get("local_id"),
            max_age_minutes=int(cfg["ai"].get("history_max_age_minutes", 0) or 0),
            max_tokens=int(cfg["ai"].get("history_max_tokens", 0) or 0),
        )
        ok, reason = policy.check(payload, history)
        if not ok:
            return False                 # 这条本来就不回：原样走老逻辑（会写「未回」日志）
        with _batch_lock:
            pend = _batch_pending.setdefault(chat, [])
            pend.append(dict(msg, _dsh_history=history, _dsh_payload=payload))
            _batch_timer.pop(chat, None)
            t = threading.Timer(wait_ms / 1000.0, _flush_batch_later, args=(chat,))
            t.daemon = True
            _batch_timer[chat] = t
            t.start()
            print(f"  · 「{payload['chat_name']}」收到一条，等 {wait_ms}ms 看还有没有连发…",
                  flush=True)
            if len(pend) >= int(bc.get("max_messages", 6) or 6):
                _flush_locked(chat, "凑够一批")

    def _same_sender(a: dict, b: dict) -> bool:
        return (a.get("sender_wxid") or "") == (b.get("sender_wxid") or "")

    def _flush_locked(chat: str, why: str) -> None:
        """把某会话攒着的一批立即处理掉（锁内调用）。"""
        with _batch_lock:
            t = _batch_timer.pop(chat, None)
            if t is not None:
                t.cancel()
            batch = _batch_pending.pop(chat, None) or []
        if batch:
            log_line(f"[连发合并] {chat[:12]}…：{len(batch)} 条一起回（{why}）")

        def _run():
            try:
                _process_batch(chat, batch, why)
            except Exception as e:  # noqa: BLE001
                log_line(f"[批量回复异常] {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()

        _run()

    def _flush_batch_later(chat: str) -> None:
        try:
            _flush_locked(chat, "窗口结束")
        except Exception as e:  # noqa: BLE001
            log_line(f"[批量回复异常] {type(e).__name__}: {e}")

    def _process_batch(chat: str, batch: list, why: str) -> None:
        """一批连发改成一次调用：合成一段话交给模型，只发一次回复。"""
        if not batch:
            return
        if len(batch) == 1:
            _on_msg_inner(batch[0], lst)        # 只有一条 → 完全等同老行为
            return
        msgs = sorted(batch, key=lambda m: float(m.get("timestamp") or 0))
        first, last = msgs[0], msgs[-1]
        last["_dsh_batch_n"] = len(msgs)
        bc = _batch_cfg()
        cap = int(bc.get("max_chars", 0) or 0)
        joined = []
        for m in msgs:
            t = str(m.get("content") or "").strip()
            if not t:
                continue
            if m.get("type") and m.get("type") != "文本":
                t = f"[{m.get('type')}]"
            joined.append(t)
        combined = "\n".join(joined)
        if cap > 0 and len(combined) > cap:
            combined = combined[:cap].rstrip() + "…"
        merged = dict(last, content=combined, is_self=False)
        # 上下文/图片沿用第一条那次算好的，别重复查库
        merged["_dsh_history"] = first.get("_dsh_history")
        merged["_dsh_payload"] = first.get("_dsh_payload")
        print(f"  · 合并 {len(msgs)} 条后统一生成回复（{why}）", flush=True)
        _on_msg_inner(merged, lst)

    def _on_msg_inner(msg: dict, _lst: Listener):
        payload = build_payload(db, resolver, msg)
        # 批量合并时，payload / history 已经算过一遍（§7.40）：复用，别重复判定/查库
        _ph = msg.get("_dsh_payload")
        if _ph:
            payload = dict(_ph)
        _batch_n = int(msg.get("_dsh_batch_n") or 0)
        if not payload["is_self"]:
            # 停机补投的旧消息只进日志、不进 GUI：重启后几十上百条旧消息会以「现在」
            # 的事件时间插到新消息后面，对话流看着就乱序（§7.27）。新鲜度判据与
            # policy.check 的 max_age_seconds 完全一致；_last_incoming 同理不刷新——
            # 几十分钟前的消息不代表「该会话正在聊」（§7.9 的图片闸门语义）。
            _fresh = (time.time() - float(payload.get("timestamp") or 0)
                      <= float(cfg["trigger"].get("max_age_seconds", 120) or 120))
            if _fresh:
                policy._last_incoming[payload["chat_username"]] = time.time()
                if _batch_n > 1:
                    BUS.event("recv", chat=payload["chat_name"], sender=payload["sender"],
                              text=f"（连发 {_batch_n} 条一起回）")
                else:
                    BUS.event("recv", chat=payload["chat_name"], sender=payload["sender"],
                              text=(payload["content"] or "")[:80])
                BUS.set_status(last_msg_time=time.time())
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
        history = msg.get("_dsh_history")
        if history is None:
            history = build_history(
                db, resolver, payload["chat_username"],
                int(cfg["ai"].get("history_limit", 8)), msg.get("local_id"),
                max_age_minutes=int(cfg["ai"].get("history_max_age_minutes", 0) or 0),
                max_tokens=int(cfg["ai"].get("history_max_tokens", 0) or 0),
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
                    # 写进日志：这些正是「该回却没回」的原因（冷却/上限/消息过旧/群里判定为否）
                    log_line(f"[{payload['time']}] 未回 {payload['chat_name']} | "
                             f"{payload['sender']}: {(payload['content'] or '')[:40]} "
                             f"| 原因：{reason}{extra}")
                return
            unsolicited = True
            log_line(f"[{payload['time']}] 主动接话 {payload['chat_name']} | "
                     f"{payload['sender']} 问：{payload['content']}")

        ms = dict(cfg["ai"].get("multi_send") or {})
        me_name = info.get("nick_name") or ""
        if _batch_n > 1:
            # 连发合并（§7.40）：一次回完对方连着发的几句，条数按合并后的算
            ms = dict(ms, max_messages=max(1, int(
                (_batch_cfg().get("max_messages") or 0) or ms.get("max_messages", 4))))
            log_line(f"[{payload['time']}] 连发 {_batch_n} 条 | "
                     f"{payload['sender']}: {payload['content']}")

        if unsolicited:
            reply = unsolicited_fill(cfg["ai"], me_name, payload["chat_name"],
                                     payload["sender"], payload["content"], history,
                                     build_mem_block(payload, history), api_key=api_key)
        else:
            log_line(f"[{payload['time']}] 收到 {payload['chat_name']} | "
                     f"{payload['sender']}: {payload['content']}")
            reply = ai_reply(cfg["ai"], payload["content"], payload["sender"], payload["chat_name"],
                             payload["is_group"], history, me_name=me_name,
                             mem_block=build_mem_block(payload, history), api_key=api_key,
                             images=[policy.pending_image] if policy.pending_image else None)
        if not reply:
            log_line("  AI 未生成回复，跳过")
            return
        # 2026-11-18：AI 自己判断这次不回（敏感话题 / 对方明说别回 / 错发广告…）。
        # 直接沉默——不发空消息、不写记忆、不打冷却（下次该回的时候不受影响）。
        if is_silence(reply) and bool((cfg["ai"].get("silence") or {}).get("enabled", True)):
            log_line(f"[{payload['time']}] 选择不回 {payload['chat_name']} | "
                     f"{payload['sender']}: {(payload['content'] or '')[:40]}"
                     f" | 原因：AI 判断这个话题不必接")
            BUS.event("system", chat=payload["chat_name"],
                      text=f"AI 选择不回（{payload['sender']}）：{(payload['content'] or '')[:40]}")
            return
        # 一句一条发（模式见 ai.multi_send）；想整段一条就把 enabled 关掉
        texts = split_for_send(reply, ms)
        policy.mark(payload["chat_username"], policy.sender_key(payload))
        if unsolicited:
            uns.mark(payload["chat_username"])
        if send_multi(texts, payload["chat_username"], payload["chat_name"],
                      names=_disp_names(payload))[0]:
            BUS.set_status(reply_count_today=BUS.status().get("reply_count_today", 0) + len(texts))
            # 异步更新记忆档案（不阻塞、不影响回复延迟）；群聊按 memory.group_enabled 拦掉
            _mcfg2 = cfg["ai"].get("memory", {}) or {}
            if bool(_mcfg2.get("enabled", True)) and (
                    bool(_mcfg2.get("group_enabled", True)) or not payload["is_group"]):
                lines = list(history or [])
                lines.append(f"{payload['sender']}: {payload['content']}")
                lines.append("我: " + " ".join(texts))
                threading.Thread(
                    target=memory_store.update,
                    args=(payload["chat_username"], payload["chat_name"], lines,
                          cfg["ai"], load_api_key(), cfg["ai"].get("memory", {})),
                    daemon=True,
                ).start()
                # 同一段对话也写进长期向量库（默认 infer=false：只做本地嵌入，不再调模型）
                if memory_vec.enabled(cfg["ai"]):
                    threading.Thread(
                        target=memory_vec.remember,
                        args=(cfg["ai"], payload["chat_username"], payload["chat_name"], lines),
                        daemon=True,
                    ).start()

    lst.add_all(on_msg, discover=True)
    lst.start()

    # -- 主动开场（proactive）：会话安静够久，就按人设主动找配置里的人说句话 ----
    # 触发条件（全部满足才发）：非免打扰时段 + 启动冷静期已过 + 距上次主动 >= interval_minutes
    # + 该会话距最后一次互动 >= min_quiet_minutes + 当日主动次数 < max_per_day + 账号在线。
    proactive_cfg = dict(cfg.get("proactive") or {})
    _pro_state = {"day": time.strftime("%Y-%m-%d"), "count": 0}
    _pro_last_sent: dict = {}
    _pro_started = time.time()

    def _in_quiet_hours(spec) -> bool:
        """quiet_hours: [开始小时, 结束小时)，支持跨午夜，如 [1, 8] = 01:00-08:00 不打扰。"""
        if not spec or len(spec) != 2:
            return False
        h = time.localtime().tm_hour
        a, b = int(spec[0]) % 24, int(spec[1]) % 24
        if a == b:
            return False
        return (a <= h < b) if a < b else (h >= a or h < b)

    def _proactive_fill(me_name: str, chat_name: str, history, mem_block: str) -> str:
        """没人发消息时，按人设主动起个头的开场白（只生成不发送）。"""
        aicfg = dict(cfg["ai"])
        ident = ""
        if me_name:
            ident = (f"你的微信昵称显示是「{me_name}」，别人叫那个名字也是在叫你；"
                     f"按人设用自己的口吻说话，别自称那个昵称。\n\n")
        ctx = ""
        if history:
            ctx = "最近的聊天记录：\n" + "\n".join(history) + "\n\n"
        user_prompt = (
            ident
            + (mem_block.strip() + "\n\n" if mem_block else "")
            + ctx
            + f"现在没有任何人给你发消息，你想主动找「{chat_name}」说句话。"
              "按你的性格自然地起个头：分享一件小事、问一句他的近况、接上之前聊过的话题都行。"
              "一到两句话就够，别像群发、别问「在吗」、别用「名字：」开头，"
              "也不要解释你是主动发的。"
        )
        text = _chat_call(aicfg, aicfg["system_prompt"], user_prompt,
                          thinking=bool(aicfg.get("thinking", False)),
                          max_tokens=int(aicfg.get("max_tokens", 1200) or 1200),
                          timeout=float(aicfg.get("timeout", 30) or 30),
                          temperature=float(aicfg.get("temperature", 1.2) or 1.2),
                          reasoning_effort=aicfg.get("reasoning_effort", "low"),
                          api_key=api_key)
        if not text:
            return ""
        text = text.strip("「」\"'“”").strip().rstrip("。.").rstrip()
        limit = int(aicfg.get("max_chars", 220) or 0)
        if limit > 0:
            text = text[:limit].rstrip()
        return text

    def _proactive_worker():
        while True:
            try:
                time.sleep(60)
                if not proactive_cfg.get("enabled"):
                    continue
                now = time.time()
                if now - _pro_started < float(proactive_cfg.get("grace_minutes", 15) or 0) * 60:
                    continue
                today = time.strftime("%Y-%m-%d")
                if today != _pro_state["day"]:
                    _pro_state["day"], _pro_state["count"] = today, 0
                if _pro_state["count"] >= int(proactive_cfg.get("max_per_day", 4) or 0):
                    continue
                if _in_quiet_hours(proactive_cfg.get("quiet_hours")):
                    continue
                if not (BUS.status() or {}).get("online", True):
                    continue
                for t in proactive_cfg.get("targets") or []:
                    wxid = str(t.get("wxid") or "").strip()
                    name = str(t.get("name") or wxid)
                    if not wxid:
                        continue
                    if now - float(_pro_last_sent.get(wxid, 0)) \
                            < float(proactive_cfg.get("interval_minutes", 180) or 0) * 60:
                        continue
                    # 最近一次会话活动（对方发的或自己发的都算「正在聊」）
                    last_act = float(policy._last_incoming.get(wxid, 0) or 0)
                    try:
                        _m = db.get_messages(wxid, limit=1)
                        if _m:
                            last_act = max(last_act, float(_m[0].get("create_time") or 0))
                    except Exception:  # noqa: BLE001
                        pass
                    if now - last_act < float(proactive_cfg.get("min_quiet_minutes", 60) or 0) * 60:
                        continue
                    history = build_history(
                        db, resolver, wxid, int(cfg["ai"].get("history_limit", 8)),
                        max_age_minutes=int(cfg["ai"].get("history_max_age_minutes", 0) or 0),
                        max_tokens=int(cfg["ai"].get("history_max_tokens", 0) or 0))
                    me_name = info.get("nick_name") or ""
                    reply = _proactive_fill(
                        me_name, name, history,
                        build_mem_block({"is_group": False, "chat_username": wxid,
                                         "chat_name": name, "content": ""}, history))
                    if not reply:
                        continue
                    texts = split_for_send(reply, dict(cfg["ai"].get("multi_send") or {}))
                    ok, _n = send_multi(texts, wxid, name)
                    _pro_last_sent[wxid] = time.time()
                    if ok:
                        _pro_state["count"] += 1
                        log_line(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 主动开场 → {name}: {reply}")
                        BUS.event("system", text=f"主动开场 → {name}: {reply[:60]}")
                        BUS.set_status(reply_count_today=(BUS.status() or {}).get("reply_count_today", 0) + len(texts))
            except Exception as e:  # noqa: BLE001
                log_line(f"[主动开场异常] {type(e).__name__}: {e}")

    if proactive_cfg.get("enabled"):
        threading.Thread(target=_proactive_worker, daemon=True).start()
        _tg = "、".join(str(t.get("name") or t.get("wxid"))
                       for t in (proactive_cfg.get("targets") or []))
        print(f"主动开场：已启用 → {_tg}"
              f"（间隔 {proactive_cfg.get('interval_minutes')} 分钟 / "
              f"安静 {proactive_cfg.get('min_quiet_minutes')} 分钟 / "
              f"每日上限 {proactive_cfg.get('max_per_day')} 次 / "
              f"免打扰 {proactive_cfg.get('quiet_hours')}）", flush=True)
    else:
        print("主动开场：关（config.json → proactive.enabled；只回消息，不自己找话题）", flush=True)

    print("机器人运行中（Ctrl+C 停止）...", flush=True)
    last_check = time.time()
    unknown_streak = 0          # 账号状态连续读不到的次数（连续够 GUARD_UNKNOWN_STREAK 才告警）
    unknown_warned = False      # 当前这轮 streak 是否已经告过警（避免每次都重复刷日志）
    try:
        while True:
            time.sleep(1)
            # 换号守卫：多账号时上游可能读到另一个号的库，self_id 会错位 → 会回复自己。
            # 一旦发现账号变了，立刻停手，宁可不出声也不误发。
            if time.time() - last_check >= 60:
                last_check = time.time()
                state = account_guard(db, lock_wxid)
                if state == "switched":
                    BUS.set_status(online=False, running=False)
                    BUS.event("warn", text="检测到微信账号已变化，机器人已停止")
                    log_line(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠ 检测到微信登录账号已变化"
                             f"（启动时 {lock_nick}/{lock_wxid}）→ 立刻停止，避免认错人、回复自己")
                    print("\n[!] 登录账号变了，机器人已退出。请重新双击 restart_bot.bat 启动。")
                    break
                if state == "unknown":
                    unknown_streak += 1
                    if unknown_streak >= GUARD_UNKNOWN_STREAK:
                        BUS.set_status(online=False)
                        if not unknown_warned:
                            unknown_warned = True
                            BUS.event("warn", text="账号状态连续读不到，正在持续观察")
                            # 措辞：这不是「微信掉线」，而是「机器人这次没读到账号状态」——
                            # 常见原因是连接偶发抖动，通常会自愈；连续多次才值得提醒。
                            log_line(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠ 账号状态连续"
                                     f"{unknown_streak} 次读不到（连接偶发抖动或多账号数据异常），"
                                     f"持续观察中；微信掉线可能性低")
                else:
                    # 一旦读到一次就重置 streak 并撤掉告警状态 —— 证明抖动已过去
                    if unknown_streak and unknown_warned:
                        BUS.event("system", text="账号状态已恢复读取")
                        log_line(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✓ 账号状态已恢复读取"
                                 f"（此前连续 {unknown_streak} 次读不到，判定为偶发抖动）")
                    unknown_streak = 0
                    unknown_warned = False
                    BUS.set_status(online=True)
    except KeyboardInterrupt:
        pass
    finally:
        lst.stop()
        BUS.set_status(running=False)
        BUS.event("system", text="机器人已停止")
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
