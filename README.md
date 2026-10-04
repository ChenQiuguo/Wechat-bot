# 微信 AI 自动回复机器人

基于 [wechatauto-replica](https://pypi.org/project/wechatauto-replica/) 的微信 4.x 自动回复机器人：

- **私聊**收到消息 → AI 生成回复并发送
- **群聊**里有人 **@我** → 同上
- 自己发的消息永不回复（防自杀循环）

读消息走**本地数据库解密**（只读，不注入 DLL、不改微信文件），发消息走 **UIA 自动化**。

> ⚠️ **免责声明**：这属于违反微信用户协议的非官方操作，**存在风控/封号风险**。
> 本项目只做只读监听 + 低频发送，但仍不等于零风险。请自行评估，建议用小号测试。
> 有统计显示 Hook 类方案约 85% 的用户被封过号；本项目不注入进程，风险低于 hook，
> 但风控是概率问题，没有绝对安全。

---

## 功能

| 能力 | 说明 |
|---|---|
| 私聊自动回复 | AI 生成，带聊天上下文 |
| 群聊 @ 自动回复 | 自动识别 `@本账号昵称` |
| **带上下文的回复** | 每次把该会话最近 8 条消息一起给模型，能接住玩笑和梗 |
| **会话列表点击发送** | 实测比库默认的搜索框路径**快一倍**（5.3s → 2.7s） |
| 冷却与限流 | 每会话冷却、每分钟/每天上限，防刷屏防风控 |
| 防重复启动 | 单实例锁，陈旧锁自动回收 |
| 回复风格可配置 | 默认傲娇，随便改 prompt |
| 安全边界 | 不许替你答应借钱/邀约、不许编造行程与权限 |

## 环境要求

- Windows 10/11
- 微信 PC 客户端 **4.x**，**已登录并保持在线**（4.1.9.35 实测可用）
- Python **3.9+**（3.12 实测）
- 一个 OpenAI 兼容的大模型 API（默认 DeepSeek）

## 安装

```bat
pip install -r requirements.txt
```

## 配置

编辑 `config.json`：

```json
{
  "ai": {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "history_limit": 8,
    "system_prompt": "..."
  },
  "trigger": {
    "private": true,
    "group_at": true,
    "at_names": [],
    "types": ["文本"],
    "max_age_seconds": 120
  },
  "limits": {
    "per_chat_cooldown": 2,
    "max_replies_per_minute": 20,
    "max_replies_per_day": 300
  },
  "rhythm": "fast"
}
```

`at_names` 留空即可——运行时会**自动把本账号昵称加进去**，不用写死。

**API Key** 按顺序从这些位置找（都不会被写进仓库）：

1. 环境变量 `DEEPSEEK_API_KEY`
2. 本项目目录下的 `apikey.txt`（可直接写一行裸 key）
3. `~/.dsh/.credentials.yaml`（DeepSeek Harness 的凭据文件，可选）

## 使用

```bat
python wx_bot.py                       :: 正式运行
python wx_bot.py --dry-run             :: 只生成回复不发送（安全预览）
python wx_bot.py --test-reply "你好"    :: 走完整 AI+发送流程（发到文件传输助手）
python wx_push.py --webhook http://...  :: 只监听并推送 JSON（不回复）
```

Windows 上双击 `start_bot.bat` 即可（会自动找 Python、检查微信是否在运行、检查依赖）。

## 工具

```bat
python doctor.py                        :: 自检：密钥提取/解密/会话/消息读取
python tools/probe_self_id.py           :: 推导本机「自己」的 sender_id
python tools/verify_payload.py          :: 用真实历史消息验证发送者解析与正文清洗
python tools/preview_replies.py         :: 预览一批典型/刁钻消息的 AI 回复
python tools/measure_tokens.py          :: 实测单次回复的 token 消耗与成本
python tools/bench_send.py 3            :: 对比两种发送路径的耗时
```

---

## 实现要点与踩坑记录

这部分是折腾出来的经验，对想基于同类库做微信自动化的人应该有用。

### 1. 「自己发的」不能按 `sender_id == 2` 判断

上游库在 `db.py` 里写死了「`sender_id == 2` 就是自己」，并据此跳过用户名解析。但
`real_sender_id` 是 `message_resource.db` 里 `SenderName2Id` 表的 rowid，**每台机器不一样**。
本机实测：自身 rowid 是 **3**，而 `id=2` 其实是一位好友。

照库的写法会出两个错：群里那位好友的消息被**误判成你自己发的**，而且名字解析被跳过
（正文里的 `wxid_xxx:` 前缀都没剥掉）。

**解决**：从「文件传输助手」反推自身 rowid——那里的消息必然全是自己发的：

```python
msgs = db.get_messages("filehelper", limit=200)
self_id = Counter(m["sender_id"] for m in msgs).most_common(1)[0][0]
```

并且**绕过库里的 `!= 2` 判断**，直接用 `SenderName2Id` 映射表解析发送者。

### 2. 发送路径：会话列表点击比搜索框快一倍（实测）

库默认发送流程是「打开搜索框 → 输入昵称 → 等搜索结果 → 点结果 → 输入正文 → 发送」。
而刚给你发消息的人**就在左侧会话列表顶部**，直接点它更快。

| 路径 | 平均耗时 | 打开会话 |
|---|---|---|
| 搜索框（库默认） | 5.3s | ~3.5s |
| **UIA 会话列表点击** | **2.7s** | **~1.3s** |

实现见 `open_chat_via_session_list()`：枚举 `session_list`（`mmui::XTableView`）下的
`ListItemControl`，按首行会话名精确匹配、点矩形中心、用 `current_chat()` 复核；
匹配不到/同名多个/点击失败时**自动回退搜索框**。

> **关键坑**：不要用库自带的 OCR 侧栏路径（`guia.find_session`）做这件事。它靠屏幕截图 +
> 文字识别，实测要 **17–24 秒且打开成功率 0/3**。原因之一是 Windows 会把**最小化**的窗口
> 放到 `(-32000, -32000)`，截图类路径完全失效。**必须走 UIA**。

### 3. 库的 `verify=True` 会误报失败

`send_msg(..., verify=True)` 靠回读数据库确认发送。但微信写盘有延迟（实测约 2 秒），
库的检查窗口太短，会报「消息已操作发送，但数据库未确认」——**消息其实已经发出去了**。

**解决**：用 `verify=False`，需要时自己延迟复查数据库。

### 4. 单实例锁（很容易踩）

两个进程同时用这个库会抢解密缓存，第二个必然报：

```
PermissionError: [WinError 5] 拒绝访问: ...\.tmp -> ...\contact__contact.db
```

本项目用 pid 锁文件实现单实例，并且**进程被强杀留下的陈旧锁会自动回收**（比对 pid 是否存活）。

### 5. `.bat` 文件必须是纯 ASCII + CRLF

cmd 按**系统代码页**读取批处理文件，不认 UTF-8。写成 UTF-8 中文会让 cmd 把 `echo` 语句
当成命令执行，报出这种莫名其妙的错：

```
'格）' is not recognized as an internal or external command
```

所以批处理本身只用英文，中文提示统一交给 Python 输出（`chcp 65001` 下正常）。
另外 LF-only 换行也可能让 `goto` 标签失效，所以用 CRLF。

### 6. 回复质量：光改 prompt 不够，得给上下文

最初每次只把孤立的一句话丢给模型，结果：「你要@我」被误解、动不动就"我先确认一下"兜底、
把自己当成第三方（"我得先问下他本人"）。

**解决**：每次调用带上该会话最近 8 条消息，并明确告知「你就是昵称本人」。之后：
接得住玩笑、明白话里有话、不再 @ 自己。

### 7. 安全边界（防止它替你闯祸）

prompt 里写死了这些规则，实测有效：

- **借钱/转账/担保、答应或拒绝邀约、承诺时间、透露隐私** → 一律回「这事我确认下再回你」，
  且**不许编理由推脱**（早期版本会回「我最近也紧巴巴的，实在匀不出来」——等于替你拒了别人）
- **不许编造有后果的事实**：行程、当前状态（在忙/在路上）、承诺、金额、权限
  （踢人/封禁/举报/删号）。早期版本回怼网友时说「你再说这种话我就直接踢了」——**而你并不是群主**
- **必须与聊天记录一致**：上文说自己还卡着没过，就不能说已经过了

### 8. Token 成本（实测）

只有「生成回复」花钱：监听、规则判断、冷却跳过、@ 判定全是本地操作，**0 token**。

实测一次回复（`deepseek-chat`，实际由 `deepseek-flash` 提供服务）：

| 项目 | 数量 |
|---|---|
| 输入（缓存命中） | 512 tokens ← 固定的 system_prompt |
| 输入（未命中） | 162 tokens ← 身份行 + 8 条聊天记录 + 消息 |
| 输出 | 12 tokens |
| **合计** | **~686 tokens** |

按 DeepSeek 官方价折算约 **0.044 分/条**（高峰时段），1000 条 ≈ 0.44 元；空闲时段减半。
按每日 300 条上限，最坏一天不到 0.13 元。

> 系统提示词会被**前缀缓存**命中（命中价约为未命中的 1/50），所以那个很长的 prompt 几乎不花钱。
> 真正的变量成本是那 8 条聊天记录——调小 `history_limit` 是主要省钱手段。

### 9. 真正的发送频率天花板在库里

`config.json` 的冷却只是第一道闸。上游库自带的「拟人节奏层」对每个对外写动作独立限速，
`fast` 档实测参数：写动作最小间隔 **0.6–1.4s**、**120 秒内最多 20 次**（≈10 条/分钟）、
撞上限后冷却 4–9 秒。所以**把冷却调到 2 秒也不会真的 2 秒一条**。

这是作者为规避风控刻意加的（他的账号在激进自动化后被要求重新登录过），不建议关掉。

---

## 致谢

- [wechatauto-replica](https://pypi.org/project/wechatauto-replica/) —— 微信 4.x 的数据库解密与 UIA 驱动，本项目站在它上面
- [DeepSeek](https://platform.deepseek.com/) —— 默认回复模型

## License

MIT
