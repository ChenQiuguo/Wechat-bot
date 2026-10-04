# -*- coding: utf-8 -*-
"""导出某个会话的全部聊天记录（本地留档用）。

数据来源与机器人完全一致：微信本地加密库 → 解密 → 只读查询。
**不改动微信任何文件**。

用法：
  python export_history.py --who Celestiaveil                # 昵称/备注/wxid 都行
  python export_history.py --who wxid_xxx --format jsonl
  python export_history.py --list                            # 只列出有消息的会话

产物默认写到 ./exports/<会话名>-<日期>.md ，**不含 wxid、不含时间戳以外的元数据**。
注意：exclude 掉媒体消息的原始 XML，只留 [图片]/[语音]/[视频] 这类占位。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录

from wechatauto.db import WeChatDB

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "exports")

_XML_RE = re.compile(r"^\s*<\?xml|^\s*<msg[\s>]")
_SENDER_PREFIX_RE = re.compile(r"^(wxid_[0-9A-Za-z_\-]+|[0-9A-Za-z_\-]+@chatroom):\s*\n?")
_FROMUSER_RE = re.compile(r'fromusername\s*=\s*"([^"]+)"')
_TOUSER_RE = re.compile(r'tousername\s*=\s*"([^"]+)"')
_MD5_RE = re.compile(r'md5\s*=\s*"([0-9a-fA-F]{16,})"')


def clean(text: str, mtype: str) -> str:
    """去掉群前缀/媒体 XML，返回可读正文。"""
    t = (text or "").strip()
    if not t:
        return f"[{mtype}]"
    stripped = _SENDER_PREFIX_RE.sub("", t, count=1).strip()
    if stripped:
        t = stripped
    if _XML_RE.match(t):
        return f"[{mtype}]"
    return t


def read_all(db, user: str, page: int = 500, on_page=None) -> list:
    """按 offset 分页把全部消息读出来（升序）。"""
    out: list = []
    offset = 0
    while True:
        rows = db.get_messages(user, limit=page, offset=offset)
        if not rows:
            break
        out.extend(rows)
        offset += len(rows)
        if on_page:
            on_page(offset)
        if len(rows) < page:
            break
    out.sort(key=lambda r: (r.get("create_time") or 0, r.get("local_id") or 0))
    return out


def resolve_speakers(rows: list, me_wxid: str, other_wxid: str, db=None,
                     is_group: bool = False) -> tuple:
    """判定每条的说话人。返回 (说话人列表, 依据统计, id 映射)。

    `sender_id` 是**按登录账号编号**的，不能沿用「==2 就是自己」这种约定。
    这里按可靠性排序使用三条依据：

    1. **filehelper 反推自己**：这个会话必然全是自己发的 → 定出「我的 sender_id」；
    2. **一聊抵消法**：私聊里出现的 id 只有两个，不是我的那个就是对方
       （比"猜对方 id"可靠——另一个 id 一定是对方）；
    3. **媒体 XML 的 fromusername**（真实发送者，可用于交叉校验，且不受换号影响）。
    """
    who: list = [None] * len(rows)
    how: Counter = Counter()
    id_map: dict = {}

    ids = Counter(r.get("sender_id") for r in rows if r.get("sender_id") is not None)
    me_id = None

    # ---- 依据 1：filehelper 定自己 ----
    if db is not None:
        try:
            fids = [m.get("sender_id") for m in (db.get_messages("filehelper", limit=200) or [])
                    if m.get("sender_id") is not None]
            if fids:
                cand = Counter(fids).most_common(1)[0][0]
                if cand in ids:                  # 这个 id 在本会话里也出现过，才敢用
                    me_id = cand
                    how["filehelper定自己"] += 1
                else:
                    how["filehelper的id未出现在本会话"] += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [提示] filehelper 定自己失败：{type(e).__name__}: {e}")

    # ---- 依据 2：一聊抵消（只对私聊成立；群里其余 id 是别的群成员）----
    if me_id is not None:
        id_map[me_id] = "me"
        if not is_group:
            for sid in ids:
                if sid != me_id:
                    id_map[sid] = "other"
            how["一聊抵消（其余 id=对方）"] += 1
    elif len(ids) == 2 and not is_group:
        # 退路：用 XML 里出现过的 wxid 来判谁是自己
        for i, r in enumerate(rows):
            m = _FROMUSER_RE.search(str(r.get("content") or ""))
            if not m:
                continue
            sid = r.get("sender_id")
            w = m.group(1)
            if sid is None:
                continue
            if w == me_wxid:
                id_map[sid] = "me"
            elif w == other_wxid:
                id_map[sid] = "other"
        if "me" in id_map.values() or "other" in id_map.values():
            other_ids = [s for s in ids if s not in id_map]
            if len(id_map) == 1 and other_ids:
                known = list(id_map.values())[0]
                id_map[other_ids[0]] = "other" if known == "me" else "me"
            how["XML发送者反推"] += 1

    # ---- 依据 3：XML 交叉校验 / 兜底 ----
    for i, r in enumerate(rows):
        sid = r.get("sender_id")
        if sid in id_map:
            who[i] = id_map[sid]
        else:
            m = _FROMUSER_RE.search(str(r.get("content") or ""))
            if m:
                w = m.group(1)
                if w == me_wxid:
                    who[i], src = "me", "xml:fromusername=当前账号"
                elif w == other_wxid:
                    who[i], src = "other", "xml:fromusername=对方"
                else:
                    continue
                how[src] += 1
                if sid is not None:
                    id_map[sid] = who[i]
                continue
            who[i] = "unknown"
            how["未能判定"] += 1
            continue
        how["按id映射"] += 1

    # ---- 交叉校验：id 映射与 XML 真实发送者是否矛盾 ----
    conflicts = 0
    for i, r in enumerate(rows):
        if who[i] not in ("me", "other"):
            continue
        m = _FROMUSER_RE.search(str(r.get("content") or ""))
        if not m:
            continue
        w = m.group(1)
        if who[i] == "me" and w not in (me_wxid,):
            conflicts += 1
        elif who[i] == "other" and w == me_wxid:
            conflicts += 1
    if conflicts:
        print(f"  ⚠ 交叉校验：{conflicts} 条「说话人」与 XML 里的真实发送者不一致"
              f"（XML 比 sender_id 可信，说明 id 映射可能不对）")
    else:
        print("  ✅ 交叉校验：说话人判定与 XML 真实发送者无矛盾")

    return who, how, id_map


def detect_owner_from_xml(rows: list, cur_wxid: str, other_wxid: str) -> tuple:
    """从消息 XML 里找会话主人。返回 (主人wxid 或 None, 依据说明)。

    XML 的 ``fromusername`` 是**真实发送者**（比 sender_id 可信，且不受换号影响）。
    会话双方必有一个反复出现在 XML 里；另一个人就是「对方」（已知）。
    因此：反复出现的那个 wxid 若不是已知的对方，它就是主人。
    """
    names: Counter = Counter()
    for r in rows:
        m = _FROMUSER_RE.search(str(r.get("content") or ""))
        if m:
            names[m.group(1)] += 1
    if not names:
        return None, "这批消息里没有可用的 XML 发送者信息"
    top, n = names.most_common(1)[0]
    if top == other_wxid:
        # 主人从不作为 fromusername 出现 → 无法据此确定主人
        return None, f"XML 里只出现对方（{n} 条），主人无法据此确认"
    if top == cur_wxid:
        return top, f"XML 发送者里最多的是当前登录账号（{n} 条）"
    return top, (f"XML 发送者里最多的是 {top}（{n} 条），"
                 f"既不是当前登录账号也不是已知的对方 → 这段历史属于另一个账号")


def main():
    ap = argparse.ArgumentParser(description="导出会话的全部聊天记录（本地留档）")
    ap.add_argument("--who", default="", help="昵称 / 备注 / wxid")
    ap.add_argument("--format", default="md", choices=("md", "jsonl", "txt"))
    ap.add_argument("--out", default="", help="输出路径（默认 ./exports/…）")
    ap.add_argument("--list", action="store_true", help="只列出有消息的会话")
    ap.add_argument("--show-ids", action="store_true", help="产物里保留 wxid（默认打码）")
    ap.add_argument("--account", default="",
                    help="这段历史所属账号的 wxid（换号后导出旧记录时必须显式指定，"
                         "否则说话人可能标反）")
    ap.add_argument("--force", action="store_true", help="账号对不上也照导（说话人可能不准）")
    args = ap.parse_args()

    db = WeChatDB()
    if args.list or not args.who:
        chats = db.list_message_chats() or []
        chats.sort(key=lambda c: -(c.get("message_count") or 0))
        print(f"{'会话':<24} {'消息数':>8}   类型")
        for c in chats:
            n = (c.get("name") or c.get("username") or "")[:22]
            u = c.get("username") or ""
            print(f"{n:<24} {c.get('message_count') or 0:>8}   "
                  f"{'群' if '@chatroom' in u else '私聊'}")
        return 0

    user = args.who.strip()
    if not user.startswith("wxid_") and "@chatroom" not in user:
        hits = db.search_contact(user) or []
        if not hits:
            print(f"[错误] 找不到会话「{user}」（可以用 --list 看看有哪些）")
            return 1
        if len(hits) > 1:
            print(f"「{user}」命中多个联系人，请改用 wxid：")
            for h in hits[:8]:
                print("   ", h.get("nick_name"), "|", h.get("remark"))
            return 1
        user = hits[0]["username"]
    name = db.get_nickname(user) or user
    is_group = "@chatroom" in user

    me = (db.get_self_info() or {}).get("username", "") or ""
    print(f"导出会话：{name}（{'群' if is_group else '私聊'}）")

    rows = read_all(db, user, on_page=lambda n: print(f"  已读 {n} 条…", flush=True))
    print(f"  共 {len(rows)} 条")
    if not rows:
        print("[错误] 这个会话没有消息")
        return 1

    who, how, id_map = resolve_speakers(rows, me, user, db, is_group)
    print(f"  说话人判定依据：{dict(how)}")
    print(f"  sender_id → 人 映射：{id_map}")
    n_me = sum(1 for w in who if w == "me")
    n_other = sum(1 for w in who if w == "other")
    print(f"  我 {n_me} 条 / 对方 {n_other} 条 / 未判定 {len(who) - n_me - n_other} 条")

    # ---- 账号校验：sender_id 是按登录账号编号的，换号后旧记录会标反 ----
    owner, why = detect_owner_from_xml(rows, me, user)
    print(f"  会话主人推断：{owner or '未能确定'} —— {why}")
    if owner and owner != me:
        msg = (f"\n❌ 这段历史属于另一个微信账号（{owner}），而当前登录的是 {me}。\n"
               f"   sender_id 是按账号编号的，「自己/对方」会标反，导出的记录不可信。\n"
               f"   要导它：先登录那个账号再导，或加 --account {owner} 并接受启发式判定。")
        if not args.account and not args.force:
            print(msg)
            return 1
        if args.account and args.account != owner:
            print(f"\n❌ --account {args.account} 与推断出的主人 {owner} 不一致，先确认清楚。")
            return 1
        print(msg.replace("❌", "⚠").replace("要导它：", "已按 --account/--force 继续。要更准："))
        # 用 XML 的 wxid 直接定主人，绕开 filehelper 得来的（跨账号无效的）self_id
        for i, r in enumerate(rows):
            m = _FROMUSER_RE.search(str(r.get("content") or ""))
            if m and m.group(1) == owner:
                who[i] = "me"
            elif m and m.group(1) == user:
                who[i] = "other"
        print(f"  已改用 XML 发送者重判：我 {sum(1 for w in who if w == 'me')} 条 / "
              f"对方 {sum(1 for w in who if w == 'other')} 条")

    label_me, label_other = "我", name
    ts = lambda t: datetime.fromtimestamp(t or 0).strftime("%Y-%m-%d %H:%M")
    t0, t1 = rows[0].get("create_time"), rows[-1].get("create_time")

    os.makedirs(OUT_DIR, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", name)[:40]
    default_ext = {"md": "md", "txt": "txt", "jsonl": "jsonl"}[args.format]
    out_path = args.out or os.path.join(
        OUT_DIR, f"{safe}-{datetime.now().strftime('%Y%m%d')}.{default_ext}")

    if args.format == "jsonl":
        with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
            for i, r in enumerate(rows):
                rec = {
                    "time": ts(r.get("create_time")),
                    "speaker": label_me if who[i] == "me" else (
                        label_other if who[i] == "other" else "?"),
                    "type": r.get("type"),
                    "text": clean(str(r.get("content") or ""), r.get("type", "")),
                }
                if args.show_ids:
                    rec["sender_id"] = r.get("sender_id")
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    else:
        lines = [
            f"# 与「{name}」的聊天记录",
            "",
            f"- 共 **{len(rows)}** 条",
            f"- 时间范围：{ts(t0)} ～ {ts(t1)}",
            f"- 导出时间:{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"- 来源：微信本地数据库（只读导出，未改动任何微信文件）",
            f"- 媒体消息只保留类型占位，原始 XML 未包含",
            "",
            "---",
            "",
        ]
        cur_day = None
        for i, r in enumerate(rows):
            t = r.get("create_time") or 0
            day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
            if day != cur_day:
                cur_day = day
                lines += ["", f"## {day}", ""]
            speaker = label_me if who[i] == "me" else (
                label_other if who[i] == "other" else "?")
            body = clean(str(r.get("content") or ""), r.get("type", ""))
            if args.format == "txt":
                lines.append(f"[{ts(t)}] {speaker}: {body}")
            else:
                body_md = body.replace("\n", "  \n")
                lines.append(f"- **{ts(t)} {speaker}**: {body_md}")
        with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    size = os.path.getsize(out_path)
    print(f"\n✅ 已导出：{out_path}")
    print(f"   {len(rows)} 条，{size / 1024:.0f} KB")
    unknown = sum(1 for w in who if w == "unknown")
    if unknown:
        print(f"   ⚠ 有 {unknown} 条说话人未能判定（已标成 ?）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
