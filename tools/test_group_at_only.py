# -*- coding: utf-8 -*-
"""一次性自查：「群里只有被 @ 或喊名字才接」的规则层 + UnsolicitedPolicy + judge 消歧。

不发送、不碰微信数据库（假 resolver / 假 db / 不打模型 API，judge 用桩函数）。
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"$WECHAT_BOT_DIR")

import wx_bot


class FakeResolver:
    def roster(self, chat):
        # 群成员名单：只有阿伟（没有任何名字包含「你的昵称」的人）
        return {"wxid_aw": "阿伟"}


class FakeDB:
    def get_self_info(self):
        return {"nick_name": "你的昵称"}


def mk_cfg(**trig_over):
    cfg = {
        "trigger": {
            "private": True, "group_at": True, "group_at_only": True,
            "group_followup_seconds": 0, "at_names": [], "name_hits": ["你的昵称"],
            "context_judge": True, "context_lookback": 8, "judge_cooldown": 8,
            "types": ["文本"], "group_image": False, "max_age_seconds": 120,
        },
        "limits": {"per_chat_cooldown": 0, "max_replies_per_minute": 20, "max_replies_per_day": 300},
        "skip_chats": [], "skip_chat_names": [],
        "ai": {"image": {}},
        "unsolicited": {"enabled": True, "max_age_seconds": 180, "per_chat_cooldown": 0,
                        "max_per_minute": 2, "max_per_day": 10},
    }
    cfg["trigger"].update(trig_over)
    return cfg


def payload(text, is_group=True, sender="阿伟"):
    return {
        "is_self": False, "is_group": is_group, "type": "文本",
        "chat_username": "123@chatroom" if is_group else "wxid_aw",
        "chat_name": "测试群" if is_group else "阿伟",
        "sender": sender, "sender_wxid": "wxid_aw",
        "content": text, "timestamp": time.time(), "time": "00:00:00",
    }


judge_calls = []


def fake_judge(cfg, names, chat, sender, text, recent, key):
    judge_calls.append(text)
    return True          # 就算判定说「是的，跟我有关」，也不能放行


def fake_can_answer(*a, **kw):
    raise AssertionError("主动接话关着的时候，绝不该调用 can_answer_question（省 token）")


wx_bot.judge_targeted = fake_judge
wx_bot.can_answer_question = fake_can_answer

results = []


def case(name, got, want):
    ok = (got == want)
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name} → {got}（期望 {want}）")


# ---------- 规则层 ----------
cfg = mk_cfg()
pol = wx_bot.ReplyPolicy(cfg, FakeResolver(), FakeDB())

case("群里 @ 我 → 回", pol.check(payload("@你的昵称 在吗"))[0], True)
case("群里直接喊名字 → 回", pol.check(payload("你的昵称 在不在"))[0], True)
case("群里喊名字但不带 @ → 回", pol.check(payload("刚你的昵称说的那个你办了没"))[0], True)

pol2 = wx_bot.ReplyPolicy(mk_cfg(), FakeResolver(), FakeDB())
case("群里别人 @ 别人 → 不回", pol2.check(payload("@小明 你来一下"))[0], False)
case("群里问句但没点我 → 不回", pol2.check(payload("有人知道这周更新啥吗"))[0], False)
case("群里刷了「？？？」没点我 → 不回", pol2.check(payload("？？？"))[0], False)

# 判定层：哪怕 judge_targeted 说「是」，group_at_only 下也不许放行
pol3 = wx_bot.ReplyPolicy(mk_cfg(context_judge=True,
                                 group_followup_seconds=999), FakeResolver(), FakeDB())
case("判定/接着聊窗口都开着，但没点名 → 不回", pol3.check(payload("他在不在，我找他有点事"))[0], False)
case("这种情况下一次判定都没跑（省 token）", len(judge_calls), 0)

# 老行为可回退（judge_cooldown=0 才会跑判定；换一个新群避免任何缓存）
pol4 = wx_bot.ReplyPolicy(mk_cfg(group_at_only=False, context_judge=True, judge_cooldown=0),
                          FakeResolver(), FakeDB())
pol4.api_key = "dummy"
p4 = payload("刚你的昵称说的那个事你办了没")
p4["chat_username"] = "999@chatroom"
case("group_at_only=false 退回老行为（判定说有关就回）", pol4.check(p4)[0], True)

# 私聊不受影响
pol5 = wx_bot.ReplyPolicy(mk_cfg(), FakeResolver(), FakeDB())
case("私聊照常回", pol5.check(payload("在吗", is_group=False))[0], True)

# ---------- 主动接话开关 ----------
cfg_uns = mk_cfg()
cfg_uns["unsolicited"]["enabled"] = False
uns = wx_bot.UnsolicitedPolicy(cfg_uns, FakeDB())
ok, why = uns.check(payload("帮我看看这个报错怎么修"), [])
case("主动接话关掉 → 群里问问题也不接", ok, False)
print(f"       原因：{why}")

print("\n==============================================")
print(f"结果：{sum(results)}/{len(results)} 通过")
