# 飞书/Lark 配置指南

本文档介绍如何配置飞书/Lark 机器人。

## 目录

1. [前提条件](#前提条件)
2. [创建应用](#创建应用)
3. [配置权限](#配置权限)
4. [配置事件订阅](#配置事件订阅)
5. [配置机器人](#配置机器人)
6. [环境配置](#环境配置)
7. [获取用户 ID](#获取用户-id)
8. [启动运行](#启动运行)
9. [常见问题](#常见问题)

## 前提条件

- 飞书或 Lark 账号（需要管理员权限创建应用）
- Python 3.11+
- Claude Code CLI（已安装并完成认证）

## 创建应用

### 1. 进入开放平台

- 飞书: https://open.feishu.cn/app
- Lark: https://open.larksuite.com/app

### 2. 创建企业自建应用

1. 点击「创建应用」
2. 选择「企业自建应用」
3. 填写应用名称（如 "Claude Code"）和描述
4. 创建完成后记录 **App ID** 和 **App Secret**

```
App ID: cli_xxxxxxxxx
App Secret: xxxxxxxxx
```

## 配置权限

进入应用 →「权限管理」，添加以下权限：

### 必需权限

| 权限 | 说明 | 权限标识 |
|------|------|---------|
| 获取与发送单聊、群组消息 | 机器人收发消息 | `im:message` |
| 接收群聊中@机器人消息 | 群聊触发 | `im:message:group_at_msg` |
| 获取群组信息 | 群组管理 | `im:chat` |
| 获取用户基本信息 | 用户身份识别 | `contact:user.base:readonly` |
| 读取云空间中文件 | 文件下载 | `drive:drive:readonly` |

### 添加步骤

1. 搜索并添加以上权限
2. 点击「批量申请权限」提交审批
3. 等待管理员批准（如果你是管理员则自动通过）

## 配置事件订阅

### 重要：WebSocket 模式不需要配置 Webhook

本机器人使用 **WebSocket 长连接** 接收事件，**不需要**配置请求 URL。但仍然需要在开放平台订阅事件。

### 订阅事件

进入应用 →「事件订阅」→ 添加事件：

1. **`im.message.receive_v1`** — 接收消息事件（必需）
2. **`card.action.trigger`** — 卡片按钮回调（必需，Stop 按钮需要）

### 注意事项

- 不需要填写「请求地址」（WebSocket 模式自动连接）
- 不需要配置「加密策略」（可选，默认关闭）
- 事件订阅后需要**重新发布**应用才能生效

## 配置机器人

进入应用 →「应用功能」→「机器人」：

1. 开启「启用机器人」
2. 填写机器人名称和描述
3. 上传头像
4. 开启「允许机器人查看用户信息」

## 环境配置

### .env 文件

```bash
# 平台选择
PLATFORM=lark

# 飞书应用凭证
LARK_APP_ID=cli_xxxxxxxxx
LARK_APP_SECRET=xxxxxxxxx

# 安全设置
APPROVED_DIRECTORY=/path/to/your/projects
ALLOWED_USERS=ou_xxxxxxxxx

# Claude 设置
USE_SDK=true
CLAUDE_TIMEOUT_SECONDS=300

# 可选：调试模式
DEBUG=false
LOG_LEVEL=INFO
```

### 获取用户 ID

与机器人对话后，在日志中查看 `open_id`：

```
{"sender_open_id": "ou_xxxxxxxxx", "event": "Received Lark message"}
```

将 `ou_xxxxxxxxx` 添加到 `ALLOWED_USERS`。

## 启动运行

```bash
# 安装依赖
make dev

# 启动机器人
make run

# 或调试模式
make run-debug
```

启动成功日志：

```
INFO Starting Lark adapter mode=websocket_long_polling
INFO WebSocket client starting...
INFO Lark adapter started (WebSocket mode)
connected to wss://msg-frontier.feishu.cn/ws/v2?...
```

## 常见问题

### 机器人无响应

1. 检查 `PLATFORM=lark` 是否设置
2. 确认 `LARK_APP_ID` 和 `LARK_APP_SECRET` 正确
3. 确认权限已审批通过
4. 确认事件已订阅且应用已发布
5. 检查 Claude Code CLI: `claude auth status`

### 权限不足

1. 进入「权限管理」确认所有权限状态为「已开通」
2. 修改权限后需要**重新发布**应用
3. 需要企业管理员审批

### 事件收不到

1. 确认已订阅 `im.message.receive_v1` 和 `card.action.trigger`
2. 应用修改后需要重新发布
3. 检查日志中 WebSocket 连接是否成功

### 卡片显示异常

1. 确认已添加 `im:message` 权限
2. 检查 CardKit API 是否可用（飞书企业版功能）
3. 查看日志中的卡片 API 返回码

### Stop 按钮不工作

1. 确认已订阅 `card.action.trigger` 事件
2. Stop 按钮通过 WebSocket 事件触发，不需要 webhook
3. 查看日志中的 "Card action received" 日志确认事件是否到达

## WebSocket vs Webhook

本机器人默认使用 WebSocket 长连接模式：

| 特性 | WebSocket (默认) | Webhook |
|------|-----------------|---------|
| 公网地址 | 不需要 | 需要 |
| 配置复杂度 | 低 | 中 |
| 实时性 | 高 | 高 |
| 适用场景 | 开发/内网部署 | 生产/云部署 |
| 连接方式 | 主动连接飞书服务器 | 飞书推送事件到你的服务器 |

如需切换到 Webhook 模式，在 `.env` 中设置 `LARK_WEBHOOK_URL`。
