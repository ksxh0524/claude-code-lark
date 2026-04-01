# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

飞书/Lark 机器人，通过聊天远程使用 Claude Code。Fork 自 [claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram)，适配飞书/Lark 平台。Python 3.11+，Poetry 构建，使用 `lark-oapi` SDK 的 WebSocket 长连接模式。

## Commands

```bash
make dev              # Install all deps (including dev)
make install          # Production deps only
make run              # Run the bot
make run-debug        # Run with debug logging
make test             # Run tests with coverage
make lint             # Black + isort + flake8 + mypy
make format           # Auto-format with black + isort

# Run a single test
poetry run pytest tests/unit/test_config.py -k test_name -v

# Type checking only
poetry run mypy src
```

## Architecture

### 核心差异 vs Telegram 版本

- **通信方式**: WebSocket 长连接（非 Webhook 回调），无需公网地址
- **消息载体**: 飞书交互式卡片（CardKit 2.0），非 Telegram 纯文本
- **流式更新**: 通过 CardKit `content_element` API 实时更新卡片内容
- **SDK Monkey-patch**: `CallBackAction._types["value"] = Any` 修复飞书 SDK 类型错误
- **JSON 双重编码**: 飞书 cardkit 的 button callback value 会双重编码 JSON 字符串

### Claude SDK Integration

`ClaudeIntegration` (facade in `src/claude/facade.py`) wraps `ClaudeSDKManager` (`src/claude/sdk_integration.py`)，使用 `claude-agent-sdk` 进行异步流式调用。Session ID 来自 Claude 的 `ResultMessage`，非本地生成。

### 请求流程

**Agentic 模式** (默认):

```
飞书消息 (WebSocket) → _on_message_received (sync, WS线程)
→ asyncio.run_coroutine_threadsafe → _process_queue (async, 主线程)
→ _dispatch_queued_message → orchestrator.handle_message
→ LarkAdapter._process_with_core_engine
→ ClaudeIntegration.run_command → SDK
→ on_stream 回调 → _update_card_content (CardKit API)
→ 最终状态 → _close_streaming_mode → 删除 Stop 按钮 + 关闭 streaming
```

**Stop 按钮流程**:

```
用户点击 Stop → card.action.trigger 事件 (WebSocket)
→ _on_card_action (sync, WS线程)
→ 解析双重编码 JSON → 设置 interrupt_event
→ asyncio.ensure_future 更新卡片为"正在中断..."
→ 主流程检测 interrupt_event → 写最终状态（用户中断）
→ 关闭 streaming mode
```

### 飞书卡片架构

使用 CardKit 2.0 Schema:

```json
{
  "schema": "2.0",
  "config": { "streaming_mode": true, "wide_screen_mode": true },
  "header": { "title": "Claude Code", "template": "blue" },
  "body": {
    "elements": [
      { "tag": "markdown", "element_id": "content_element" },  // 主内容区
      { "tag": "markdown", "element_id": "loading_icon" },     // 加载动画
      { "tag": "button", "element_id": "stop_button" }         // 中断按钮
    ]
  }
}
```

卡片更新 API:
- `card_element.content` — 更新 content_element 的 markdown 内容
- `card_element.delete` — 删除 loading_icon / stop_button
- `card.settings` — 关闭 streaming_mode

### 关键技术细节

**飞书 SDK Monkey-patch** (`lark.py` line ~1365):
```python
from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackAction
CallBackAction._types["value"] = Any
```
原因: SDK 将 `CallBackAction.value` 类型声明为 `Dict[str, Any]`，但飞书实际返回 JSON 字符串，导致反序列化失败 (error 200671)。

**双重 JSON 解析**:
飞书 cardkit button callback 的 value 是双重编码的: `"{\"action\": \"stop\"}"` 字符串被再次 JSON 编码。需要 `json.loads()` 两次。

**P2CardActionTriggerResponse 构造**:
SDK bug — 无法通过构造函数传入嵌套 dict。使用直接属性赋值:
```python
resp = P2CardActionTriggerResponse()
resp.toast = CallBackToast({"type": "info", "content": "..."})
```

**Stop 按钮竞争条件**:
`_on_card_action` 是 sync 函数，通过 `asyncio.ensure_future` 异步更新卡片。主流程的 `on_stream` 回调也在更新同一张卡片。使用 `_stop_card_updated` 标志 + sequence bumping 确保最终状态正确。

**Sequence 管理**:
所有卡片更新操作使用递增的 sequence 号。飞书 API 会忽略过期的 sequence（值低于当前最大值的更新会被丢弃，返回 error 300317）。

### 依赖注入

Lark 适配器通过 `core_multiplatform.py` 的 `inject_deps` 注入:
```python
adapter.core_engine = deps["core_engine"]
adapter.settings = deps["settings"]
adapter.orchestrator = deps["orchestrator"]
adapter.security_validator = deps["security_validator"]
```

### Key Directories

- `src/bot/adapters/lark.py` — 飞书适配器 (~2880 行，核心文件)
- `src/bot/adapters/telegram.py` — Telegram 适配器 (保留但非主力)
- `src/bot/adapters/base.py` — 平台适配器基类
- `src/bot/orchestrator.py` — 消息编排 (Telegram 模式，Lark 用 core_multiplatform)
- `src/bot/core_engine.py` — 核心引擎 (Claude 调用封装)
- `src/bot/core_multiplatform.py` — 多平台启动入口
- `src/claude/` — Claude 集成 (facade, SDK, session 管理)
- `src/config/` — Pydantic Settings v2 配置
- `src/storage/` — SQLite 存储
- `src/security/` — 安全模块
- `src/events/` — 事件系统

### 安全模型

5 层防御: 认证 (白名单/token) → 目录隔离 (APPROVED_DIRECTORY) → 输入验证 → 速率限制 → 审计日志。

`SecurityValidator` 拦截敏感文件 (.env, .ssh, id_rsa, .pem) 和危险 shell 模式。

### 配置

通过环境变量 / `.env` 文件加载。必需: `LARK_APP_ID`, `LARK_APP_SECRET`, `APPROVED_DIRECTORY`。

关键可选配置:
- `ALLOWED_USERS` — 用户白名单 (逗号分隔的 open_id)
- `CLAUDE_TIMEOUT_SECONDS` — Claude 操作超时 (默认 300)
- `AGENTIC_MODE` — Agentic 模式 (默认 true)
- `VERBOSE_LEVEL` — 输出详细程度 (0-2, 默认 1)

### DateTime Convention

所有 datetime 使用 UTC 时区: `datetime.now(UTC)`。

## Code Style

- Black (88 char line length), isort (black profile), flake8, mypy strict
- pytest-asyncio with `asyncio_mode = "auto"`
- structlog for all logging (JSON in prod, console in dev)
- Type hints required on all functions
