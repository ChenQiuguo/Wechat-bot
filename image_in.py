# -*- coding: utf-8 -*-
"""图片理解支持：把微信消息里的图片取出、解密、编码，交给模型的视觉输入。

要点（都经本机实测校正）：
  * 图片消息的正文是一段 XML，**群消息还带 `wxid_xxx:\\n` 前缀**，所以取指纹前
    必须先剥前缀，再去 XML 里找 32 位 hex（就是本地 `.dat` 的文件名）；
  * 只读本机缓存，**不驱动界面**：三档副本 `_h.dat`(原件) > `.dat`(微信下发的那份)
    > `_t.dat`(预览图)。默认取「不是预览图的那一份」，本机两档都没有就放弃
    （不点开图片，避免把微信窗口拽到前台）；
  * 视频/文件等其他媒体不在这里处理；
  * `wxgf` 是微信动画表情的容器（内部 HEVC），不是标准图片，模型看不了 → 跳过。
"""
from __future__ import annotations

import base64
import os
import re
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

_MD5_RE = re.compile(r"[0-9a-fA-F]{32}")
_PREFIX_RE = re.compile(r"^(wxid_[0-9A-Za-z_\-]{3,}|[0-9A-Za-z_\-]+@chatroom):\s*\n?")

_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}

_IMG_EXT = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}

MAX_BYTES_DEFAULT = 8 * 1024 * 1024        # 单张图片上限（超过就跳过，别把请求撑爆）


def sniff_mime(head: bytes) -> str:
    """按文件实际内容判断格式（API 也是按内容判，不看扩展名）。"""
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def image_md5(content) -> str:
    """从图片消息正文里取出本图指纹（32 位 hex），取不到返回空串。"""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    text = _PREFIX_RE.sub("", (content or "").strip(), count=1)
    m = _MD5_RE.search(text)
    return m.group(0).lower() if m else ""


def _raw_message(db, chat_username: str, local_id):
    """尽量拿到这条消息的原始正文（带 packed_info 的窄查询，便于取指纹）。"""
    try:
        row = db.get_message_row(chat_username, int(local_id), local_type=3)
    except Exception:
        row = None
    return (row or {}).get("content") or (row or {}).get("packed_info") or ""


def read_image(db, chat_username: str, local_id, md5hint: str = "", *,
               save_dir: str = "", log=print):
    """读出这条图片消息在本机的图片字节。返回 ``(data, mime)``，拿不到返回 ``(None, "")``。

    只读缓存、不动界面；本机只有预览图时也用（`_t.dat` 仍能看懂大意，
    但若存在 `_h.dat`/`.dat` 就优先用它们）。
    """
    md5 = (md5hint or "").lower() or image_md5(_raw_message(db, chat_username, local_id))
    if not md5:
        log(f"  · 图片 #{local_id} 正文里找不到指纹，跳过")
        return None, ""
    path = ""
    try:
        from wechatauto.media import MediaDownloader
        md = MediaDownloader(db)
        path = md.download_image(chat_username, int(local_id),
                                 save_dir=save_dir or None, tier="full") or ""
    except Exception as e:  # noqa: BLE001
        log(f"  · 图片 #{local_id} 解密失败：{type(e).__name__}: {e}")
        return None, ""
    if not path:
        # `full` 只认「不是预览图」的那两份；本机只剩预览图时退回预览图，聊胜于无
        try:
            from wechatauto.media import MediaDownloader
            path = MediaDownloader(db).download_image(
                chat_username, int(local_id), save_dir=save_dir or None, tier="thumb") or ""
        except Exception:  # noqa: BLE001
            path = ""
    if not path or not os.path.isfile(path):
        log(f"  · 图片 #{local_id} 本机没有缓存（微信里没点开过），跳过")
        return None, ""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        log(f"  · 图片 #{local_id} 读文件失败：{e}")
        return None, ""
    mime = sniff_mime(data[:16])
    if not mime:
        log(f"  · 图片 #{local_id} 不是标准图片格式（可能是 wxgf 动画表情），跳过")
        return None, ""
    return data, mime


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def image_block(data: bytes, mime: str, detail: str = "low") -> dict:
    """OpenAI 兼容的内容块（图片只能出现在 user 消息里）。"""
    return {"type": "image_url",
            "image_url": {"url": data_url(data, mime), "detail": detail or "low"}}


def image_cfg(cfg: dict) -> dict:
    """图片相关配置（缺失时给安全默认值）。"""
    ic = dict((cfg or {}).get("image") or {})
    ic.setdefault("enabled", False)
    ic.setdefault("detail", "low")
    ic.setdefault("max_bytes", MAX_BYTES_DEFAULT)
    ic.setdefault("save_dir", "")
    return ic


class ImageMemory:
    """最近取到的图片字节，按 (会话, local_id) 暂存。

    为什么要缓存：群聊里一条图片消息会先过「是不是在叫我」的判定，
    判定通过后生成回复时要再用一次。没有缓存就会解密两遍（要走 os.walk 扫目录）。
    """

    def __init__(self, limit: int = 12, ttl: float = 600.0):
        self._d: dict = {}
        self._lock = threading.Lock()
        self.limit = int(limit)
        self.ttl = float(ttl)

    def put(self, chat_username: str, local_id, item):
        if not item:
            return
        with self._lock:
            self._d[(chat_username, str(local_id))] = (time.time(), item)
            if len(self._d) > self.limit:
                for k, _ in sorted(self._d.items(), key=lambda kv: kv[1][0])[
                        :len(self._d) - self.limit]:
                    self._d.pop(k, None)

    def get(self, chat_username: str, local_id):
        with self._lock:
            v = self._d.get((chat_username, str(local_id)))
            if not v:
                return None
            if time.time() - v[0] > self.ttl:
                self._d.pop((chat_username, str(local_id)), None)
                return None
            return v[1]
