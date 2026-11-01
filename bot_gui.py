# -*- coding: utf-8 -*-
"""微信 AI 机器人图形化监控面板。

纯 Python 标准库 Tkinter 实现，零额外依赖。
- 状态区：微信在线 / 机器人运行 / 账号 / 模式 / 今日回复数 / 最后消息时间
- 对话区：实时滚动显示收发消息，按会话分色
- 系统日志区：收发/警告/错误/启动等系统事件
- 顶部工具栏：启动/停止/重启机器人、打开日志、打开配置

用法：
    python bot_gui.py             # 打开 GUI（会自动启动机器人子进程）
    python bot_gui.py --attach    # 只打开 GUI，不自动启动机器人（连已有实例）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot_state import BotStateReader  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
BOT_SCRIPT = os.path.join(BASE_DIR, "wx_bot.py")
LOG_PATH = os.path.join(BASE_DIR, "bot_replies.log")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOCK_PATH = os.path.join(BASE_DIR, "bot.lock")

# ---- 配色（整体粉色调：粉白基底 + 樱粉强调 + 亮粉点缀，参考用户给的 #F77EA9）----
BG = "#FFFAFC"          # 粉白（页面基底，60%+ 占比）
BG2 = "#FDE8EF"         # 浅樱粉（卡片/工具栏/状态栏）
FG = "#8A5A6B"          # 粉棕文字（粉底上柔和可读）
FG_DIM = "#D4A5B5"      # 浅粉灰（次要信息）
ACCENT = "#F77EA9"      # 亮粉（主视觉：主按钮/标题/高亮，用户指定）
OK = "#E88AAE"          # 樱粉（在线/成功）
WARN = "#F9C6D8"        # 浅樱粉（警告）
ERR = "#E0527F"         # 深亮粉（错误/停止，避免刺眼）
RECV_BG = "#FFE4EF"     # 淡粉（接收消息背景，浅化 #FFC0D9）
SEND_BG = "#D6EDF8"     # 淡蓝（发送消息背景，浅化 #85C6EB）
SYS_BG = "#FFF5F8"      # 粉白（系统消息背景）

# 主题彩虹色（亮粉 + 樱粉 + 浅粉，粉色层次）
CHAT_COLORS = [
    "#F77EA9",  # 亮粉（主视觉，用户指定）
    "#E88AAE",  # 樱粉
    "#F9C6D8",  # 浅樱粉
    "#F4A7C0",  # 中粉
    "#FADCE7",  # 极浅粉
    "#D4738F",  # 深粉
    "#F77EA9"   # 亮粉
]

# ♪ + ♡ 装饰符号颜色
NOTE_COLOR = "#F77EA9"
HEART_COLOR = "#E88AAE"


class BotGUI:
    def __init__(self, root: tk.Tk, auto_start: bool = True):
        self.root = root
        self.bot_proc: subprocess.Popen | None = None
        self._events_buffer = []
        self._last_status_pull = 0.0
        self._reader = BotStateReader()

        self._setup_window()
        self._build_toolbar()  # ← 工具栏移到最上面
        self._build_status_bar()
        self._build_chat_area()
        self._build_log_area()

        # 初始拉一次状态
        self._pull_status(force=True)

        # 定时刷新
        self.root.after(200, self._poll)
        self.root.after(2000, self._check_bot_alive)

        if auto_start:
            self.root.after(500, self.start_bot)

    # ------------------------------------------------------------------ UI 构建
    def _setup_window(self):
        self.root.title("♪ 你的昵称 ♡")
        self.root.geometry("1320x820")  # 再次放大窗口容纳大字号
        self.root.minsize(1040, 660)
        self.root.configure(bg=BG)
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass
        # 高DPI适配，避免模糊
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        # 关窗时不停止机器人（它是独立进程）
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_status_bar(self):
        bar = tk.Frame(self.root, bg=BG2, height=74)  # 加高以容纳大字号
        bar.pack(fill=tk.X, side=tk.TOP)
        bar.pack_propagate(False)

        def _badge(parent, label):
            f = tk.Frame(parent, bg=BG2)
            f.pack(side=tk.LEFT, padx=14, pady=14)
            tk.Label(f, text=label, bg=BG2, fg=FG_DIM, font=("Microsoft YaHei UI", 14)).pack(side=tk.LEFT)
            dot = tk.Canvas(f, width=15, height=15, bg=BG2, highlightthickness=0)  # 加大圆点
            dot.pack(side=tk.LEFT, padx=(5, 0))
            oval = dot.create_oval(1, 1, 14, 14, fill=ERR, outline="")
            val = tk.Label(f, text="—", bg=BG2, fg=FG, font=("Microsoft YaHei UI", 14, "bold"))
            val.pack(side=tk.LEFT, padx=(7, 0))
            return dot, oval, val

        self._dot_online, self._oval_online, self._val_online = _badge(bar, "微信")
        self._dot_bot, self._oval_bot, self._val_bot = _badge(bar, "机器人")
        self._dot_canvas_online = self._dot_online
        self._dot_canvas_bot = self._dot_bot
        self._val_account = self._badge_label(bar, "账号")
        self._val_mode = self._badge_label(bar, "模式")
        self._val_count = self._badge_label(bar, "今日回复")
        self._val_last = self._badge_label(bar, "最后消息")

        # 右侧：♪ + ♡ 装饰符号（加大更清晰）
        tk.Label(bar, text="♪", bg=BG2, fg=NOTE_COLOR, font=("Microsoft YaHei UI", 26, "bold")).pack(side=tk.RIGHT, padx=18)
        tk.Label(bar, text="♡", bg=BG2, fg=HEART_COLOR, font=("Microsoft YaHei UI", 24)).pack(side=tk.RIGHT, padx=(0, 8))

    def _badge_label(self, parent, label):
        f = tk.Frame(parent, bg=BG2)
        f.pack(side=tk.LEFT, padx=14, pady=14)
        tk.Label(f, text=label, bg=BG2, fg=FG_DIM, font=("Microsoft YaHei UI", 14)).pack(side=tk.LEFT)
        val = tk.Label(f, text="—", bg=BG2, fg=FG, font=("Microsoft YaHei UI", 14, "bold"))
        val.pack(side=tk.LEFT, padx=(7, 0))
        return val

    def _build_chat_area(self):
        frame = tk.LabelFrame(self.root, text="  ♪ 对话流 ♡  ", bg=BG, fg=ACCENT,
                              font=("Microsoft YaHei UI", 15, "bold"), bd=0, relief=tk.FLAT)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(10, 4))

        self._chat_text = tk.Text(frame, bg=BG, fg=FG, font=("Microsoft YaHei UI", 16),  # 大字号
                                  wrap=tk.WORD, bd=0, padx=14, pady=12,
                                  state=tk.DISABLED, insertbackground=ACCENT,
                                  selectbackground="#F9C6D8", selectforeground=FG)
        scrollbar = ttk.Scrollbar(frame, command=self._chat_text.yview)
        self._chat_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._chat_text.pack(fill=tk.BOTH, expand=True)

        # 消息标签（主题风格：接收浅樱粉，发送浅冰晶蓝，冷暖对比，更大间距）
        self._chat_text.tag_configure("recv", foreground=FG, background=RECV_BG, lmargin1=18, lmargin2=18,
                                      spacing1=5, spacing3=5, relief=tk.FLAT, borderwidth=2)
        self._chat_text.tag_configure("send", foreground=FG, background=SEND_BG, justify=tk.RIGHT,
                                      lmargin1=18, lmargin2=18,
                                      spacing1=5, spacing3=5, relief=tk.FLAT, borderwidth=2)
        self._chat_text.tag_configure("sys", foreground=FG_DIM, background=SYS_BG, font=("Microsoft YaHei UI", 14),
                                      lmargin1=18, lmargin2=18,
                                      spacing1=5, spacing3=5)
        self._chat_text.tag_configure("err", foreground=ERR, lmargin1=18, lmargin2=18,
                                      spacing1=5, spacing3=5)
        self._chat_text.tag_configure("chat_header", foreground=ACCENT,
                                      font=("Microsoft YaHei UI", 14, "italic"),
                                      lmargin1=18, lmargin2=18, spacing1=7)
        # 思考过程（模型思维链）：斜体浅色、缩进、独立浅背景，和正常消息区分开
        self._chat_text.tag_configure("think", foreground=FG_DIM, background="#FFF5F8",
                                      font=("Microsoft YaHei UI", 15, "italic"),
                                      lmargin1=34, lmargin2=34,
                                      spacing1=4, spacing3=4)

    def _build_log_area(self):
        frame = tk.LabelFrame(self.root, text="  系统日志  ", bg=BG, fg=FG_DIM,
                              font=("Microsoft YaHei UI", 14), bd=0, relief=tk.FLAT, height=180)
        frame.pack(fill=tk.X, padx=12, pady=(4, 10))
        frame.pack_propagate(False)

        self._log_text = tk.Text(frame, bg="#FFF5F8", fg=FG_DIM, font=("Consolas", 14),  # 大字号
                                 wrap=tk.WORD, bd=0, padx=10, pady=8,
                                 state=tk.DISABLED, height=6)
        scrollbar = ttk.Scrollbar(frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(fill=tk.BOTH, expand=True)
        self._log_text.tag_configure("ok", foreground="#D4738F")  # 深粉（成功）
        self._log_text.tag_configure("warn", foreground="#D4A5B5")  # 浅粉灰（警告）
        self._log_text.tag_configure("err", foreground="#E0527F")  # 深亮粉（错误，避免刺眼）
        self._log_text.tag_configure("info", foreground=FG_DIM)

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=BG2, height=70)  # 加高以容纳大字号
        bar.pack(fill=tk.X, side=tk.TOP)  # ← TOP
        bar.pack_propagate(False)

        def _btn(text, cmd, color=ACCENT):
            b = tk.Button(bar, text=text, command=cmd, bg=BG, fg=color,
                          activebackground="#FDE8EF", activeforeground=FG,
                          font=("Microsoft YaHei UI", 14, "bold"), bd=0, padx=24, pady=11,  # 大字号大按钮
                          cursor="hand2", relief=tk.FLAT)
            b.pack(side=tk.LEFT, padx=(12, 6), pady=11)
            return b

        _btn("▶ 启动", self.start_bot, "#D4738F")  # 深粉（启动/在线）
        _btn("⏹ 停止", self.stop_bot, "#E0527F")   # 深亮粉（停止/错误）
        _btn("↻ 重启", self.restart_bot, "#D4A5B5") # 浅粉灰（重启/警告）
        _btn("📂 打开日志", lambda: self._open_file(LOG_PATH), FG_DIM)
        _btn("⚙ 配置", lambda: self._open_file(CONFIG_PATH), FG_DIM)
        _btn("🗑 清空对话", self._clear_chat, FG_DIM)

        # 状态栏右侧：机器人 PID
        self._val_pid = tk.Label(bar, text="", bg=BG2, fg=FG_DIM,
                                 font=("Consolas", 14))
        self._val_pid.pack(side=tk.RIGHT, padx=18)

    # ------------------------------------------------------------------ 机器人控制
    def _is_bot_running(self) -> bool:
        if self.bot_proc and self.bot_proc.poll() is None:
            return True
        # 也检查 lock 文件（可能是别的终端启动的）
        if os.path.exists(LOCK_PATH):
            try:
                with open(LOCK_PATH) as f:
                    parts = f.read().split()
                if parts:
                    import psutil
                    pid = int(parts[0])
                    if psutil.pid_exists(pid):
                        return True
            except Exception:
                pass
        return False

    def start_bot(self):
        if self._is_bot_running():
            self._log("机器人已在运行，无需重复启动", "warn")
            return
        try:
            # 走 start_bot.bat 启动，继承它的"微信在线检查 + 依赖自检自愈"，
            # 否则依赖丢失时 wx_bot.py 会静默死掉（今天就栽在这）。
            bat = os.path.join(BASE_DIR, "start_bot.bat")
            env = dict(os.environ)
            env["NO_PAUSE"] = "1"  # 后台启动：出错也不 pause 卡住隐藏窗口
            self.bot_proc = subprocess.Popen(
                ["cmd", "/c", bat],
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            self._log("机器人启动中（自检微信 + 依赖）…", "ok")
            threading.Thread(target=self._pump_bot_output, daemon=True).start()
        except Exception as e:
            self._log(f"启动失败：{e}", "err")
            messagebox.showerror("启动失败", str(e))

    def stop_bot(self):
        killed = False
        # 先杀自己启动的子进程
        if self.bot_proc and self.bot_proc.poll() is None:
            try:
                self.bot_proc.terminate()
                killed = True
            except Exception:
                pass
        # 再扫残留进程
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "cmdline"]):
                cl = " ".join(proc.info.get("cmdline") or [])
                if "wx_bot.py" in cl:
                    proc.kill()
                    killed = True
        except Exception:
            pass
        # 清 lock
        try:
            if os.path.exists(LOCK_PATH):
                os.remove(LOCK_PATH)
        except Exception:
            pass
        self._log("机器人已停止" if killed else "未发现运行中的机器人", "ok" if killed else "warn")

    def restart_bot(self):
        self.stop_bot()
        self.root.after(1500, self.start_bot)

    def _pump_bot_output(self):
        """读取机器人子进程的 stdout，推到系统日志区。"""
        if not self.bot_proc:
            return
        try:
            for raw in iter(self.bot_proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._events_buffer.append(("bot_stdout", line))
        except Exception:
            pass

    # ------------------------------------------------------------------ 状态与事件
    def _pull_status(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_status_pull < 1.0:
            return
        self._last_status_pull = now

        st = self._reader.status()

        # 微信在线
        online = st.get("online", False)
        self._set_dot(self._dot_canvas_online, self._oval_online, OK if online else ERR)
        self._val_online.config(text="在线" if online else "离线",
                                fg=OK if online else ERR)

        # 机器人运行
        running = self._is_bot_running() or st.get("running", False)
        self._set_dot(self._dot_canvas_bot, self._oval_bot, OK if running else ERR)
        self._val_bot.config(text="运行中" if running else "已停止",
                             fg=OK if running else ERR)

        # 账号
        acct = st.get("account", "")
        self._val_account.config(text=acct or "—")

        # 模式
        mode = "DRY-RUN" if st.get("dry_run") else "正式"
        self._val_mode.config(text=mode, fg=WARN if st.get("dry_run") else OK)

        # 今日回复数
        cnt = st.get("reply_count_today", 0)
        self._val_count.config(text=str(cnt))

        # 最后消息
        last = st.get("last_msg_time", 0)
        if last:
            dt = time.time() - last
            if dt < 60:
                self._val_last.config(text=f"{int(dt)}秒前", fg=OK)
            elif dt < 3600:
                self._val_last.config(text=f"{int(dt // 60)}分钟前", fg=WARN)
            else:
                self._val_last.config(text=f"{int(dt // 3600)}小时前", fg=ERR)
        else:
            self._val_last.config(text="—", fg=FG_DIM)

        # PID
        pid_text = ""
        if self.bot_proc and self.bot_proc.poll() is None:
            pid_text = f"PID: {self.bot_proc.pid}"
        elif st.get("account_wxid"):
            pid_text = ""
        self._val_pid.config(text=pid_text)

    def _set_dot(self, canvas, oval_id, color):
        canvas.itemconfig(oval_id, fill=color)

    def _poll(self):
        """每 200ms 轮询一次：拉状态 + 处理事件。"""
        self._pull_status()

        # 从 bot stdout 管道来的文本
        while self._events_buffer:
            kind, payload = self._events_buffer.pop(0)
            if kind == "bot_stdout":
                self._append_log(payload)
            elif kind == "chat_recv":
                self._append_chat("recv", payload)
            elif kind == "chat_send":
                self._append_chat("send", payload)
            elif kind == "system":
                self._append_chat("sys", payload)
            elif kind == "error":
                self._append_chat("err", payload)

        # 从 bot 文件读事件
        for ev in self._reader.drain():
            kind = ev.get("kind")
            if kind == "recv":
                self._append_chat("recv", f"{ev.get('chat', '?')} · {ev.get('sender', '?')}：{ev.get('text', '')}")
            elif kind == "send":
                tag = "send" if ev.get("ok") else "err"
                prefix = "" if ev.get("ok") else "[失败] "
                self._append_chat(tag, f"{prefix}{ev.get('chat', '?')} ← {ev.get('text', '')}")
            elif kind == "thinking":
                # 模型思维链：多行文本整体缩进展示（不加时间戳前缀，保持思考原文完整）
                text = (ev.get("text") or "").strip()
                if text:
                    self._append_think(f"{ev.get('chat', '?')} · {ev.get('sender', '?')} 思考中…", text)
            elif kind == "system":
                self._append_chat("sys", ev.get("text", ""))
            elif kind == "warn":
                self._append_chat("err", f"⚠ {ev.get('text', '')}")
            elif kind == "error":
                self._append_chat("err", f"✗ {ev.get('text', '')}")

        self.root.after(200, self._poll)

    def _check_bot_alive(self):
        """定期检查子进程是否还活着。"""
        if self.bot_proc and self.bot_proc.poll() is not None:
            rc = self.bot_proc.returncode
            self._log(f"机器人进程退出（exit code {rc}）", "warn" if rc == 0 else "err")
            self.bot_proc = None
        self.root.after(2000, self._check_bot_alive)

    # ------------------------------------------------------------------ 文本渲染
    def _append_chat(self, tag: str, text: str):
        ts = time.strftime("%H:%M:%S")
        self._chat_text.config(state=tk.NORMAL)
        # 自动滚动判断
        at_bottom = self._chat_text.yview()[1] >= 0.99
        line = f"[{ts}] {text}\n"
        self._chat_text.insert(tk.END, line, tag)
        # 截断
        max_lines = 2000
        line_count = int(self._chat_text.index("end-1c").split(".")[0])
        if line_count > max_lines:
            self._chat_text.delete("1.0", f"{line_count - max_lines}.0")
        if at_bottom:
            self._chat_text.see(tk.END)
        self._chat_text.config(state=tk.DISABLED)

    def _append_think(self, header: str, body: str):
        """把模型的思维链（多行）以斜体缩进块渲染到对话流。"""
        ts = time.strftime("%H:%M:%S")
        self._chat_text.config(state=tk.NORMAL)
        at_bottom = self._chat_text.yview()[1] >= 0.99
        # 思维链可能很长，截断到合理长度避免刷屏（完整内容在 bot_replies.log 里没有，这里就地保留前 1200 字）
        if len(body) > 1200:
            body = body[:1200] + " ……（已截断）"
        block = f"[{ts}] ◈ {header}\n{body}\n"
        self._chat_text.insert(tk.END, block, "think")
        # 截断
        max_lines = 2000
        line_count = int(self._chat_text.index("end-1c").split(".")[0])
        if line_count > max_lines:
            self._chat_text.delete("1.0", f"{line_count - max_lines}.0")
        if at_bottom:
            self._chat_text.see(tk.END)
        self._chat_text.config(state=tk.DISABLED)

    def _append_log(self, text: str, tag: str = "info"):
        self._log_text.config(state=tk.NORMAL)
        at_bottom = self._log_text.yview()[1] >= 0.99
        # 自动识别 tag
        if "⚠" in text or "警告" in text or "warning" in text.lower():
            tag = "warn"
        elif "错误" in text or "error" in text.lower() or "Traceback" in text:
            tag = "err"
        elif "启动" in text or "已发送" in text or "收到" in text:
            tag = "ok"
        ts = time.strftime("%H:%M:%S")
        self._log_text.insert(tk.END, f"[{ts}] {text}\n", tag)
        # 截断
        max_lines = 500
        line_count = int(self._log_text.index("end-1c").split(".")[0])
        if line_count > max_lines:
            self._log_text.delete("1.0", f"{line_count - max_lines}.0")
        if at_bottom:
            self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _log(self, text: str, tag: str = "info"):
        self._append_log(text, tag)

    def _clear_chat(self):
        self._chat_text.config(state=tk.NORMAL)
        self._chat_text.delete("1.0", tk.END)
        self._chat_text.config(state=tk.DISABLED)

    # ------------------------------------------------------------------ 工具
    def _open_file(self, path: str):
        if os.path.exists(path):
            os.startfile(path) if sys.platform == "win32" else subprocess.Popen(["xdg-open", path])
        else:
            messagebox.showinfo("提示", f"文件不存在：{path}")

    def _on_close(self):
        # 不杀机器人（它是独立进程），只关 GUI
        self.root.destroy()


def main():
    auto_start = "--attach" not in sys.argv
    root = tk.Tk()
    BotGUI(root, auto_start=auto_start)
    root.mainloop()


if __name__ == "__main__":
    main()
