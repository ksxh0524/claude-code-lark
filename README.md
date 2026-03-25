# Claude Code Multi-Platform Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

A multi-platform bot that gives you remote access to [Claude Code](https://claude.ai/code). Chat naturally with Claude about your projects from anywhere -- no terminal commands needed.

## 🌍 Supported Platforms

- **Telegram** - The world's most secure messaging platform
- **Lark/Feishu** - Enterprise collaboration platform (飞书)

## What is this?

This bot connects your favorite messaging platform to Claude Code, providing a conversational AI interface for your codebase:

- **Chat naturally** -- ask Claude to analyze, edit, or explain your code in plain language
- **Maintain context** across conversations with automatic session persistence per project
- **Code on the go** from any device with your messaging app
- **Receive proactive notifications** from webhooks, scheduled jobs, and CI/CD events
- **Stay secure** with built-in authentication, directory sandboxing, and audit logging
- **Multi-platform** -- switch between Telegram and Lark seamlessly

## Quick Start

### Demo

```
You: Can you help me add error handling to src/api.py?

Bot: I'll analyze src/api.py and add error handling...
     [Claude reads your code, suggests improvements, and can apply changes directly]

You: Looks good. Now run the tests to make sure nothing broke.

Bot: Running pytest...
     All 47 tests passed. The error handling changes are working correctly.
```

### 1. Prerequisites

- **Python 3.11+** -- [Download here](https://www.python.org/downloads/)
- **Claude Code CLI** -- [Install from here](https://claude.ai/code)
- **Platform credentials**:
  - **Telegram**: Bot token from [@BotFather](https://t.me/botfather)
  - **Lark/Feishu**: App credentials from [Open Platform](https://open.feishu.cn/)

### 2. Install

```bash
git clone https://github.com/yourusername/claude-code-lark.git
cd claude-code-lark
make dev  # requires Poetry
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your settings:
```

**Minimum required:**

```bash
# Choose your platform
PLATFORM=telegram  # or 'lark'

# Telegram (if PLATFORM=telegram)
TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_BOT_USERNAME=my_claude_bot

# Lark/Feishu (if PLATFORM=lark)
LARK_APP_ID=cli_xxxxxxxxx
LARK_APP_SECRET=xxxxxxxxx

# Common settings
APPROVED_DIRECTORY=/Users/yourname/projects
ALLOWED_USERS=123456789  # Your user ID
```

### 4. Run

```bash
make run          # Production
make run-debug    # With debug logging
```

Message your bot on your chosen platform to get started.

## Platform Setup Guides

### Telegram Setup

1. Create a bot on Telegram:
   - Message [@BotFather](https://t.me/botfather)
   - Send `/newbot`
   - Follow instructions to create your bot
   - Copy the bot token

2. Get your Telegram User ID:
   - Message [@userinfobot](https://t.me/userinfobot)
   - Copy your user ID

3. Configure `.env`:
   ```bash
   PLATFORM=telegram
   TELEGRAM_BOT_TOKEN=your_token
   TELEGRAM_BOT_USERNAME=your_bot_username
   ALLOWED_USERS=your_user_id
   ```

### Lark/Feishu Setup

1. Create a Lark/Feishu app:
   - Visit [Open Platform](https://open.feishu.cn/app) (Feishu) or [https://open.larksuite.com/app](https://open.larksuite.com/app) (Lark)
   - Click "Create App" → "Create App"
   - Select "Enterprise Self-Built App" (企业自建应用)
   - Copy App ID and App Secret

2. Configure permissions:
   - Go to "Permissions & Scopes"
   - Add these scopes:
     - `im:message` - Send and receive messages
     - `im:message:group_at_msg` - Group messages
     - `im:chat` - Access chat information
     - `contact:user.base:readonly` - Read user information

3. Configure events:
   - Go to "Events" → "Add Event"
   - Subscribe to: `im.message.receive_v1`

4. Configure bot:
   - Go to "Bot Configuration"
   - Enable the bot
   - Set a name and avatar

5. Get your credentials:
   - Copy `App ID` and `App Secret` from the app page
   - (Optional) Generate `Encrypt Key` and `Verification Token` for webhooks

6. Configure `.env`:
   ```bash
   PLATFORM=lark
   LARK_APP_ID=cli_xxxxxxxxx
   LARK_APP_SECRET=xxxxxxxxx
   ALLOWED_USERS=your_open_id
   ```

See [LARK_SETUP.md](docs/LARK_SETUP.md) for detailed Feishu setup instructions.

## Modes

The bot supports two interaction modes:

### Agentic Mode (Default)

The default conversational mode. Just talk to Claude naturally -- no special commands required.

**Commands:** `/start`, `/new`, `/status`, `/verbose`, `/repo`

```
You: What files are in this project?
Bot: Working... (3s)
     📖 Read
     📂 LS
     💬 Let me describe the project structure
Bot: [Claude describes the project structure]

You: Add a retry decorator to the HTTP client
Bot: Working... (8s)
     📖 Read: http_client.py
     💬 I'll add a retry decorator with exponential backoff
     ✏️ Edit: http_client.py
     💻 Bash: poetry run pytest tests/ -v
Bot: [Claude shows the changes and test results]
```

### Classic Mode

Set `AGENTIC_MODE=false` to enable the full 13-command terminal-like interface.

**Commands:** `/start`, `/help`, `/new`, `/continue`, `/end`, `/status`, `/cd`, `/ls`, `/pwd`, `/projects`, `/export`, `/actions`, `/git`

```
You: /cd my-web-app
Bot: Directory changed to my-web-app/

You: /ls
Bot: src/  tests/  package.json  README.md

You: /actions
Bot: [Run Tests] [Install Deps] [Format Code] [Run Linter]
```

## Event-Driven Automation

Beyond direct chat, the bot can respond to external triggers:

- **Webhooks** -- Receive events and route them through Claude for automated summaries or code review
- **Scheduler** -- Run recurring Claude tasks on a cron schedule (e.g., daily code health checks)
- **Notifications** -- Deliver agent responses to configured chats

Enable with `ENABLE_API_SERVER=true` and `ENABLE_SCHEDULER=true`.

## Features

### Working Features

- ✅ **Multi-platform support** - Telegram and Lark/Feishu
- ✅ Conversational agentic mode with natural language interaction
- ✅ Classic terminal-like mode with 13 commands
- ✅ Full Claude Code integration with SDK
- ✅ Automatic session persistence per user/project directory
- ✅ Multi-layer authentication (whitelist + optional token-based)
- ✅ Rate limiting with token bucket algorithm
- ✅ Directory sandboxing with path traversal prevention
- ✅ File upload handling with archive extraction
- ✅ Image/screenshot upload with analysis
- ✅ Voice message transcription (Mistral Voxtral / OpenAI Whisper)
- ✅ Git integration with safe repository operations
- ✅ Quick actions system with context-aware buttons
- ✅ Session export in Markdown, HTML, and JSON formats
- ✅ SQLite persistence with migrations
- ✅ Usage and cost tracking
- ✅ Audit logging and security event tracking
- ✅ Event bus for decoupled message routing
- ✅ Webhook API server
- ✅ Job scheduler with cron expressions
- ✅ Notification service with per-chat rate limiting
- ✅ Platform-agnostic architecture for easy extension

### Platform-Specific Features

#### Telegram
- Inline keyboards for interactive buttons
- Bot command menu
- Message threading and topics
- HTML message formatting
- Typing indicators

#### Lark/Feishu
- Interactive card messages
- Quick actions
- Rich text with Markdown
- Button interactions
- Threaded conversations

## Configuration

### Required

```bash
# Platform selection
PLATFORM=telegram  # or 'lark'

# Platform credentials (depends on PLATFORM)
TELEGRAM_BOT_TOKEN=...  # Telegram only
TELEGRAM_BOT_USERNAME=...  # Telegram only
LARK_APP_ID=...  # Lark only
LARK_APP_SECRET=...  # Lark only

# Common settings
APPROVED_DIRECTORY=...  # Base directory for project access
ALLOWED_USERS=...  # Comma-separated user IDs
```

### Common Options

```bash
# Claude
ANTHROPIC_API_KEY=sk-ant-...  # API key (optional if using CLI auth)
CLAUDE_MAX_COST_PER_USER=10.0  # Spending limit per user (USD)
CLAUDE_TIMEOUT_SECONDS=300  # Operation timeout

# Mode
AGENTIC_MODE=true  # Agentic (default) or classic mode
VERBOSE_LEVEL=1  # 0=quiet, 1=normal, 2=detailed

# Rate Limiting
RATE_LIMIT_REQUESTS=10  # Requests per window
RATE_LIMIT_WINDOW=60  # Window in seconds
```

See [docs/configuration.md](docs/configuration.md) for full reference.

## Database Migration

When upgrading from a single-platform version, run the database migration:

```bash
python scripts/migrations/add_platform_support.py migrate
```

This adds the `platform` column to support multiple platforms.

## Troubleshooting

**Bot doesn't respond:**
- Check your `PLATFORM` setting
- Verify platform credentials (token/app_id/app_secret)
- Verify your user ID is in `ALLOWED_USERS`
- Ensure Claude Code CLI is installed and accessible
- Check bot logs with `make run-debug`

**Claude integration not working:**
- SDK mode (default): Check `claude auth status` or verify `ANTHROPIC_API_KEY`
- CLI mode: Verify `claude --version` and `claude auth status`
- Check `CLAUDE_ALLOWED_TOOLS` includes necessary tools

**Platform-specific issues:**

Telegram:
- Webhook not receiving: Check webhook URL and secret
- Commands not working: Use `/start` to refresh command menu

Lark/Feishu:
- Events not received: Verify event subscription in Open Platform
- Permissions denied: Check app permissions include `im:message`

## Security

This bot implements defense-in-depth security:

- **Access Control** -- Whitelist-based user authentication
- **Directory Isolation** -- Sandboxing to approved directories
- **Rate Limiting** -- Request and cost-based limits
- **Input Validation** -- Injection and path traversal protection
- **Webhook Authentication** -- Platform-specific verification (Telegram HMAC-SHA256, Lark signatures)
- **Audit Logging** -- Complete tracking of all user actions

See [SECURITY.md](SECURITY.md) for details.

## Development

```bash
make dev           # Install all dependencies
make test          # Run tests with coverage
make lint          # Black + isort + flake8 + mypy
make format        # Auto-format code
make run-debug     # Run with debug logging
```

### Architecture

The bot uses a platform-agnostic architecture:

```
┌─────────────────────────────────────────┐
│         Business Logic Layer           │
│  (Claude, Sessions, Security, Storage) │
└──────────────┬──────────────────────────┘
               │
               │ Platform Adapter Interface
               ↓
┌─────────────────────────────────────────┐
│       Platform Adapter Layer            │
│  ┌──────────────┐  ┌──────────────┐    │
│  │  Telegram    │  │  Lark/Feishu │    │
│  │  Adapter     │  │  Adapter     │    │
│  └──────────────┘  └──────────────┘    │
└─────────────────────────────────────────┘
```

This design allows:
- Easy addition of new platforms
- Shared business logic across platforms
- Platform-specific optimizations
- Independent platform evolution

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for details.

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make changes with tests: `make test && make lint`
4. Submit a Pull Request

**Code standards:** Python 3.11+, Black formatting (88 chars), type hints required, pytest with >85% coverage.

## License

MIT License -- see [LICENSE](LICENSE).

## Acknowledgments

- [Claude](https://claude.ai) by Anthropic
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [lark-oapi](https://github.com/larksuite-oapi/lark-oapi-python) - Official Lark/Feishu SDK

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=yourusername/claude-code-lark&type=Date)](https://star-history.com/#yourusername/claude-code-lark&Date)
