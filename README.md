# Wechat-bot — 微信 4.x AI 自动回复机器人

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![MiMo](https://img.shields.io/badge/MiMo-API-green.svg)](https://platform.xiaomimimo.com/)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

> 一套**微信 4.x 自动回复机器人**：私聊自动回复、群里**只有被 @ 或喊名字才回**（可选：话题相关也接 / 有人提问时主动接）、**对方连发多条合并成一次回复**、**1M token 长上下文**、开思考模式（`reasoning_effort=low`）、内置**联网搜索**、**长期向量记忆**（mem0 + 本地嵌入）、**说话口吻可定制**（人设写在 `config.json` 的 `system_prompt` 里）、**回复一句一条发**、**发送不碰剪贴板**、带安全红线、**多账号守卫**、自带一套**本地 Web 监控台**。

**v1.3.1（2026-11-18）主要变化**：
- 🔧 **发送不再重复点会话**：目标会话已经打开就直接打字（微信 4.x 里点已高亮的会话行 = 关掉会话，输入框会跟着没）；单条失败时先「把焦点交回输入框」，还不行才点列表
- ✅ **连发合并**：对方一口气发两三条时，合并成一段话**只回一次**（`ai.batching`，默认 7 秒窗口、最多 8 条）
- ⚡ **提示词放宽**：去掉「5～15 字」「三句上限」一堆硬限制，长短跟着话题走；口吻优先于「像人打字」，角色味道和红线都保留

**v1.3.0（2026-11-17）**：
- 🖥️ **GUI 换成 Web 版**：Tkinter 退役，`bot_gui.py` 现在是本地 HTTP 服务（Edge 应用窗打开，随机端口复用），前端是自研「粉雾璃光」监控台——会话卡片 / 日期胶囊 / 连发聚簇 / ◈ 思考块折叠 / 日志分色 / `#chat:xxx` 深链 / JS 错误自诊断浮层
- 🤖 **AI 引擎切到小米 MiMo**（`mimo-v2.6-flash`，OpenAI 兼容接口，**参数形状与 DeepSeek 不同**），并开启 **web_search 联网搜索**
- 🧠 **上下文拉到 1M token**（`history_limit=5000` + `history_max_tokens=1000000`）
- 🧷 **长期记忆**：mem0 + fastembed + 本地 qdrant，按语义召回以前聊过的片段（默认只走本地嵌入，不额外调模型）
- 🔕 **群里只回 @ / 喊名字**：`trigger.group_at_only=true`，话题判定与「接着聊」都不走（不花判定 token）
- 🏷️ **会话别名自愈**：昵称瞬时解析失败不再让面板里的会话裂成两条（`chat_alias.py`）
- 🎨 **图标 / 头像可换**：`webui/icon/make_icons.py` 一键从一张方图生成整套图标

**此前版本**：v1.2.x 见 [CHANGELOG.md](CHANGELOG.md)（GUI + 文件 IPC + 依赖自愈 + 私聊收不到修复）。

---

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [架构](#架构)
- [配置](#配置)
- [安全红线](#安全红线)
- [更新日志](#更新日志)
- [贡献](#贡献)
- [许可](#许可)

---

## 特性

### 触发与回复
- **私聊自动回复**：收到私聊消息自动回复（`trigger.private`）
- **群聊触发**（三档，按需开）：
  - **只认 @ / 名字**（默认，`trigger.group_at_only=true`）：被 @，或正文出现你的昵称才回；话题判定、接着聊都不走
  - **话题相关也接**（`group_at_only=false` + `context_judge=true`）：拿不准时不回，`context_lookback` 条上文交给轻量判定
  - **接着聊**（`group_followup_seconds=90`）：她刚回过的人 90 秒内再说一句也接
- **有人提问时主动接**（`unsolicited.enabled`，**默认关**）：只接群里「确实答得上来的问题」，私聊永不主动
- **一句话一条发**：多句回复按句子拆成多条消息（最多 4 条），更像人在打字
- **连发合并**：对方一口气发两三条，等一个短窗口合成一段话、**只回一次**（`ai.batching`，关掉即退回首条即回）
- **思考模式**：`ai.thinking=true` + `reasoning_effort=low`（MiMo 合法值 `none/low/medium/high`）
- **联网搜索**：`ai.web_search.enabled`，模型判断需要时自己搜（省 token：`force_search=false`）

### 记忆
- **上下文**：`history_limit` 条 + `history_max_tokens` 预算（默认 1M，按日预算从新往旧截，至少留 3 条）；`history_max_age_minutes` 可给上下文加时间窗
- **联系人记忆档案**：每个会话一份 JSON（关系、称呼、城市、爱好、约定…），回复时注入
- **长期向量记忆**：mem0 + 本地嵌入（`BAAI/bge-small-zh-v1.5`）+ 本地 qdrant，语义召回「聊过什么」；`infer=false` 不额外调模型，依赖缺失自动降级

### 发送
- **一句一条**、**只打开一次会话连发多条**
- **会话已经打开就不再点**：发送前先读「当前会话」，是目标会话就直接打字（微信 4.x 里点已高亮的会话行等于关闭它）；单条失败时先「把焦点交回输入框」重试，还不行才点会话列表
- **发送不碰剪贴板**：`kb_input.py` 用 `SendInput` + `KEYEVENTF_UNICODE` 逐字注入，校验输入框内容一致；失败可回退剪贴板（`input.fallback_clipboard`，默认关）
- **发送确认**：发完回读数据库确认，确认不到才算失败并重试（有落库水位门槛，不会重发两遍）
- **节流**：每会话冷却 + 每分钟/每天上限；另有上游库节奏层（`rhythm=fast`）

### 监控台（Web 版）
- 本地 HTTP 服务 + Edge 应用窗（无地址栏）；随机端口写入 `gui_port.txt`，再开一次复用已在跑的服务
- 会话列表（可筛选）/ 气泡流 / ◈ 思考块折叠 / 连发聚簇 / 日期胶囊 / 日志分色三视图
- 顶部工具栏：启动 / 停止 / 重启 / 打开日志 / 打开配置 / 关闭界面（**关窗不杀机器人**）
- 文件 IPC：`bot_status.json` + `bot_events.jsonl`，跨进程实时同步
- 开界面 0.5 秒后**自动拉起机器人**（已开则跳过）；`python bot_gui.py --attach` 只开界面

### 稳健性
- **多账号守卫**：启动锁定账号（`config.account` 钉号，留空则自动选），主循环每 60 秒核对，换号立刻停手
- **单实例锁**：`bot.lock` 记录 PID + 进程启动时间，PID 复用不会误判；陈旧锁自动回收
- **回调异常兜底**：`on_msg` 包 try/except，单条消息异常不会拖垮后续收取
- **不回的每个原因都进日志**：跳过原因 / 发送失败 / AI 调用失败 / 微信掉线

### 安全特性
- **绝不发送真实姓名**：聊天记录、日志、文档里都不能出现真人名
- **绝不发送 API key**：`apikey.txt` 已 gitignore
- **绝不向非 filehelper 发测试消息**：自测只能发「文件传输助手」
- **绝不绕过 verify**：`send.verify=true` 保持开启
- **绝不删除红线段**：`config.json` 的红线部分字节级不变

---

## 快速开始

### 前置要求

1. **Windows 10/11**
2. **Python 3.10+**
3. **微信 4.x**（已登录并保持在线，可最小化）
4. **一个 OpenAI 兼容的模型 API**：默认走小米 MiMo（[控制台](https://platform.xiaomimimo.com/)），换成 DeepSeek / 其它兼容服务见下方「换模型」

### 安装

```powershell
# 1. 克隆仓库
git clone https://github.com/ChenQiuguo/Wechat-bot.git
cd Wechat-bot

# 2. 安装依赖（核心两条；长期记忆/图标生成是可选的，见 requirements.txt）
pip install wechatauto-replica>=1.2.9 psutil

# 3. 配置 API Key（一行一个，第一个 sk- 开头的生效）
echo sk-your-api-key > apikey.txt

# 4. （可选）长期向量记忆
pip install mem0ai fastembed

# 5. 启动监控台（推荐，会自动拉起机器人）
python bot_gui.py

# 或直接跑命令行版
python wx_bot.py
```

> 没装 `wechatauto-replica` 或 `psutil` 时，`start_bot.bat` 会在启动前自动 `pip install`（可换镜像）。

### Windows 一键启动

- `start_bot.bat`：启动机器人（含依赖自愈，静默后台）
- `restart_bot.bat` / `stop_bot.bat`：重启 / 停止
- `python bot_gui.py`：打开监控台（Web 版，自动启动机器人）
- `python bot_gui.py --attach`：只开界面，不动已在跑的机器人

### 换模型（provider）

`config.json` 的 `ai` 段：

```jsonc
"provider": "mimo",                            // mimo | deepseek | openai 兼容
"base_url": "https://api.xiaomimimo.com/v1",
"model": "mimo-v2.6-flash",
"thinking": true,
"reasoning_effort": "low"                      // MiMo 合法值 none/low/medium/high
```

⚠️ **参数形状不同**：MiMo 用 `max_completion_tokens`（不是 `max_tokens`）、`thinking: {type: enabled|disabled}`，代码按 `provider` 自动翻译；照抄 DeepSeek 的请求体直接打 MiMo 会 **400**。
⚠️ 走 MiMo 的联网搜索需要在模型控制台先启用搜索插件（有几分钟缓存）。

---

## 架构

```
微信 4.x 客户端
  ↓ (读消息：本地数据库解密；发消息：UIA)
wx_bot.py (主程序)
  ├── wx_push.Listener／SenderResolver   → 监听 + 认人（群消息用正文前缀 wxid + 本群名单）
  ├── ReplyPolicy.check()               → 私聊 / 群 @ / 喊名字 / 话题判定 / 接着聊
  ├── ai 调用（MiMo，OpenAI 兼容）        → 生成回复（+ web_search）
  ├── memory_store.py / memory_vec.py   → 档案记忆 + 向量记忆召回
  ├── kb_input.py                       → 不碰剪贴板的逐字注入
  ├── send_multi + verify               → 连发 + 回读数据库确认
  └── BUS = BotStateBus()               → bot_status.json + bot_events.jsonl + bot_replies.log
        ↓ (文件 IPC)
bot_gui.py (本地 HTTP 服务 + Edge 应用窗)
  ├── /api/status /api/events /api/log   → 轮询状态与事件
  ├── /api/action                        → 启动 / 停止 / 重启机器人
  └── webui/                             → index.html + css/ui.css + js/app.js + icon/
```

### 核心文件

| 文件 | 用途 |
|---|---|
| `wx_bot.py` | 机器人主程序（监听 / 判断 / 生成 / 发送 / 记忆） |
| `wx_push.py` | 纯监听推送 + `SenderResolver`（认人逻辑） |
| `bot_gui.py` | Web 监控台（本地 HTTP 服务） |
| `webui/` | 前端：`index.html`、`css/ui.css`、`js/app.js`、`icon/` |
| `bot_state.py` | 文件 IPC（状态 + 事件） |
| `memory_store.py` | 联系人档案记忆 |
| `memory_vec.py` | 长期向量记忆（mem0 + fastembed + qdrant） |
| `chat_alias.py` | 会话别名表（裸 ID → 显示名，启动自愈） |
| `kb_input.py` | 不碰剪贴板的 Unicode 注入 |
| `image_in.py` | 看图（默认关，见「配置 → 看图」） |
| `config.json` | 配置文件（所有可调项 + `system_prompt`） |
| `tools/` | 预览、测量、探针、自查脚本 |

---

## 配置

**配置文件：** `config.json`（**改完必须重启机器人**，配置不是热加载）

### 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `account` | `""` | 账号钉号（wxid），留空则自动选当前登录账号 |
| `ai.provider` | `mimo` | `mimo` / `deepseek` / 其它 OpenAI 兼容 |
| `ai.model` | `mimo-v2.6-flash` | 模型 |
| `ai.thinking` | `true` | 是否开思考（开着时 `temperature` 会被 API 强制成 1.0） |
| `ai.reasoning_effort` | `low` | MiMo：`none/low/medium/high` |
| `ai.max_tokens` | `1200` | 生成上限；开思考时别太小，否则思维链吃光会返回空串 |
| `ai.max_chars` | `220` | 回复最大长度 |
| `ai.temperature` | `1.2` | 输出随机性（仅 `thinking=false` 时生效） |
| `ai.web_search.enabled` | `true` | 联网搜索（`max_keyword` / `limit` / `force_search`） |
| `ai.history_limit` | `5000` | 每次带多少条聊天记录 |
| `ai.history_max_tokens` | `1000000` | 上下文 token 预算（从新往旧截，至少留 3 条） |
| `ai.history_max_age_minutes` | `0` | 只带这么久以内的上下文（0 = 不限） |
| `ai.multi_send.mode` | `sentence` | `sentence` 一句一条 / `separator` 只按 `\|\|\|` 切 / 关 |
| `ai.multi_send.max_messages` | `4` | 一次最多发几条 |
| `ai.batching.enabled` | `true` | 连发合并：同一人短窗口内连发多条 → 合成一次回复 |
| `ai.batching.wait_ms` | `7000` | 合并窗口（毫秒，从最近一条算起）；调大会等更久但合并得更多 |
| `ai.batching.max_messages` | `8` | 一批最多合并几条、回复条数上限也按它放宽 |
| `ai.memory.max_facts` | `25` | 每个联系人的事实条数上限 |
| `ai.memory.vector.enabled` | `true` | 长期向量记忆（依赖 mem0 + fastembed） |
| `ai.memory.vector.infer` | `false` | `true` = 让 mem0 额外调模型做事实抽取（更贵） |
| `trigger.private` | `true` | 私聊自动回复 |
| `trigger.group_at` | `true` | 群里 @ / 喊名字才回 |
| `trigger.group_at_only` | `true` | 只认 @ / 名字，话题判定与接着聊都不走 |
| `trigger.group_followup_seconds` | `0` | 「接着聊」窗口（需 `group_at_only=false`） |
| `trigger.name_hits` | `[]` | 正文里出现这些名字也算叫你；留空自动用本账号昵称 |
| `trigger.context_judge` | `true` | 话题相关时用轻量模型判定该不该回 |
| `trigger.types` | `["文本"]` | 处理哪些消息类型（看图见下） |
| `trigger.max_age_seconds` | `120` | 太旧的消息不再追 |
| `unsolicited.enabled` | `false` | 群里有人提问时主动接 |
| `proactive.enabled` | `false` | 主动开场（长时间安静后主动找人） |
| `limits.per_chat_cooldown` | `2` | 每个会话冷却秒数 |
| `limits.max_replies_per_minute` | `20` | 每分钟上限 |
| `limits.max_replies_per_day` | `300` | 每天上限 |
| `send.verify` | `true` | 发完回读数据库确认 |
| `input.method` | `unicode` | `unicode` 只键盘注入 / `auto` 允许回退剪贴板 / `clipboard` 不打补丁 |
| `rhythm` | `fast` | 上游库节奏档（`fast` / `natural`） |

### 看图（默认关）

1. `trigger.types` 加回 `"图片"`；
2. `ai.image.enabled = true`；
3. 想连群图一起接，再开 `trigger.group_image`（建议同时设 `ai.image.group_recent_seconds`）。

代码在 `image_in.py`：只读本机 `.dat` 缓存解密，不驱动界面，图片以 base64 内联进请求。

### 换 API Key

只改 `apikey.txt` 一行，重启生效：

```powershell
echo sk-new-api-key > apikey.txt
```

### 自定义口吻

`config.json` → `ai.system_prompt` 就是机器人的说话口吻（人设、规则、红线都在里面）。改成你自己的角色即可；改完用 `python tools/preview_replies.py` 预览一批典型消息的回复。

### 换图标 / 头像

把一张正方形图存成 `webui/icon/avatar-src.jpg`，然后：

```powershell
python webui/icon/make_icons.py
```

会重新生成 `favicon.ico` + `icon-*.png` + `avatar.png`（16–48px 会裁到脸部并锐化）。换完记得把 `index.html` 里 favicon 的 `?v=` 版本号加一，否则浏览器会用缓存的旧图标。

---

## 安全红线

**绝不能做的事：**
1. **绝不发送真实姓名** —— 聊天记录、日志、文档里都不能出现真人名
2. **绝不发送 API key** —— `apikey.txt` 已 gitignore
3. **绝不向非 filehelper 发测试消息** —— 自测只能发「文件传输助手」
4. **绝不绕过 verify** —— `send.verify=true` 保持开启
5. **绝不删除 `config.json` 的红线段** —— system_prompt 里的红线部分字节级不变

> 这类工具本质上是**自动化操作你自己的微信账号**，请自己评估风控风险、保持低频使用；项目不对封号负责。

---

## 更新日志

完整更新日志见 [CHANGELOG.md](CHANGELOG.md)。

### v1.3.1 (2026-11-18)
- 🔧 修发送时重复点会话把输入框点掉；单条失败先重设输入框焦点
- ✅ 连发合并（`ai.batching`）：对方一口气发几条只回一次
- ⚡ 提示词放宽：长短跟着话题走、口吻优先于「像人打字」，红线未动

### v1.3.0 (2026-11-17)
- 🖥️ GUI 换成 Web 版（本地 HTTP 服务 + Edge 应用窗 + 自研「粉雾璃光」前端），Tkinter 退役
- 🤖 AI 引擎切到小米 MiMo（`mimo-v2.6-flash`），开 web_search 联网搜索
- 🧠 上下文拉到 1M token（`history_limit=5000` + `history_max_tokens=1000000`）
- 🧷 长期向量记忆（mem0 + fastembed + 本地 qdrant）
- 🔕 群里只回 @ / 喊名字（`group_at_only`），话题判定与接着聊短路
- 🏷️ 会话别名自愈（`chat_alias.py`），修「同一会话在面板裂成两条」
- ⬆️ 底层库升级到 `wechatauto-replica 1.2.9`；账号钉号 `config.account`
- 🎨 图标/头像可一键生成（`webui/icon/make_icons.py`）
- 🔧 修「昵称带句号被切成孤儿消息」；回复不再用对方名字开场

### v1.2.1 (2026-10-25)
- 🔧 修复私聊消息收不到 + 同一条消息被重复记录 4 次（`on_msg` 加 try/except 兜底）

### v1.2.0 (2026-10-24)
- ✅ GUI 图形界面、文件 IPC、依赖自愈、显示名解析修复、参数调优（temperature=1.2, max_chars=220）

### v1.1.0 (2026-10-11)
- 🔧 运行时重置后依赖自愈 + `apikey.txt` 支持

### v1.0.1 (2026-10-07)
- 🔧 发送后回读数据库确认，修好「只输入不发送」和「重复发送」

### v1.0.0 (2026-10-06)
- ✅ 按人指定关系（`ai.relations` 机制）

---

## 贡献

欢迎提交 Issue 和 Pull Request！

**开发环境：**
- Windows 10/11
- Python 3.10+
- 微信 4.x

**测试原则：**
- 只向「文件传输助手」(filehelper) 发测试消息
- 绝不向真人发测试
- 测试前确认 `dry_run` 或 `send.verify=true`
- `tools/` 与根目录的自查脚本大多只读、不发送（如 `tools/test_group_at_only.py`）

---

## 许可

MIT License（`webui/` 前端设计与类名体系参考了 [dot-chat](https://gitee.com/lidaoyang/dot-chat)，MulanPSL-2.0）

---

## 致谢

- [wechatauto-replica](https://pypi.org/project/wechatauto-replica/) —— 微信 4.x 自动化库
- [mem0](https://github.com/mem0ai/mem0) + [fastembed](https://github.com/qdrant/fastembed) —— 长期记忆与本地嵌入
- [dot-chat](https://gitee.com/lidaoyang/dot-chat) —— 前端布局参考

---

**最后更新：** 2026-11-17
**维护者：** ChenQiuguo
