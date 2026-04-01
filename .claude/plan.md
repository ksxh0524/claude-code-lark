# Lark 完整功能迁移计划

## 目标
将 Telegram 版本的所有功能完整迁移到飞书/Lark 适配器，消除功能差距。

## 核心问题
当前 Lark 适配器存在 **双重命令处理架构**：
- `lark.py` 的原生处理器（12 个命令，内存状态，优先级高）
- `orchestrator.py` 的 `_cmd_*` 方法（18 个命令，使用 storage，但大部分被原生处理器拦截）

这导致：状态不持久化、verbose 不生效、功能不完整。

## 实施步骤

### 第一阶段：合并上游 v1.6.0 平台无关变更
合并 `sdk_integration.py`、`settings.py`、`facade.py` 等共享代码的上游修复：
- 指数退避重试机制
- TextBlock/ThinkingBlock 修复
- 空响应处理修复
- 图片多模态内容传递

### 第二阶段：重构 Lark 命令路由（核心架构）
**删除** `lark.py` 中的 12 个原生命令处理器
**统一** 所有命令走 `orchestrator.py` 的 `handle_command()` 路径
这样确保：持久化存储、统一中间件、完整功能

### 第三阶段：补全 Orchestrator Lark 命令
增强 `orchestrator.py` 中的 Lark `_cmd_*` 方法：
- `/cd` — 完整路径导航 + 会话恢复
- `/ls` — 文件列表 + 导航卡片
- `/git` — 完整 GitIntegration（status/diff/log）
- `/export` — 完整导出 + 格式选择卡片
- `/actions` — 上下文感知快速操作
- `/verbose` — 接入流式卡片显示
- `/status` — 完整状态信息
- `/restart` — SIGTERM 重启
- `/repo` — git 指示器 + 卡片按钮

### 第四阶段：增强流式卡片
- 将 verbose_level 传递到 CoreEngine 和流式回调
- Level 0: 仅显示最终结果
- Level 1: 工具名 + 简短推理（当前行为）
- Level 2: 工具名 + 输入摘要 + 完整推理
- 改进工具显示（使用 Telegram 的工具图标和摘要逻辑）

### 第五阶段：新增功能
- 语音消息支持（复用 VoiceHandler，适配 Lark 音频 API）
- 图片分析（多模态内容传递给 Claude SDK）
- 安全验证集成（SecurityValidator、输入校验）
- 审计日志集成
- 速率限制集成

### 第六阶段：完善卡片交互
- 快速操作卡片（上下文感知按钮）
- Git 操作卡片（status/diff/log 按钮）
- 导出格式选择卡片
- 目录导航卡片
- 会话管理卡片

## 文件变更范围
- `src/bot/adapters/lark.py` — 删除原生处理器，增强适配器方法
- `src/bot/orchestrator.py` — 增强所有 Lark `_cmd_*` 方法
- `src/bot/core_engine.py` — 修复 verbose_level 传递
- `src/claude/sdk_integration.py` — 合并上游修复
- `src/claude/facade.py` — 合并上游修复
- `src/config/settings.py` — 合并上游新配置项
