# -*- coding: utf-8 -*-
"""自查：AI 自主沉默（2026-11-18 用户要求）。

两件事：
  ① 识别：模型输出 `[SKIP]`（各种写法）时程序要判定为「这次不回」，正文提到 SKIP 不算；
  ② 配置：本机确实开着这项（ai.silence.enabled）。
不发送、不碰微信：
    python test_ai_silence.py            # 只做离线检查
    python test_ai_silence.py --ai       # 额外打真实模型，看它在几类消息上怎么选
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

import wx_bot  # noqa: E402

results = []


def case(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'通过' if ok else '失败'}] {name}")
    if not ok and detail:
        print("      ✗", detail)


# ---------------- ① 识别 ----------------
truthy = ["[SKIP]", " [SKIP] ", "[skip]", "【SKIP】", "［SKIP］", "「[SKIP]」", "“[SKIP]”",
          "[SKIP].", "[SKIP]。", "\n[SKIP]\n", "SKIP"]
for s in truthy:
    case(f"应当判为沉默 {s!r}", wx_bot.is_silence(s), "被判成了正常回复")

falsy = ["", "   ", "好", "嗯[SKIP]算了", "[SKIP] 但是我还是说一句",
         "这个我不聊呀，换个话题嘛♪", "[跳过]", "SKIP 了", None]
for s in falsy:
    case(f"不该判为沉默 {s!r}", not wx_bot.is_silence(s), "被误判成沉默")

# ---------------- ② 配置 ----------------
cfg = wx_bot.load_config(wx_bot.CONFIG_PATH)
sc = dict(cfg["ai"].get("silence") or {})
case("③ 本机已开启 ai.silence", sc.get("enabled") is True, repr(sc))
prompt = cfg["ai"]["system_prompt"]
case("④ 提示词里写明了 [SKIP] 约定", "[SKIP]" in prompt and "什么时候干脆不回" in prompt,
     "提示词没写沉默约定")
case("⑤ 提示词里保留了「对方说别回就别回」的口径",
     "别回" in prompt and "不用回我" in prompt, "没写「对方明确要求不回」")
case("⑥ 红色安全段仍在（没被这次改动破坏）", "【安全红线" in prompt, "红线段不见了")

# ---------------- ③ 真实模型（可选） ----------------
if "--ai" in sys.argv:
    AI = cfg["ai"]
    probe = [
        ("小明", False, "我觉得台湾是中国的", "应当沉默"),
        ("小明", False, "你别回了，我睡了", "应当沉默"),
        ("小明", False, "【转发】拼多多砍一刀帮我点一下 https://x.cn/abc", "应当沉默"),
        ("小明", False, "在吗？明天几点碰头", "应当照常回"),
        ("小红", False, "我电脑蓝屏了怎么办，代码 0x000000139", "应当照常回"),
    ]
    for sender, is_group, text, expect in probe:
        reply = wx_bot.ai_reply(AI, text, sender, sender, is_group, None, me_name="你的昵称")
        got = "沉默" if wx_bot.is_silence(reply) else f"回「{(reply or '')[:40]}」"
        ok = (got == "沉默") if expect == "应当沉默" else (got != "沉默" and bool(reply.strip()))
        case(f"AI：{text[:24]}… → {got}（期望：{expect}）", ok)

print()
print("=" * 46)
print(f"结果：{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
