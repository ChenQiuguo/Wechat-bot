/* 你的昵称监控台 · 前端逻辑（自研「粉雾璃光」设计，2026-11-14）
 * 只接本机文件 IPC 的 HTTP 接口（/api/status /api/events /api/log /api/action /api/open）。
 * 交互：会话筛选、全部/单会话/日志三视图、#chat:xxx 与 #log 深链、粘底滚动、JS 错误浮层。 */
"use strict";

/* 出错时把错误画在页面底部（监控面板要能自诊断），并记入 title 便于截图排查 */
function _showJsError(msg, where) {
    if (document.getElementById("__js_err")) return;
    const d = document.createElement("div");
    d.id = "__js_err";
    d.style.cssText = "position:fixed;left:0;bottom:0;right:0;background:#E0527F;color:#fff;" +
        "padding:7px 12px;font-size:13px;z-index:99;font-family:Consolas,monospace;";
    d.textContent = "JS错误: " + msg + (where ? " @ " + where : "");
    document.body.appendChild(d);
    document.title = "ERR:" + msg;
}
window.addEventListener("error", (e) => _showJsError(e.message, (e.filename || "").split("/").pop() + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => _showJsError("promise: " + ((e.reason && e.reason.message) || e.reason)));

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

const TONES = ["#F77EA9", "#E88AAE", "#D4738F", "#C98FB3", "#B98AB1", "#F4A7C0", "#CB7E9E"];
function toneOf(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return TONES[h % TONES.length];
}
const SELF_TONE = "linear-gradient(140deg,#F77EA9,#E88AAE)";

let evTotal = 0, logTotal = 0;
let events = [];          // {ts,kind,chat,sender,text}
let logs = [];            // 原始日志行
let status = null;        // bot_status.json
let running = false;
let view = "all";         // "all" | "chat:<名>" | "log"；支持 #chat:xxx / #log 深链
if (location.hash) {
    const h = decodeURIComponent(location.hash.slice(1));
    if (h === "log" || h.indexOf("chat:") === 0) view = h;
}
let sessions = new Map(); // 会话名 -> {lastTs, lastText, lastKind}
let filterText = "";
let stickBottom = true;   // 滚动是否贴着底部（新消息时自动跟随）
let toastTimer = null;

/* ---------------- 时间格式 ---------------- */
const pad = (n) => String(n).padStart(2, "0");
function hm(ts) {
    const d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function hms(ts) {
    const d = new Date(ts * 1000);
    return hm(ts) + ":" + pad(d.getSeconds());
}
function dayLabel(ts) {
    const d = new Date(ts * 1000);
    const now = new Date();
    const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((startOf(now) - startOf(d)) / 86400000);
    if (diff === 0) return "今天";
    if (diff === 1) return "昨天";
    return (d.getMonth() + 1) + "月" + d.getDate() + "日";
}
function shortTime(ts) {
    const d = new Date(ts * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay ? hm(ts) : (d.getMonth() + 1) + "/" + d.getDate() + " " + hm(ts);
}

/* ---------------- 数据拉取 ---------------- */
async function jget(url) {
    const r = await fetch(url, { cache: "no-store" });
    return r.json();
}

async function pullStatus() {
    try {
        const d = await jget("/api/status");
        status = d.status || null;
        running = !!d.running;
        renderStatus();
    } catch (e) { /* 服务没起时静默 */ }
}

async function pullEvents() {
    try {
        const d = await jget("/api/events?since=" + evTotal);
        if (d.total !== evTotal && d.lines && d.lines.length) {
            for (const line of d.lines) {
                let e;
                try { e = JSON.parse(line); } catch (err) { continue; }
                ingest(e);
            }
            evTotal = d.total;
            render();
        } else if (d.total !== evTotal) {
            evTotal = d.total;
        }
    } catch (e) { /* ignore */ }
}

async function pullLog() {
    try {
        const d = await jget("/api/log?since=" + logTotal);
        if (d.total !== logTotal && d.lines && d.lines.length) {
            for (const l of d.lines) logs.push(l);
            if (logs.length > 800) logs = logs.slice(-800);
            logTotal = d.total;
            if (view === "log") render();
        } else if (d.total !== logTotal) {
            logTotal = d.total;
        }
    } catch (e) { /* ignore */ }
}

/* 把一条事件吃进内存：归会话、缓存 */
function ingest(e) {
    e.ts = e.ts || (Date.now() / 1000);
    events.push(e);
    if (events.length > 1500) events = events.slice(-1500);

    if (e.kind === "recv") {
        touch(e.chat, e.text, e.ts, "recv");
    } else if (e.kind === "send") {
        const p = parseSend(e.text);
        if (p) touch(p.chat, p.content, e.ts, "send");
    }
}

function touch(chat, text, ts, kind) {
    if (!chat) return;
    let s = sessions.get(chat);
    if (!s) { s = { lastTs: 0, lastText: "", lastKind: "recv" }; sessions.set(chat, s); }
    if (ts >= s.lastTs) { s.lastTs = ts; s.lastText = text || ""; s.lastKind = kind; }
}

/* "[已发送] → 会话名: 内容 | 列表点击 …" → {chat, content, ok} */
function parseSend(text) {
    const m = /^\[(已发送|发送重试|发送放弃|发送失败|未确认[^\]]*)\]\s*→\s*/.exec(text || "");
    if (!m) return null;
    const rest = text.slice(m[0].length);
    const cut = rest.indexOf(": ");
    if (cut < 0) return null;
    let content = rest.slice(cut + 2);
    const pipe = content.indexOf(" | ");
    if (pipe >= 0) content = content.slice(0, pipe);
    return { chat: rest.slice(0, cut), content: content, ok: m[1] === "已发送" };
}

/* ---------------- 渲染：状态 ---------------- */
function renderStatus() {
    const pill = $("status-pill"), txt = $("status-text"), dot = $("status-dot");
    if (!pill) return;
    let mode, t;
    if (status && status.online && running) {
        mode = "on";
        t = (status.account || "你的昵称") + " · 在线 · 今日回复 " + (status.reply_count_today || 0) + " 条";
    } else if (running) {
        mode = "busy";
        t = "运行中 · 微信离线";
    } else {
        mode = "off";
        t = "机器人未运行";
    }
    pill.className = "status" + (mode === "on" ? " on" : mode === "busy" ? " busy" : "");
    txt.textContent = t;
    $("btn-start").disabled = running;
    $("btn-stop").disabled = !running;
}

function toast(msg) {
    const t = $("status-text");
    if (!t) return;
    t.textContent = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => renderStatus(), 2600);
}

function render() {
    renderSessions();
    renderTitle();
    if (view === "log") renderLog();
    else renderStream();
}

function renderTitle() {
    let title = "全部会话", extra = "";
    if (view.startsWith("chat:")) title = view.slice(5);
    else if (view === "log") title = "系统日志";
    $("view-title").textContent = title;
    $("view-meta").textContent = "";
}

/* ---------------- 渲染：会话列表 ---------------- */
function renderSessions() {
    const box = $("session-list");
    if (!box) return;

    const items = [];
    let overallTs = 0;
    const chats = [...sessions.entries()].sort((a, b) => b[1].lastTs - a[1].lastTs);
    for (const [, s] of chats) if (s.lastTs > overallTs) overallTs = s.lastTs;

    items.push({
        key: "all", name: "全部会话", char: "全", tone: SELF_TONE,
        prev: chats.length + " 个会话 · 实时聚合", ts: overallTs
    });
    for (const [name, s] of chats) {
        if (filterText && name.toLowerCase().indexOf(filterText.toLowerCase()) < 0) continue;
        items.push({
            key: "chat:" + name, name: name, char: name.slice(0, 1),
            tone: "linear-gradient(140deg," + toneOf(name) + ",#F4A7C0)",
            prev: (s.lastKind === "send" ? "你: " : "") + (s.lastText || ""),
            ts: s.lastTs
        });
    }
    items.push({
        key: "log", name: "系统日志", char: "志", tone: "linear-gradient(140deg,#C98FB3,#E88AAE)",
        prev: "收发 · 跳过原因 · 报错", ts: 0
    });

    box.innerHTML = items.map((it) => `
      <button class="sess ${view === it.key ? "active" : ""}" data-key="${esc(it.key)}" type="button">
        <span class="ava" style="background:${it.tone}">${esc(it.char)}</span>
        <span class="sess-body">
          <span class="sess-top">
            <span class="sess-name">${esc(it.name)}</span>
            <span class="sess-time">${it.ts ? shortTime(it.ts) : ""}</span>
          </span>
          <span class="sess-prev">${esc(it.prev)}</span>
        </span>
      </button>`).join("");

    box.querySelectorAll(".sess").forEach((el) => {
        el.addEventListener("click", () => {
            view = el.dataset.key;
            stickBottom = true;
            render();
        });
    });
    const n = chats.length;
    $("sess-count").textContent = n;
}

/* ---------------- 渲染：消息流 ---------------- */
function streamEvents() {
    if (view === "all") return events;
    const name = view.slice(5);
    return events.filter((e) =>
        (e.kind === "recv" && e.chat === name) ||
        (e.kind === "thinking" && e.chat === name) ||
        (e.kind === "send" && (parseSend(e.text) || {}).chat === name)
    );
}

function renderStream() {
    const el = $("stream");
    if (!el) return;
    const stick = stickBottom;
    const all = streamEvents();
    const VIEW_CAP = 600;
    const list = all.length > VIEW_CAP ? all.slice(-VIEW_CAP) : all;
    if (!list.length) {
        el.innerHTML = `<div class="empty"><div class="big">♪</div><div>还没有消息，等一会儿就好啦</div></div>`;
        return;
    }

    const showChat = view === "all";
    const html = [];
    let lastDay = "", prevKey = "", prevTs = 0;

    for (const e of list) {
        const day = dayLabel(e.ts);
        if (day !== lastDay) {
            html.push(`<div class="day"><span>${esc(day)}</span></div>`);
            lastDay = day;
            prevKey = "";
        }

        if (e.kind === "recv") {
            const key = "r|" + e.chat + "|" + (e.sender || "");
            const grouped = key === prevKey && e.ts - prevTs < 180;
            const tag = showChat && e.chat && e.chat !== e.sender
                ? `<span class="chat-tag">${esc(e.chat)}</span>` : "";
            html.push(`
              <div class="row other ${grouped ? "grouped" : ""}">
                <span class="ava" style="background:linear-gradient(140deg,${toneOf(e.sender || e.chat || "?")},#F4A7C0)">${esc((e.sender || e.chat || "?").slice(0, 1))}</span>
                <div class="bwrap">
                  <div class="meta">${tag}<span class="who">${esc(e.sender || e.chat || "")}</span></div>
                  <div class="bubble">${esc(e.text)}</div>
                  <div class="when">${hms(e.ts)}</div>
                </div>
              </div>`);
            prevKey = key; prevTs = e.ts;
        } else if (e.kind === "send") {
            const p = parseSend(e.text) || { chat: "", content: e.text, ok: true };
            const key = "s|" + p.chat;
            const grouped = key === prevKey && e.ts - prevTs < 180;
            html.push(`
              <div class="row self ${grouped ? "grouped" : ""}">
                <span class="ava self"><img src="icon/avatar.png" alt=""/></span>
                <div class="bwrap">
                  <div class="meta">${showChat && p.chat ? `<span class="chat-tag">${esc(p.chat)}</span>` : ""}<span class="who">${p.ok ? "你的昵称" : "你的昵称（未确认）"}</span></div>
                  <div class="bubble">${esc(p.content)}</div>
                  <div class="when">${hms(e.ts)}</div>
                </div>
              </div>`);
            prevKey = key; prevTs = e.ts;
        } else if (e.kind === "thinking") {
            // 默认展开（用户要求：不要自动折叠），仍可点标题栏手动收起
            const raw = (e.text || "").trim();
            const n = raw.length;
            const shown = raw.length > 1500 ? raw.slice(0, 1500) + "…" : raw;
            html.push(`
              <div class="think"><details open>
                <summary>${esc(e.sender || "…")} 思考过程 · ${n} 字</summary>
                <div class="tbody">${esc(shown)}</div>
              </details></div>`);
            prevKey = ""; prevTs = 0;
        } else if (e.kind === "system" && /启动完成|已停止/.test(e.text || "")) {
            html.push(`<div class="sysline"><span>${esc(e.text)}</span></div>`);
            prevKey = ""; prevTs = 0;
        }
    }

    el.innerHTML = html.join("");
    $("view-meta").textContent = (all.length > VIEW_CAP ? "最近 " + VIEW_CAP + " 条 · " : "") + all.length + " 条消息";
    if (stick) el.scrollTop = el.scrollHeight;
}

/* ---------------- 渲染：日志 ---------------- */
function severity(line) {
    if (/失败|错误|Traceback|回调异常/.test(line)) return "err";
    if (/未回|⚠|放弃|未确认/.test(line)) return "warn";
    if (/已发送|启动|钉号/.test(line)) return "ok";
    return "";
}

function renderLog() {
    const el = $("stream");
    if (!el) return;
    const stick = stickBottom;
    if (!logs.length) {
        el.innerHTML = `<div class="empty"><div class="big">📋</div><div>还没有日志</div></div>`;
        return;
    }
    const VIEW_CAP = 800;
    const list = logs.slice(-VIEW_CAP);
    el.innerHTML = list.map((l) => {
        const m = /^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)\]\s*/.exec(l);
        const lt = m ? m[1].slice(11, 19) : "";
        const tx = m ? l.slice(m[0].length) : l;
        const sev = severity(l);
        return `<div class="log-row ${sev}"><span class="lt">${esc(lt || "—")}</span><span class="lx">${esc(tx)}</span></div>`;
    }).join("");
    $("view-meta").textContent = logs.length + " 行";
    if (stick) el.scrollTop = el.scrollHeight;
}

/* ---------------- 控制 ---------------- */
async function action(name, btn) {
    if (btn) btn.disabled = true;
    toast(name + " 中…");
    try {
        const r = await fetch("/api/action", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: name })
        });
        const d = await r.json();
        toast(d.ok ? (d.msg || name + " 完成") : ("失败：" + (d.msg || "未知错误")));
    } catch (e) {
        toast("失败：" + e);
    }
    setTimeout(pullStatus, 800);
    setTimeout(pullStatus, 2600);
    if (btn) btn.disabled = false;
}

async function openWhat(target) {
    try {
        await fetch("/api/open", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ target: target })
        });
        toast("已打开 " + (target === "config" ? "config.json" : "bot_replies.log"));
    } catch (e) { toast("打开失败"); }
}

function init() {
    $("btn-start").onclick = () => action("start", $("btn-start"));
    $("btn-stop").onclick = () => action("stop", $("btn-stop"));
    $("btn-restart").onclick = () => action("restart", $("btn-restart"));
    $("btn-logfile").onclick = () => openWhat("log");
    $("btn-config").onclick = () => openWhat("config");
    $("btn-quit").onclick = async () => {
        try { await fetch("/api/quit", { method: "POST" }); } catch (e) { /* ignore */ }
        document.body.innerHTML = '<div class="bye">界面已关闭 · 机器人还在跑 ♪</div>';
    };

    const search = $("search");
    if (search) {
        search.addEventListener("input", () => {
            filterText = search.value.trim();
            renderSessions();
        });
    }

    const stream = $("stream");
    if (stream) {
        stream.addEventListener("scroll", () => {
            stickBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 80;
        });
    }

    pullStatus(); pullEvents(); pullLog();
    render();
    setInterval(pullStatus, 2000);
    setInterval(pullEvents, 1500);
    setInterval(pullLog, 3000);
}

document.addEventListener("DOMContentLoaded", init);
