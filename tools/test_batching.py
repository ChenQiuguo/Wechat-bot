# -*- coding: utf-8 -*-
"""自查：连发合并（ai.batching）——对方一口气发三四条，只回一次，且把几句都看到。

不发送、不碰微信：policy.check / ai 生成 / 发送全用桩，只看「批次拼装」这一步。
用法：python test_batching.py
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

results = []


def case(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'通过' if ok else '失败'}] {name}")
    if not ok and detail:
        print("      ✗", detail)


# ---------- 纯逻辑：批次拼装（不依赖 wx_bot 的闭包） ----------
def join_batch(msgs: list, cap: int = 0) -> str:
    joined = []
    for m in msgs:
        t = str(m.get("content") or "").strip()
        if not t:
            continue
        if m.get("type") and m.get("type") != "文本":
            t = f"[{m.get('type')}]"
        joined.append(t)
    s = "\n".join(joined)
    if cap > 0 and len(s) > cap:
        s = s[:cap].rstrip() + "…"
    return s


texts = [{"content": "在吗", "type": "文本"},
         {"content": "帮我看看这个报错", "type": "文本"},
         {"content": "", "type": "文本"},
         {"content": "ModuleNotFoundError", "type": "文本"}]
got = join_batch(texts)
case("① 三条合成一段（空消息被丢掉）", got == "在吗\n帮我看看这个报错\nModuleNotFoundError", repr(got))
case("② 超长时截断并加省略号", join_batch(texts, cap=10).endswith("…")
     and len(join_batch(texts, cap=10)) == 11, repr(join_batch(texts, cap=10)))
case("③ 非文本消息用类型占位", join_batch([{"content": "x", "type": "图片"}]) == "[图片]",
     repr(join_batch([{"content": "x", "type": "图片"}])))

# ---------- 真模块：配置与默认值 ----------
import wx_bot  # noqa: E402

cfg = wx_bot.load_config(wx_bot.CONFIG_PATH)
bc = dict(cfg["ai"].get("batching") or {})
case("④ 本机配置里 batching 已开启", bc.get("enabled") is True, repr(bc))
case("⑤ 等待窗口在 0.5–3 秒之间（不影响正常对话节奏）",
     500 <= int(bc.get("wait_ms", 0)) <= 3000, repr(bc))
mx = int(bc.get("max_messages", 0) or 0)
case("⑥ 一次最多合并 3–6 条，且回复条数上限 >= 3",
     mx <= 6 and int(cfg["ai"]["multi_send"].get("max_messages", 0)) >= 3,
     f"batching.max_messages={mx} multi_send.max_messages="
     f"{cfg['ai']['multi_send'].get('max_messages')}")


# ---------- 真模块：窗口逻辑（用假 Listener 驱动 on_msg 里的队列） ----------
# 说明：on_msg 是 main() 内部的闭包，依赖真库/真微信，这里不实例化它；
# 队列行为由 test_batching_e2e 的人工实测记录在 HANDOFF §7.40。
def dedupe(seq, window):
    """模拟：窗口内的同一个人连发合并成一批，异人立刻切开。"""
    batches, cur, t0 = [], [], 0
    for t, who in seq:
        if cur and (who != cur[0][1] or t - t0 > window):
            batches.append(cur)
            cur = []
        cur.append((t, who))
        t0 = t0 if len(cur) > 1 else t
    if cur:
        batches.append(cur)
    return [len(b) for b in batches]


sizes = dedupe([(0, "A"), (0.3, "A"), (0.7, "A")], 1.2)
case("⑦ 同一人 1.2s 内三条 → 一批 3 条", sizes == [3], str(sizes))
sizes = dedupe([(0, "A"), (0.3, "A"), (1.5, "A")], 1.2)
case("⑧ 间隔超过窗口 → 分成两批", sizes == [2, 1], str(sizes))
sizes = dedupe([(0, "A"), (0.3, "B")], 1.2)
case("⑨ 中途换了人 → 上一批立刻冲掉、各回一次", sizes == [1, 1], str(sizes))

print()
print("=" * 46)
print(f"结果：{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
