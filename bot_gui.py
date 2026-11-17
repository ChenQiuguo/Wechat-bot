# -*- coding: utf-8 -*-
"""微信 AI 机器人图形化监控面板（Web 版）。

前端照搬 dot-chat（gitee.com/lidaoyang/dot-chat，木兰宽松许可证 MulanPSL-2.0，见
webui/LICENSE-dot-chat.txt）的 PC 聊天窗布局与类名体系，配色换成本机粉色系；
本文件只负责：本地 HTTP 服务（静态页 + 文件 IPC 接口）+ 机器人启停 + 开窗。

用法：
    python bot_gui.py             # 打开界面（自动启动机器人子进程）
    python bot_gui.py --attach    # 只开界面，不自动启动（连已有实例）
界面关闭后机器人继续独立运行；再次打开会复用已在跑的服务端口。
"""
from __future__ import annotations

import atexit
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEBUI_DIR = os.path.join(BASE_DIR, "webui")
PORT_FILE = os.path.join(BASE_DIR, "gui_port.txt")
LOCK_PATH = os.path.join(BASE_DIR, "bot.lock")
EVENTS_PATH = os.path.join(BASE_DIR, "bot_events.jsonl")
STATUS_PATH = os.path.join(BASE_DIR, "bot_status.json")
LOG_PATH = os.path.join(BASE_DIR, "bot_replies.log")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
START_OUT = os.path.join(BASE_DIR, "start_out.log")
BAT = os.path.join(BASE_DIR, "start_bot.bat")

# ---------------- 文件 IPC 读取 ----------------

def _read_lines(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _is_bot_running() -> bool:
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        with open(LOCK_PATH) as f:
            parts = f.read().split()
        if not parts:
            return False
        import psutil
        return psutil.pid_exists(int(parts[0]))
    except Exception:
        return False


def _status_obj() -> dict | None:
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _start_hint() -> str:
    """机器人没跑时，把最近一次启动输出的最后一行带出去（bat 的失败原因）。"""
    if _is_bot_running():
        return ""
    try:
        with open(START_OUT, "r", encoding="utf-8", errors="replace") as f:
            lines = [x.strip() for x in f.read().splitlines() if x.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


# ---- 会话别名：裸 ID → 显示名（§7.29；chat_alias.json 由 wx_bot 成功解析时刷新）----
ALIAS_PATH = os.path.join(BASE_DIR, "chat_alias.json")
_alias_cache = {"mtime": -1.0, "map": {}}


def _aliases() -> dict:
    try:
        m = os.path.getmtime(ALIAS_PATH)
    except OSError:
        return _alias_cache["map"]
    if m != _alias_cache["mtime"]:
        try:
            with open(ALIAS_PATH, encoding="utf-8") as f:
                _alias_cache["map"] = json.load(f)
        except Exception:
            _alias_cache["map"] = {}
        _alias_cache["mtime"] = m
    return _alias_cache["map"]


def _apply_alias_line(line: str, am: dict) -> str:
    """事件行是 JSON：把裸 chat / sender 换成显示名，同一会话就不会裂成两条。
    send 事件的会话名藏在 text 里（「[已发送] → 4746…@chatroom: 内容」），一并替换。"""
    try:
        e = json.loads(line)
        c, s = e.get("chat"), e.get("sender")
        nc, ns = am.get(c, c), am.get(s, s)
        t = e.get("text") or ""
        nt = t
        if any(k in t for k in am):
            nt = t
            for k, v in am.items():
                nt = nt.replace(k, v)
        if nc != c or ns != s or nt != t:
            e["chat"], e["sender"], e["text"] = nc, ns, nt
            return json.dumps(e, ensure_ascii=False)
    except Exception:
        pass
    return line


def _apply_alias_text(line: str, am: dict) -> str:
    """日志行是自由文本：裸 ID 子串直接替换成显示名。"""
    for k, v in am.items():
        if k in line:
            line = line.replace(k, v)
    return line


# ---------------- 启停控制（移植自旧 Tkinter 版） ----------------

def do_start() -> tuple[bool, str]:
    if _is_bot_running():
        return True, "机器人已在运行"
    try:
        env = dict(os.environ)
        env["NO_PAUSE"] = "1"  # 后台启动：出错也不 pause 卡住隐藏窗口
        with open(START_OUT, "w", encoding="utf-8") as out:
            subprocess.Popen(
                ["cmd", "/c", BAT], cwd=BASE_DIR, env=env,
                stdout=out, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        return True, "启动中（自检微信 + 依赖）…"
    except Exception as e:  # noqa: BLE001
        return False, f"启动失败：{e}"


def do_stop() -> tuple[bool, str]:
    killed = False
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            cl = " ".join(proc.info.get("cmdline") or [])
            if "wx_bot.py" in cl:
                proc.kill()
                killed = True
    except Exception:
        pass
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except OSError:
        pass
    return True, "机器人已停止" if killed else "未发现运行中的机器人"


def do_restart() -> tuple[bool, str]:
    do_stop()
    time.sleep(1.5)
    return do_start()


# ---------------- HTTP 服务 ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默访问日志
        pass

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/ping":
            return self._json({"ok": True})
        if u.path == "/api/status":
            return self._json({
                "running": _is_bot_running(),
                "status": _status_obj(),
                "start_hint": _start_hint(),
            })
        if u.path == "/api/events":
            since = int((q.get("since") or ["0"])[0] or 0)
            lines = _read_lines(EVENTS_PATH)
            total, out = len(lines), lines[since:]
            am = _aliases()
            if am and out:
                out = [_apply_alias_line(x, am) for x in out]
            return self._json({"total": total, "lines": out})
        if u.path == "/api/log":
            since = int((q.get("since") or ["0"])[0] or 0)
            lines = _read_lines(LOG_PATH)
            total, out = len(lines), lines[since:]
            am = _aliases()
            if am and out:
                out = [_apply_alias_text(x, am) for x in out]
            return self._json({"total": total, "lines": out})
        # 静态文件
        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        target = os.path.normpath(os.path.join(WEBUI_DIR, rel))
        if not target.startswith(os.path.normpath(WEBUI_DIR) + os.sep) and \
                target != os.path.normpath(WEBUI_DIR):
            return self._json({"error": "forbidden"}, 403)
        if not os.path.isfile(target):
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(target, "rb") as f:
            return self._bytes(f.read(), ctype)

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            payload = {}
        if u.path == "/api/action":
            act = payload.get("action")
            fn = {"start": do_start, "stop": do_stop, "restart": do_restart}.get(act)
            if not fn:
                return self._json({"ok": False, "msg": "未知动作"})
            ok, msg = fn()
            return self._json({"ok": ok, "msg": msg})
        if u.path == "/api/open":
            target = CONFIG_PATH if payload.get("target") == "config" else LOG_PATH
            try:
                os.startfile(target)  # noqa: S606
                return self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "msg": str(e)})
        if u.path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        return self._json({"error": "not found"}, 404)


# ---------------- 开窗 ----------------

def _edge_path() -> str | None:
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            return p
    return None


def _icon_stamp() -> str:
    """给页面 URL 加个版本号：Chromium 的 favicon 是按**页面 URL**缓存的，
    光改 favicon 文件/加 ?v= 到 icon 上没用，任务栏会一直显示旧图标（§7.31④）。
    用 index.html 与 favicon.ico 的 mtime 当版本号，改完图标重开窗就换新的。"""
    try:
        stamp = max(os.path.getmtime(os.path.join(WEBUI_DIR, "index.html")),
                    os.path.getmtime(os.path.join(WEBUI_DIR, "icon", "favicon.ico")))
        return str(int(stamp))
    except Exception:
        return ""


def open_window(url: str):
    edge = _edge_path()
    stamp = _icon_stamp()
    if stamp:
        url = url + ("&" if "?" in url else "?") + "v=" + stamp
    try:
        if edge:
            subprocess.Popen(
                [edge, f"--app={url}", "--no-first-run", "--no-default-browser-check",
                 "--window-size=1340,860"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            import webbrowser
            webbrowser.open(url)
    except Exception:
        import webbrowser
        webbrowser.open(url)


def _existing_url() -> str | None:
    try:
        with open(PORT_FILE) as f:
            port = int(f.read().strip())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.5) as r:
            if json.loads(r.read()).get("ok"):
                return f"http://127.0.0.1:{port}/"
    except Exception:
        pass
    return None


def main():
    auto_start = "--attach" not in sys.argv
    url = _existing_url()
    if url:  # 服务已在跑：只开窗（要拉机器人就再发一次 start，运行中会自幂等）
        if auto_start:
            try:
                req = urllib.request.Request(
                    url + "api/action", data=b'{"action":"start"}',
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass
        open_window(url)
        return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    with open(PORT_FILE, "w") as f:
        f.write(str(port))

    def _cleanup():
        try:
            os.remove(PORT_FILE)
        except OSError:
            pass
    atexit.register(_cleanup)

    if auto_start:
        do_start()
    open_window(f"http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
