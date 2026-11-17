# -*- coding: utf-8 -*-
"""长期记忆（mem0 + 本地向量）：把聊过的话存成向量，回复时按语义捞回相关片段。

为什么单独一层：
* ``memory_store.py`` 的档案是「稳定的关于对方的事实」（最多 25 条，常驻注入），贵在精；
* 这一层是「聊过什么」的情节记忆，量大、按需召回，不进常驻 prompt；
* 依赖 mem0（开源，~63K★）+ fastembed（本地 ONNX 嵌入，无需 OpenAI key）+ qdrant 本地库。

设计约束：
* **不加额外 LLM 调用**：默认 ``infer=false``，直接把原话写进向量库（只走本地嵌入，~0.3s）；
  想要 mem0 的事实抽取（去重/覆盖矛盾）就把 ``ai.memory.vector.infer`` 打开，代价是每条多一次模型调用；
* **不在回复路径上阻塞**：初始化放在启动时的后台预热线程，``recall`` 在没就绪时直接返回空；
* **失败即静默降级**：没装依赖 / 模型下不下来 / 路径被占，都只打印一行日志，主流程照旧。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

# mem0 默认会往自家服务器打点；本机跑不需要
os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
# 国内直连 huggingface 基本下不动嵌入模型，没显式配就用镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_LOCK = threading.Lock()
_STATE = {"mem": None, "ready": False, "error": ""}


def vcfg(cfg_ai: dict) -> dict:
    return ((cfg_ai or {}).get("memory") or {}).get("vector") or {}


def enabled(cfg_ai: dict) -> bool:
    return bool(vcfg(cfg_ai).get("enabled"))


def _build(cfg_ai: dict, key: str):
    """构造 mem0 的 Memory 实例（首次约 2s，含本地向量库加载）。"""
    from mem0 import Memory

    v = vcfg(cfg_ai)
    store_dir = v.get("dir") or os.path.join(BASE_DIR, "memory_vec")
    os.makedirs(store_dir, exist_ok=True)
    model = v.get("model") or "BAAI/bge-small-zh-v1.5"
    dims = int(v.get("dims") or 512)
    conf = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": cfg_ai.get("model") or "mimo-v2.6-flash",
                "api_key": key,
                "openai_base_url": (cfg_ai.get("base_url") or "").rstrip("/"),
                "temperature": 0.1,
                # MiMo 不认 max_tokens（要 max_completion_tokens）；标成 reasoning 模型让 mem0 干脆不发这两个字段
                "is_reasoning_model": True,
            },
        },
        "embedder": {"provider": "fastembed", "config": {"model": model}},
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": v.get("collection") or "wx_memory",
                "path": store_dir,
                "embedding_model_dims": dims,
                "on_disk": True,
            },
        },
    }
    return Memory.from_config(conf)


def warmup(cfg_ai: dict, key: str) -> None:
    """启动时后台调用：把模型/向量库加载好，别让第一条消息等 2 秒。"""
    if not enabled(cfg_ai) or not key or _STATE["ready"]:
        return
    t0 = time.time()
    try:
        with _LOCK:
            if _STATE["ready"]:
                return
            _STATE["mem"] = _build(cfg_ai, key)
            _STATE["ready"] = True
        print(f"  [长期记忆] 向量库就绪（{time.time() - t0:.1f}s）", flush=True)
    except Exception as e:  # noqa: BLE001
        _STATE["error"] = f"{type(e).__name__}: {e}"
        print(f"  [长期记忆] 初始化失败，降级为仅档案：{_STATE['error']}", flush=True)


def status() -> dict:
    return {"ready": _STATE["ready"], "error": _STATE["error"]}


def _uid(chat_username: str) -> str:
    return f"chat:{chat_username}"


def recall(cfg_ai: dict, chat_username: str, query: str, limit: int = 0) -> list:
    """按语义捞回该会话相关的旧片段；没就绪/没匹配就返回 []。"""
    if not _STATE["ready"] or not query:
        return []
    v = vcfg(cfg_ai)
    try:
        hits = _STATE["mem"].search(
            query,
            filters={"user_id": _uid(chat_username)},
            limit=int(limit or v.get("top_k") or 5),
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [长期记忆] 召回失败：{type(e).__name__}: {e}", flush=True)
        return []
    rows = hits.get("results", hits) if isinstance(hits, dict) else (hits or [])
    floor = float(v.get("min_score") or 0.0)
    out = []
    for r in rows:
        text = (r.get("memory") or "").strip()
        if not text:
            continue
        if floor and float(r.get("score") or 0) < floor:
            continue
        out.append(text)
    return out


def remember(cfg_ai: dict, chat_username: str, chat_name: str,
             lines: list, infer: bool | None = None) -> int:
    """把一段对话写进向量库（后台线程调用）。返回写入条数，失败返回 0。"""
    if not _STATE["ready"] or not lines:
        return 0
    v = vcfg(cfg_ai)
    do_infer = bool(v.get("infer", False)) if infer is None else bool(infer)
    max_chars = int(v.get("max_line_chars") or 500)
    msgs = []
    for line in lines[-int(v.get("max_lines") or 8):]:
        text = str(line).strip()[:max_chars]
        if len(text) < int(v.get("min_line_chars") or 4):
            continue
        if text.startswith("我:"):
            role, content = "assistant", text[2:].strip()
        elif ": " in text[:40]:
            role, content = "user", text.split(": ", 1)[1].strip()
        else:
            role, content = "user", text
        if not content:
            continue
        msgs.append({"role": role, "content": content})
    if not msgs:
        return 0
    try:
        res = _STATE["mem"].add(
            msgs,
            user_id=_uid(chat_username),
            infer=do_infer,
            metadata={"chat": chat_name or "", "ts": time.strftime("%Y-%m-%d %H:%M")},
        )
        rows = res.get("results", []) if isinstance(res, dict) else (res or [])
        return len(rows)
    except Exception as e:  # noqa: BLE001
        print(f"  [长期记忆] 写入失败：{type(e).__name__}: {e}", flush=True)
        return 0


def block(cfg_ai: dict, chat_username: str, query: str, context_lines: list,
          limit: int = 0) -> str:
    """召回并格式化成一段注入 prompt 的文本；与上下文重复的条目会被剔掉。"""
    memories = recall(cfg_ai, chat_username, query, limit=limit)
    if not memories:
        return ""
    ctx = "\n".join(str(x) for x in (context_lines or []))
    keep = []
    for mtext in memories:
        core = mtext.strip()
        if len(core) >= 8 and core[:30] in ctx:   # 已经在这次上下文里，别重复喂
            continue
        if core not in keep:
            keep.append(core)
    if not keep:
        return ""
    cap = int(vcfg(cfg_ai).get("max_block_chars") or 1200)
    lines, used = [], 0
    for mtext in keep:
        if used + len(mtext) > cap:
            break
        lines.append(f"- {mtext}")
        used += len(mtext)
    if not lines:
        return ""
    return ("\n\n【长期记忆（以前和这个人聊过的相关片段；只作参考，"
            "别当成刚刚发生的事，也别直接复述）】\n" + "\n".join(lines))
