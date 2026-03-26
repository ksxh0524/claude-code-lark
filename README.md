# Claude Code Lark Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

飞书/Lark 机器人，让你通过聊天远程使用 [Claude Code](https://claude.ai/code)。随时随地与 Claude 讨论你的代码项目——无需终端命令。

> 📝 **Fork 自 [claude-code-telegram](https://github.com/nickg/claude-code-telegram)**，适配飞书/Lark 平台。

## ✨ 功能特性

- 🤖 **自然对话** - 用自然语言让 Claude 分析、编辑、解释代码
- 🔄 **流式卡片** - 实时显示 Claude 处理进度和内容
- ⏱️ **计时器** - 显示处理耗时
- ⏹️ **中断按钮** - 随时停止长时间运行的任务
- 💾 **会话持久化** - 自动保存每个项目的对话上下文
- 🔒 **安全隔离** - 目录沙箱、用户白名单、审计日志
- 📁 **文件处理** - 支持文件上传、图片分析
- 🌐 **多平台** - 飞书/Lark (主要) + Telegram (可选)

## 📸 效果展示

```
你: 帮我在 src/api.py 添加错误处理

Bot: ⏱ 处理中... 3s
     📖 Read: src/api.py
     💬 我来分析代码并添加错误处理...
     ✏️ Edit: src/api.py

Bot: ✅ 完成 · 8.2s

     已添加 try-except 错误处理...
```

## 🚀 快速开始

### 1. 前置要求

- **Python 3.11+**
- **Claude Code CLI** - [安装指南](https://claude.ai/code)
- **飞书/Lark 应用** - 从 [开放平台](https://open.feishu.cn/) 创建

### 2. 安装

```bash
git clone https://github.com/ksxh0524/claude-code-lark.git
cd claude-code-lark
make dev  # 需要 Poetry
```

### 3. 配置

```bash
cp .env.example .env
```

编辑 `.env`:

```bash
# 平台选择
PLATFORM=lark

# 飞书应用凭证
LARK_APP_ID=cli_xxxxxxxxx
LARK_APP_SECRET=xxxxxxxxx

# 安全设置
APPROVED_DIRECTORY=/Users/yourname/projects
ALLOWED_USERS=ou_xxxxxxxxx  # 你的 open_id
```

### 4. 运行

```bash
make run          # 生产模式
make run-debug    # 调试模式
```

## 📱 飞书/Lark 配置

### 创建应用

1. 访问 [飞书开放平台](https://open.feishu.cn/app) 或 [Lark 开放平台](https://open.larksuite.com/app)
2. 创建企业自建应用
3. 复制 **App ID** 和 **App Secret**

### 配置权限

在「权限管理」中添加：

| 权限 | 说明 |
|------|------|
| `im:message` | 获取与发送消息 |
| `im:message:receive_as_bot` | 接收机器人消息 |
| `im:message:group_at_msg` | 群聊 @ 机器人 |
| `im:chat` | 获取群组信息 |
| `contact:user.base:readonly` | 获取用户信息 |
| `cardkit:card` | 创建和更新卡片 |

### 配置事件

在「事件订阅」中：
- 订阅 `im.message.receive_v1` (接收消息)
- 启用「卡片操作触发」事件 (按钮回调)

### 获取 User ID

与机器人对话后，在日志中查看你的 `open_id`，添加到 `ALLOWED_USERS`。

## 🎮 使用方式

### Agentic 模式 (默认)

直接对话，无需命令：

```
你: 这个项目有哪些文件？
Bot: [Claude 分析项目结构]

你: 帮我重构 http_client.py
Bot: [Claude 读取、分析、修改代码]
```

### 可用命令

| 命令 | 说明 |
|------|------|
| `/start` | 开始使用 |
| `/new` | 开始新会话 |
| `/status` | 查看状态 |
| `/verbose [0\|1\|2]` | 设置详细程度 |
| `/repo [name]` | 列出/切换项目 |
| `/ls` | 列出文件 |
| `/cd <dir>` | 切换目录 |
| `/pwd` | 显示当前目录 |
| `/projects` | 显示所有项目 |
| `/export` | 导出会话 |
| `/actions` | 快速操作 |
| `/git` | Git 命令 |
| `/list` | 显示命令列表 |
| `/help` | 显示帮助 |

## ⚙️ 配置选项

### 必需配置

```bash
PLATFORM=lark
LARK_APP_ID=cli_xxxxxxxxx
LARK_APP_SECRET=xxxxxxxxx
APPROVED_DIRECTORY=/path/to/projects
ALLOWED_USERS=ou_xxxxxxxxx
```

### 可选配置

```bash
# Claude
ANTHROPIC_API_KEY=sk-ant-...  # API 密钥 (可选，默认使用 CLI 认证)
CLAUDE_MAX_COST_PER_USER=10.0  # 每用户费用限制 (USD)
CLAUDE_TIMEOUT_SECONDS=300     # 操作超时时间

# 模式
AGENTIC_MODE=true   # Agentic 模式 (默认)
VERBOSE_LEVEL=1     # 0=静默, 1=正常, 2=详细

# 速率限制
RATE_LIMIT_REQUESTS=10  # 每窗口请求数
RATE_LIMIT_WINDOW=60    # 窗口秒数
```

## 🔧 故障排除

**机器人无响应：**
- 检查 `PLATFORM=lark` 设置
- 验证 `LARK_APP_ID` 和 `LARK_APP_SECRET`
- 确认你的 `open_id` 在 `ALLOWED_USERS` 中
- 检查 Claude Code CLI 是否安装
- 查看日志 `make run-debug`

**Claude 集成问题：**
- 运行 `claude auth status` 检查认证状态
- 或设置 `ANTHROPIC_API_KEY`

**卡片更新失败：**
- 确认已添加 `cardkit:card` 权限
- 检查事件订阅是否正确配置

## 📁 项目结构

```
src/
├── bot/
│   ├── adapters/          # 平台适配器
│   │   ├── lark.py        # 飞书/Lark 适配器
│   │   └── telegram.py    # Telegram 适配器
│   ├── core_engine.py     # 核心引擎
│   └── orchestrator.py    # 消息编排
├── claude/                # Claude 集成
│   ├── facade.py          # 门面层
│   └── sdk_integration.py # SDK 集成
├── storage/               # 存储层
└── security/              # 安全模块
```

## 🛡️ 安全特性

- **用户白名单** - 只允许授权用户访问
- **目录沙箱** - 限制在批准的目录内操作
- **速率限制** - 防止滥用
- **输入验证** - 防止注入攻击
- **审计日志** - 完整的操作记录

## 📄 文档

- [飞书配置详解](docs/LARK_SETUP.md)
- [配置参考](docs/configuration.md)
- [开发指南](docs/development.md)

## 🤝 贡献

1. Fork 本仓库
2. 创建分支: `git checkout -b feature/amazing-feature`
3. 提交更改: `make test && make lint`
4. 提交 Pull Request

## 📜 许可证

MIT License - 详见 [LICENSE](LICENSE)

## 🙏 致谢

- [Claude](https://claude.ai) by Anthropic
- [claude-code-telegram](https://github.com/nickg/claude-code-telegram) - 原始 Telegram 版本
- [lark-oapi-python](https://github.com/larksuite-oapi/lark-oapi-python) - 飞书官方 SDK
