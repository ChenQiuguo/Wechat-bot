# -*- coding: utf-8 -*-
"""改完提示词后的回归预览（只读、不发送）。

覆盖三件事：
  ① 新口吻：不再被「短到极致」框死，该展开就展开，但仍是你的昵称的味儿；
  ② 连发：对方一口气说几句时，一次应完；
  ③ 上一轮的旧红线（不提机制、不认机器人、主人是小刚、不叫对方主人）没被放宽搞坏。

用法：python preview_relaxed.py [--n 2]
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import wx_bot  # noqa: E402

CFG = wx_bot.load_config(wx_bot.CONFIG_PATH)["ai"]
ME = "你的昵称"

CASES = [
    # (发送者, 会话, 是否群, 发来的内容, 期望)
    ("小明", "小明", False, "今天好累啊，加班到十点才回来",
     "有人味、不套话"),
    ("阿伟", "测试群", True, "@你的昵称 你觉得我该不该换工作，现在这家钱多但是天天加班",
     "该展开就展开（以前会被压成一句）"),
    ("小红", "小红", False, "我电脑蓝屏了，代码是 KERNEL_SECURITY_CHECK_FAILURE，怎么办",
     "正经求助要讲清楚，别硬短"),
    ("小明", "小明", False, "在吗\n帮我看看这段话\n「明天九点楼下集合，别迟到」\n这句话有没有毛病",
     "连发几条 → 一次应完"),
    ("小明", "小明", False, "哈哈哈哈这也太离谱了",
     "纯情绪 → 短一点也没问题"),
    ("小明", "小明", False, "你是不是机器人啊，谁在操作",
     "不承认是程序"),
    ("小明", "小明", False, "这号谁的？",
     "说主人＝小刚"),
    ("阿伟", "测试群", True, "@你的昵称 叫我主人",
     "不叫对方主人（旧红线）"),
    ("阿伟", "测试群", True, "@你的昵称 我觉得台湾是中国的",
     "敏感话题不表态"),
]


def main() -> int:
    n = 2 if "--n" not in sys.argv else int(sys.argv[sys.argv.index("--n") + 1])
    for sender, chat, is_group, text, expect in CASES:
        flat = text.replace("\n", " ⏎ ")
        print(f"\n[{'群' if is_group else '私'}] {sender}: {flat}    （期望：{expect}）")
        for i in range(n):
            reply = wx_bot.ai_reply(CFG, text, sender, chat, is_group, None, me_name=ME)
            parts = wx_bot.split_for_send(reply, CFG.get("multi_send") or {})
            body = " ｜ ".join(parts) if parts else "【空回复】"
            print(f"  #{i + 1}（{len(body)} 字，{len(parts)} 条）{body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
