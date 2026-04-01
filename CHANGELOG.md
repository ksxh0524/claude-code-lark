# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Stop 按钮竞争条件**: 修复卡片卡在"正在中断"状态的问题。添加 `_stop_card_updated` 标志，主流程检测到 stop 后等待 0.5s 再用更高 sequence 覆盖为最终状态
- **`format_with_status` NameError**: `_on_card_action` 中引用了 `_process_with_core_engine` 内部定义的闭包函数，改为内联格式化
- **`image_data` 未定义风险**: 替换不可靠的 `'image_data' in dir()` 检查为正规流程控制
- **图片重复下载**: 图片处理和 base64 编码各下载一次，改为单次下载复用
- **`handle_card_callback` stop 缺少卡片更新**: webhook 路径的 stop 只发文本消息，现在也更新卡片状态
- **调试日志清理**: 移除 `value_repr=repr()` 和 `callbacks_keys=list()` 防止敏感信息泄露

### Added
- **Lark/飞书平台支持**: 完整适配飞书/Lark 平台，支持 WebSocket 长连接模式
- **流式卡片**: 实时显示 Claude 处理进度，使用 CardKit 2.0 API
- **中断按钮**: Stop 按钮可中断长时间运行的请求，即时反馈"正在中断..."状态
- **计时器显示**: 实时显示处理耗时 (如 "⏱ 处理中... 5s" → "✅ 完成 · 31.0s")
- **所有消息转卡片**: 自动将文本消息转换为交互式卡片格式
- **Agentic 模式增强**: 添加 utility 命令 (ls, cd, pwd, projects, export, actions, git, list, help, restart)
- **中文状态提示**: 使用中文显示状态信息 (处理中、完成、已中断、出错)
- **用户消息队列**: 每用户独立队列，保证顺序处理，排队时通知用户
- **安全验证**: 文件名验证、消息长度限制、目录边界检查

### Changed
- 默认平台从 Telegram 改为 Lark (`PLATFORM=lark`)
- 项目定位为飞书/Lark 版本 (fork 自 claude-code-telegram)
- 更新 README 以飞书/Lark 为主要平台
- CLAUDE.md 全面更新为 Lark 架构说明

## [1.6.0] - 2026-03-26 (upstream merge)

### Upstream Changes (claude-code-telegram)
- Voice message transcription (Mistral/OpenAI)
- `/restart` command
- Streaming partial responses via sendMessageDraft
- ToolMonitor replaced with SDK `can_use_tool` callback
- Various bug fixes and improvements

### Lark Adaptation
- Full Lark adapter implementation (WebSocket long polling)
- CardKit 2.0 streaming cards
- Stop button with immediate feedback
- Double JSON parse for Lark callback values
- Monkey-patch for SDK type error (CallBackAction.value)

## [1.5.0] - 2026-03-04

### Added
- **Voice Message Transcription**: Send voice messages for automatic transcription and Claude processing
- **`/restart` command**: Restart bot process from Telegram
- **Streaming partial responses**: Stream Claude's output in real-time via Telegram `sendMessageDraft` API

## [0.1.0] - 2025-06-05

### Added
- Project foundation with Poetry dependency management
- Configuration system with Pydantic Settings v2
- Authentication & security framework
- Telegram bot core with command routing
- Claude Code integration with session management
- SQLite storage layer with repository pattern
