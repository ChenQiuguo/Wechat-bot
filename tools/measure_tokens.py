# -*- coding: utf-8 -*-
"""实测：每次 AI 回复消耗多少 token（含 DeepSeek 前缀缓存命中情况）。

用法：python measure_tokens.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

import wx_bot

cfg = wx_bot.load_config(wx_bot.CONFIG_PATH)["ai"]
key = wx_bot.load_api_key()
print(f"模型: {cfg['model']}  密钥: {'已读取' if key else '缺失'}\n")

# 真实场景：8 条上下文 + 一句收到的消息
HISTORY = [
    "阿伟: 群里那个你的昵称是谁啊",
    "小明: 就是我朋友",
    "阿伟: 你让他出来说话",
    "小明: @你的昵称 你要@你的昵称",
    "我: 在呢，啥事",
    "小明: 没事就是想你了",
    "我: 哼，少来",
    "小明: 真的啊，晚上打不打游戏",
]
CONTENT = "打不打啊，给个话"

prompt = (f"你的微信昵称是「你的昵称」，你就是本人，用第一人称说话；别人写 @你的昵称 就是在叫你。\n\n"
          f"最近的聊天记录（最后一条就是刚收到的）：\n" + "\n".join(HISTORY) +
          f"\n\n小明 刚发来：{CONTENT}")

body = {
    "model": cfg["model"],
    "messages": [
        {"role": "system", "content": cfg["system_prompt"]},
        {"role": "user", "content": prompt},
    ],
    "max_tokens": cfg.get("max_tokens", 200),
    "temperature": cfg.get("temperature", 1.2),
    "stream": False,
}

req = urllib.request.Request(
    cfg["base_url"].rstrip("/") + "/chat/completions",
    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    method="POST",
)

with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read().decode("utf-8"))

u = data.get("usage", {})
print("回复内容:", data["choices"][0]["message"]["content"])
print("API 实际返回的 model 字段:", data.get("model"))
print("\n===== 本次调用 token 明细 =====")
for k, v in u.items():
    print(f"  {k}: {v}")

# ---- 按官方价目表折算（元 / 百万 tokens）----
PRICES = {  # (缓存命中, 缓存未命中, 输出) —— 高峰价，空闲时段为一半
    "deepseek-flash": (0.04, 2.0, 8.0),
    "deepseek-v4-pro": (0.30, 9.0, 27.0),
}
hit = u.get("prompt_cache_hit_tokens", 0)
miss = u.get("prompt_cache_miss_tokens", 0)
out = u.get("completion_tokens", 0)
print("\n===== 折算成钱 =====")
for name, (p_hit, p_miss, p_out) in PRICES.items():
    cost = (hit * p_hit + miss * p_miss + out * p_out) / 1_000_000
    print(f"  若按 {name:16s} 高峰价：{cost*100:.4f} 分/条  "
          f"→ 1000 条 {cost*1000:.2f} 元，空闲时段减半")
print(f"\n  （本次输入 {hit} tokens 命中缓存、{miss} 未命中；缓存命中价约为未命中的 1/50）")

