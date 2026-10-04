# -*- coding: utf-8 -*-
"""联系人记忆：给每个会话建一份小档案，回复时注入，事后异步更新。

存储：memory/<wxid>.json
  {"username": "...", "chat_name": "...", "facts": ["..."], "updated": 1699...}

设计要点：
* 只记「对方」的稳定信息（关系、称呼、城市、工作、爱好、约定、近况），不记主人的隐私；
* 更新走一次**非思考模式**的小调用（便宜、快），失败就静默保留旧档案；
* 条数上限可配，超了就丢最旧的——防止档案无限膨胀把 prompt 撑大。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEM_DIR = os.path.join(BASE_DIR, "memory")

EXTRACT_PROMPT = """你在给微信联系人维护一份简短档案。根据「已有档案」和「最新对话」，输出更新后的档案。

规则：
1. 只记关于**对方**的稳定、以后用得上的信息：称呼/关系、所在城市、职业、爱好、正在忙的事、重要约定、明显偏好、情绪近况；
2. **不要记录主人的隐私**（真实姓名、住址、单位、账号、证件），也不要记无意义寒暄；
3. 新信息与旧条目冲突时，用新的替换旧的，不要两条都留；
4. 合并同类项，越精简越好，每条不超过 {max_chars} 字，总数不超过 {max_facts} 条；
5. 如果这段对话没有任何值得记的新信息，就原样返回已有档案。

只输出 JSON，格式：{{"facts": ["条目1", "条目2"]}}"""


def _safe_name(username: str) -> str:
    return re.sub(r"[^0-9A-Za-z_@\-]", "_", username or "unknown")[:80]


def path_for(username: str) -> str:
    return os.path.join(MEM_DIR, _safe_name(username) + ".json")


def load(username: str) -> dict:
    try:
        with open(path_for(username), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("facts"), list):
            return d
    except Exception:
        pass
    return {"username": username, "chat_name": "", "facts": [], "updated": 0}


def save(username: str, chat_name: str, facts: list) -> None:
    os.makedirs(MEM_DIR, exist_ok=True)
    data = {
        "username": username,
        "chat_name": chat_name,
        "facts": [str(x).strip() for x in facts if str(x).strip()][:80],
        "updated": time.time(),
    }
    tmp = path_for(username) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path_for(username))


def prompt_block(mem: dict) -> str:
    """把档案格式化成注入 prompt 的一小段。"""
    facts = mem.get("facts") or []
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts[:40])
    return f"\n\n【关于对方的已知档案（供参考，不要直接念出来）】\n{lines}"


def _chat(cfg_ai: dict, messages: list, key: str, max_tokens: int = 400) -> str:
    body = {
        "model": cfg_ai.get("model", "deepseek-flash"),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": False,
        "thinking": {"type": "disabled"},   # 抽取信息不需要思考，省钱省时间
    }
    req = urllib.request.Request(
        cfg_ai["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg_ai.get("timeout", 30)) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data["choices"][0]["message"].get("content") or "").strip()


def update(username: str, chat_name: str, recent_lines: list, cfg_ai: dict,
           key: str, mem_cfg: dict) -> list:
    """用一次小调用更新档案，返回新的 facts。任何失败都不影响主流程。"""
    if not mem_cfg.get("enabled", True) or not key or not recent_lines:
        return load(username).get("facts", [])

    mem = load(username)
    old = mem.get("facts") or []
    sys_prompt = EXTRACT_PROMPT.format(
        max_chars=int(mem_cfg.get("max_fact_chars", 36)),
        max_facts=int(mem_cfg.get("max_facts", 25)),
    )
    user = ("【已有档案】\n" + ("\n".join(f"- {f}" for f in old) if old else "（空）")
            + "\n\n【最新对话】\n" + "\n".join(recent_lines[-12:]))
    try:
        text = _chat(cfg_ai, [{"role": "system", "content": sys_prompt},
                              {"role": "user", "content": user}], key)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return old
        facts = json.loads(m.group(0)).get("facts") or []
        facts = [str(x).strip() for x in facts if str(x).strip()]
        if not facts:
            return old
        facts = facts[: int(mem_cfg.get("max_facts", 25))]
        save(username, chat_name, facts)
        return facts
    except Exception as e:  # noqa: BLE001
        print(f"  [记忆更新失败] {type(e).__name__}: {e}", flush=True)
        return old
