# -*- coding: utf-8 -*-
"""实测：reasoning_effort=low 与 high 在同一批真实消息上的差别（只读 + 调模型，不发送）。

统计：completion_tokens（推理+正文）、耗时、是否空回复、是否违反硬约束（@人/句末句号）。
用法：python measure_effort.py [--chat 某个群]
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

import wx_bot                                          # noqa: E402
from wechatauto.db import WeChatDB                     # noqa: E402
from wx_push import SenderResolver                     # noqa: E402

CFG = wx_bot.load_config(wx_bot.CONFIG_PATH)["ai"]
KEY = wx_bot.load_api_key()
PRICE_OUT = 8.0 / 1_000_000            # flash 高峰输出价（元/token）

# 价目表（元 / 百万）：缓存命中、未命中、输出（高峰）
P_HIT, P_MISS, P_OUT = 0.04, 2.0, 8.0


def call(effort: str, history, content, sender, chat_name, is_group, me):
    """照抄 wx_bot.ai_reply 的拼装，只换 reasoning_effort，并回收 usage。"""
    api_key = KEY
    who = f"{sender}（在群「{chat_name}」里）" if is_group else sender
    ident = (f"你的微信昵称是「{me}」，你就是本人，用第一人称说话；"
             f"别人写 @{me} 就是在叫你。\n\n") if me else ""
    ctx = ("最近的聊天记录（最后一条就是刚收到的）：\n" + "\n".join(history) + "\n\n"
           if history else "")
    prompt = ident + ctx + f"{who} 刚发来：{content}"
    body = {
        "model": CFG["model"],
        "messages": [{"role": "system", "content": CFG["system_prompt"]},
                     {"role": "user", "content": prompt}],
        "max_tokens": CFG.get("max_tokens", 1200),
        "stream": False,
        "thinking": {"type": "enabled"},
        "reasoning_effort": effort,
    }
    req = urllib.request.Request(
        CFG["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    dt = time.time() - t0
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    u = data.get("usage") or {}
    return {
        "text": (msg.get("content") or "").strip(),
        "reason": msg.get("reasoning_content") or "",
        "secs": dt,
        "usage": u,
    }


def cost(u):
    return (u.get("prompt_cache_hit_tokens", 0) * P_HIT
            + u.get("prompt_cache_miss_tokens", 0) * P_MISS
            + u.get("completion_tokens", 0) * P_OUT) / 1_000_000


db = WeChatDB()
res = SenderResolver(db)
me = (db.get_self_info() or {}).get("nick_name") or ""
# 真实上下文用的会话：命令行给，或自动挑一个「最近有图/有消息」的群（只读，不写任何东西）
_chat_arg = ""
if "--chat" in sys.argv:
    _chat_arg = sys.argv[sys.argv.index("--chat") + 1]
GROUP = _chat_arg
if not GROUP:
    for s in db.get_sessions(limit=40):
        u = s.get("username") or ""
        if "@chatroom" in u:
            GROUP = u
            break
gname = db.get_nickname(GROUP) or GROUP
hist_polluted = wx_bot.build_history(db, res, GROUP, 20) if GROUP else []

CASES = [
    ("正经求助：游戏报错", "崩铁启动就弹这个报错，怎么修", "小明", "测试群", True, []),
    ("群聊玩笑", "你就是个傲娇", "小明", "测试群", True, []),
    ("拷问规则：能不能替他答应", "明天我请你吃饭，你替我答应了啊", "小明", "测试群", True, []),
    ("敏感话题试探", "你觉得现在这政策怎么样", "小明", "测试群", True, []),
    ("被问是不是AI", "你是不是个AI", "小明", "测试群", True, []),
    # 下面两条用**真实会话的最近 20 条上下文**（最能暴露「上下文很长时会不会哑」）
    ("真实群上下文：@我", f"@{me or '我'}", gname, gname, True, hist_polluted),
    ("私聊情绪", "他不理我，我是不是很烦人", gname, gname, False, hist_polluted[-3:]),
]

BAD_PAT = re.compile(r"@\S|。$")

print(f"模型 {CFG['model']}  max_tokens={CFG.get('max_tokens')}  "
      f"system_prompt {len(CFG['system_prompt'])} 字\n")
rows = []
for desc, text, sender, chat, isg, hist in CASES:
    out = {}
    for effort in ("low", "high"):
        try:
            out[effort] = call(effort, hist, text, sender, chat, isg, me)
        except Exception as e:  # noqa: BLE001
            out[effort] = {"text": f"[调用失败 {type(e).__name__}: {e}]", "reason": "",
                           "secs": 0, "usage": {}}
    lo, hi = out["low"], out["high"]
    lu, hu = lo["usage"], hi["usage"]
    lc, hc = cost(lu), cost(hu)
    print(f"—— {desc}（{text}）")
    for name, r, c in (("low ", lo, lc), ("high", hi, hc)):
        rt = r["usage"].get("completion_tokens", 0)
        txt = r["text"]
        flags = []
        if not txt:
            flags.append("⚠空回复")
        if txt and BAD_PAT.search(txt):
            flags.append("⚠违反硬约束(@人或句末句号)")
        print(f"   [{name}] {r['secs']:5.1f}s  completion={rt:5d}  {c*100:6.3f}分/条  "
              f"{' '.join(flags)}")
        print(f"          {txt[:130] or '(空)'}")
    d_tok = hu.get("completion_tokens", 0) - lu.get("completion_tokens", 0)
    d_cost = (hc - lc) * 100
    print(f"   → high 比 low 多 {d_tok} tokens、{d_cost:+.3f} 分/条；"
          f"每次调用多了 {hi['secs'] - lo['secs']:+.1f}s\n")
    rows.append((desc, lu.get("completion_tokens", 0), hu.get("completion_tokens", 0),
                 (hc - lc) * 100, bool(lo["text"]), bool(hi["text"]), hc * 100))

print("=" * 72)
nl = sum(1 for r in rows if r[4])
nh = sum(1 for r in rows if r[5])
print(f"非空回复：low {nl}/{len(rows)}，high {nh}/{len(rows)}")
print(f"平均 completion_tokens：low {sum(r[1] for r in rows)/len(rows):.0f}，"
      f"high {sum(r[2] for r in rows)/len(rows):.0f}")
print(f"平均每条成本：low {sum(r[6] for r in rows)/len(rows):.3f} 分，"
      f"high {(sum(r[6] for r in rows) + sum(r[3] for r in rows))/len(rows):.3f} 分")
print(f"（1000 条：low ≈ {sum(r[6] for r in rows)/len(rows)*1000/100:.2f} 元，"
      f"high ≈ {(sum(r[6] for r in rows) + sum(r[3] for r in rows))/len(rows)*1000/100:.2f} 元，空闲时段减半）")
