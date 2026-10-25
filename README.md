# Wechat-bot — 微信 4.x AI 自动回复机器人

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-API-green.svg)](https://platform.deepseek.com/)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

> 一套**微信 4.x 自动回复机器人**：私聊自动回复、群里被 @ 或**被叫名字/话题轮到自己**时回复、**群里有人问了能答的问题会主动接**（公开仓库默认关）、带 20 条上下文（**只取 60 分钟内的**）、开思考模式（`reasoning_effort=low`）、**说话口吻可定制**（人设写在 `config.json` 的 `system_prompt` 里）、**回复一句一条发**、**发送不碰剪贴板**、有联系人记忆档案、带安全红线、**多账号守卫**。

**最新更新（v1.2.1，2026-10-25）**：
- 🔧 修复私聊消息收不到 + 同一条消息被重复记录 4 次的问题

**v1.2.0（2026-10-24）**：
- ✅ **GUI 图形界面**（`bot_gui.py`，大字号清晰界面，工具栏在顶部）
- ✅ **文件 IPC**（`bot_state.py`）—— `bot_status.json` + `bot_events.jsonl`
- ✅ **依赖自愈**（`start_bot.bat` / `restart_bot.bat`）—— 启动前检查 wechatauto/psutil，缺则自动 pip install
- ✅ **参数调优** —— temperature=1.2, max_chars=220
- 🌸 **梦幻主题 GUI** —— 纯洁白 + 柔粉 + 冰晶蓝绿，无黑色圣洁风

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

### 核心功能
- **私聊自动回复**：收到私聊消息自动回复
- **群聊智能触发**：
  - 被 @ 时回复
  - 被叫名字时回复
  - 话题轮到自己时回复（上下文判断）
  - 刚回过的人 90 秒内再说一句也接
- **一句一条发**：多句回复拆成多条消息发送，更自然
- **上下文记忆**：带 20 条上下文（只取 60 分钟内的）
- **思考模式**：开启 `reasoning_effort=low`，平衡质量与速度
- **发送不碰剪贴板**：用 UIA 注入，不干扰用户剪贴板
- **联系人记忆档案**：自动记住联系人信息
- **多账号守卫**：检测到账号切换自动停止
- **发送确认**：发完回读数据库确认，失败自动重试
- **回调异常兜底**：`on_msg` 包 try/except，单条消息异常不会导致后续消息收不到

### GUI 图形界面（v1.2.0 新增）
- **状态栏**：微信在线 / 机器人运行 / 账号 / 模式 / 今日回复数 / 最后消息时间
- **对话流**：实时滚动显示收发消息，按会话分色
- **系统日志**：收发/警告/错误/启动等系统事件
- **工具栏**（顶部）：启动/停止/重启机器人、打开日志、打开配置、清空对话
- **文件 IPC**：`bot_status.json` + `bot_events.jsonl`，跨进程实时同步
- **高DPI适配**：`SetProcessDpiAwareness`，高分屏不模糊
- **大字号界面**：对话 13pt、状态栏 12pt、按钮 12pt 加粗，清晰易读
- **梦幻主题**：纯洁白基底 + 柔粉 + 冰晶蓝绿，无黑色圣洁风（♪♡ 装饰）

### 依赖自愈（v1.2.0 新增）
- 启动脚本自动检查依赖（`wechatauto` + `psutil`）
- 缺失时自动 `pip install`，无需手动干预

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
3. **微信 4.x**（已登录）
4. **DeepSeek API Key**（[申请地址](https://platform.deepseek.com/)）

### 安装

```powershell
# 1. 克隆仓库
git clone https://github.com/ChenQiuguo/Wechat-bot.git
cd Wechat-bot

# 2. 安装依赖
pip install wechatauto-replica==1.2.4.4 psutil

# 3. 配置 API Key
echo "sk-your-api-key" > apikey.txt

# 4. 启动 GUI（推荐）
python bot_gui.py

# 或启动命令行版本
python wx_bot.py
```

### Windows 用户（一键启动）

双击 `start_bot.bat`（含依赖自愈，首次会自动安装依赖）

---

## 架构

```
微信 4.x 客户端
  ↓ (UIA / 数据库读取)
wx_bot.py (主程序)
  ├── db.get_nickname() → 显示名解析
  ├── send_multi() → 逐条发送 + verify
  ├── BUS = BotStateBus() → 写 bot_status.json + bot_events.jsonl
  └── bot_replies.log
        ↓ (文件 IPC)
bot_gui.py (GUI，Tkinter)
  ├── BotStateReader → 读 bot_status.json + bot_events.jsonl
  ├── 每 200ms 轮询状态 + 事件
  └── 每 2s 检查 bot.lock + psutil 进程存活
```

### 核心文件

| 文件 | 用途 |
|---|---|
| `wx_bot.py` | 机器人主程序 |
| `bot_gui.py` | GUI 图形界面 |
| `bot_state.py` | 文件 IPC（状态 + 事件） |
| `config.json` | 配置文件 |
| `apikey.txt` | API Key（gitignored） |
| `start_bot.bat` | 启动脚本（含依赖自愈） |
| `restart_bot.bat` | 重启脚本（含依赖自愈） |
| `stop_bot.bat` | 停止脚本 |

---

## 配置

**配置文件：** `config.json`

### 关键参数

| 参数 | 值 | 说明 |
|---|---|---|
| `ai.temperature` | 1.2 | 输出随机性（越高越发散） |
| `ai.max_chars` | 220 | 回复最大长度（字符） |
| `ai.max_tokens` | 1200 | 生成上限（token） |
| `ai.reasoning_effort` | low | 思考深度（low/medium/high） |
| `ai.thinking` | true | 是否开启思考模式 |
| `ai.model` | deepseek-flash | 模型选择 |
| `trigger.private` | true | 私聊自动回复 |
| `trigger.group_at` | true | 群里 @ 或叫名字时回复 |
| `trigger.group_followup_seconds` | 90 | 刚回过的人 90 秒内再说一句也接 |
| `trigger.context_judge` | true | 判断话题是否轮到自己 |
| `limits.per_chat_cooldown` | 2 | 每个会话冷却 2 秒 |
| `limits.max_replies_per_day` | 300 | 每天最多 300 条 |
| `rhythm` | fast | 发送节奏 |

### 换 API Key

只改 `apikey.txt` 一行，重启 bot 生效：

```powershell
echo "sk-new-api-key" > apikey.txt
```

### 自定义口吻

`config.json` → `ai.system_prompt` 就是机器人的说话口吻（人设、规则、红线都在里面）。改成你自己的角色即可。

---

## 安全红线

**绝不能做的事：**
1. **绝不发送真实姓名** —— 聊天记录、日志、文档里都不能出现真人名
2. **绝不发送 API key** —— `apikey.txt` 已 gitignore
3. **绝不向非 filehelper 发测试消息** —— 自测只能发「文件传输助手」
4. **绝不绕过 verify** —— `send.verify=true` 保持开启
5. **绝不删除 `config.json` 的红线段** —— system_prompt 里的红线部分字节级不变

---

## 更新日志

完整更新日志见 [CHANGELOG.md](CHANGELOG.md)。

### v1.2.1 (2026-10-25)
- 🔧 修复私聊消息收不到 + 同一条消息被重复记录 4 次（`on_msg` 加 try/except 兜底）

### v1.2.0 (2026-10-24)
- ✅ GUI 图形界面（大字号、高DPI适配、工具栏在顶部、梦幻主题）
- ✅ 文件 IPC（`bot_status.json` + `bot_events.jsonl`）
- ✅ 依赖自愈（bat 启动前自动检查安装 wechatauto/psutil）
- 🔧 显示名解析修复（`_private_display_name()` / `_room_display_name()`）
- ⚡ 参数调优（temperature=1.2, max_chars=220）

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

---

## 许可

MIT License

---

## 致谢

- [wechatauto-replica](https://github.com/) —— 微信 4.x 自动化库
- [DeepSeek](https://platform.deepseek.com/) —— AI API

---

**最后更新：** 2026-10-25
**维护者：** ChenQiuguo
