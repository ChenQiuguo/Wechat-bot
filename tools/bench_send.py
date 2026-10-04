# -*- coding: utf-8 -*-
"""基准测试：搜索框路径 vs UIA 会话列表点击路径。

每轮先切到别的会话，保证目标会话处于「未打开」状态，避免快路径干扰。
用法：python bench_send.py [轮数]
"""
from __future__ import annotations

import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

os.environ.setdefault("WECHATAUTO_RHYTHM", "fast")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

from wechatauto.guia import WeChatGUI
from wx_bot import open_chat_via_session_list

TARGET = "文件传输助手"
RESET = "微信团队"


def ok_of(resp) -> bool:
    ok = bool(getattr(resp, "is_success", None))
    if not ok and isinstance(resp, dict):
        ok = resp.get("status") == "成功"
    return ok


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    gui = WeChatGUI()
    gui.ensure_visible()
    print(f"目标={TARGET}  重置会话={RESET}\n", flush=True)

    results = {"搜索": [], "列表": []}
    for i in range(rounds):
        for label in ("搜索", "列表"):
            open_chat_via_session_list(gui, RESET)   # 重置：确保目标未打开
            time.sleep(0.8)

            text = f"{label}测试{i}"
            t = time.time()
            if label == "搜索":
                resp = gui.send_msg(text, TARGET, False)
                detail = ""
            else:
                opened = open_chat_via_session_list(gui, TARGET)
                t_open = time.time() - t
                resp = gui.send_msg(text, None, False) if opened else gui.send_msg(text, TARGET, False)
                detail = f"打开={'OK' if opened else '失败'}({t_open:.1f}s)"
            dt = time.time() - t
            results[label].append(dt)
            print(f"[{i}] {label:<3} {dt:5.1f}s  {'成功' if ok_of(resp) else '失败'}  {detail}", flush=True)
            time.sleep(1.0)

    print("\n===== 汇总 =====")
    for k, v in results.items():
        print(f"{k}路径: 平均 {sum(v)/len(v):.1f}s   明细 {[round(x,1) for x in v]}")


if __name__ == "__main__":
    main()
