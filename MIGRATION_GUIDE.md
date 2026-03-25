# Migration Guide: v1.5.0 → v2.0.0

This guide helps you migrate from the Telegram-only version (v1.5.0) to the multi-platform version (v2.0.0).

## What's New in v2.0.0

### Major Features

- ✨ **Multi-Platform Support** - Now supports both Telegram and Lark/Feishu
- 🏗️ **Platform Adapter Architecture** - Clean abstraction for adding new platforms
- 🔄 **Unified Configuration** - Single `.env` file for all platforms
- 📊 **Database Migration** - Platform-aware user and session management

### Breaking Changes

1. **Configuration Changes**
   - `TELEGRAM_BOT_TOKEN` is now optional (required only if `PLATFORM=telegram`)
   - New required field: `PLATFORM` (choices: `telegram` or `lark`)
   - New Lark-specific fields: `LARK_APP_ID`, `LARK_APP_SECRET`, etc.

2. **Database Schema**
   - New `platform` column in `users` table
   - `telegram_username` renamed to `platform_username`

3. **Command Changes**
   - Bot executable renamed: `claude-telegram-bot` → `claude-bot`

## Migration Steps

### Step 1: Backup Your Data

```bash
# Backup database
cp data/bot.db data/bot.db.backup

# Backup configuration
cp .env .env.backup
```

### Step 2: Update Dependencies

```bash
# Pull latest changes
git pull origin main

# Install new dependencies
poetry install
```

Or if using pip:
```bash
pip install -e .
pip install lark-oapi
```

### Step 3: Run Database Migration

```bash
python scripts/migrations/add_platform_support.py migrate
```

Expected output:
```
✓ Platform column already exists in users table
✓ Migration completed successfully
```

### Step 4: Update Configuration

Edit your `.env` file:

**Before (v1.5.0):**
```bash
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_BOT_USERNAME=your_bot
APPROVED_DIRECTORY=/path/to/projects
ALLOWED_USERS=123456789
```

**After (v2.0.0 - staying on Telegram):**
```bash
# Add platform selection
PLATFORM=telegram

# Keep existing Telegram settings
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_BOT_USERNAME=your_bot

# Keep other settings
APPROVED_DIRECTORY=/path/to/projects
ALLOWED_USERS=123456789
```

**Or switching to Lark:**
```bash
PLATFORM=lark

# New Lark settings
LARK_APP_ID=cli_xxxxxxxxx
LARK_APP_SECRET=xxxxxxxxx

# Keep other settings
APPROVED_DIRECTORY=/path/to/projects
ALLOWED_USERS=ou_xxxxxxxxx
```

### Step 5: Verify Installation

```bash
# Check configuration
make run-debug

# You should see:
# INFO: Platform: telegram
# INFO: Initializing Telegram adapter
# INFO: Bot initialized successfully
```

### Step 6: Test Your Bot

1. Send `/start` to your bot
2. Verify it responds correctly
3. Check session functionality with `/status`

## Platform-Specific Migration

### Migrating from Telegram to Lark

If you want to switch from Telegram to Lark:

1. **Create a Lark app** - Follow [LARK_SETUP.md](LARK_SETUP.md)
2. **Update `.env`**:
   ```bash
   PLATFORM=lark
   LARK_APP_ID=cli_xxxxxxxxx
   LARK_APP_SECRET=xxxxxxxxx
   ```
3. **Get Lark user IDs** - Update `ALLOWED_USERS` with Lark `open_id`s
4. **Restart bot**:
   ```bash
   make run
   ```
5. **Find your bot** in Lark and send `/start`

### Running Both Platforms Simultaneously

To run both Telegram and Lark bots at the same time, you'll need:

1. **Two separate installations**:
   ```bash
   # Telegram bot
   cd /path/to/telegram-bot
   # .env with PLATFORM=telegram

   # Lark bot
   cd /path/to/lark-bot
   # .env with PLATFORM=lark
   ```

2. **Separate databases** (recommended) or use the same database with platform separation

3. **Run both instances**:
   ```bash
   # Terminal 1 - Telegram
   cd /path/to/telegram-bot && make run

   # Terminal 2 - Lark
   cd /path/to/lark-bot && make run
   ```

## Rollback Procedure

If you need to rollback to v1.5.0:

### Step 1: Stop the Bot

```bash
# Stop the bot process
```

### Step 2: Restore Database

```bash
# Restore from backup
cp data/bot.db.backup data/bot.db
```

### Step 3: Restore Configuration

```bash
# Restore .env
cp .env.backup .env
```

### Step 4: Checkout Previous Version

```bash
git checkout v1.5.0
poetry install
```

### Step 5: Restart

```bash
make run
```

## Compatibility Notes

### Features Supported on Both Platforms

| Feature | Telegram | Lark | Notes |
|---------|----------|------|-------|
| Text messages | ✅ | ✅ | Fully supported |
| File uploads | ✅ | ⚠️ | Lark support in progress |
| Image uploads | ✅ | ✅ | Fully supported |
| Voice messages | ✅ | ⚠️ | Lark support in progress |
| Interactive buttons | ✅ | ✅ | Cards (Lark) / Keyboards (Telegram) |
| Command menu | ✅ | ⚠️ | Quick actions (Lark) |
| Message threads | ✅ | ✅ | Fully supported |

### Platform-Specific Limitations

**Telegram:**
- Message size limit: 4096 characters
- File size limit: 50 MB
- No native rich text (HTML only)

**Lark:**
- Message size limit: 10,000 characters
- File size limit: 100 MB
- Rich text with Markdown support
- Card-based UI with more flexibility

## Getting Help

If you encounter issues during migration:

1. **Check logs**: Use `make run-debug` for detailed logs
2. **Verify configuration**: Run `python scripts/migrations/add_platform_support.py status`
3. **Review documentation**: Check [README.md](README.md) and platform-specific guides
4. **Open an issue**: Report problems on GitHub with:
   - Your current version
   - Error messages
   - Configuration (redacted)
   - Platform (Telegram/Lark)

## FAQ

**Q: Do I need to migrate?**
A: No, v1.5.0 continues to work. But v2.0.0 offers Lark support if you need it.

**Q: Will my existing sessions work after migration?**
A: Yes, all existing sessions are preserved during database migration.

**Q: Can I switch platforms later?**
A: Yes! Just change `PLATFORM` in `.env` and add the new platform's credentials.

**Q: What happens to my Telegram bot if I switch to Lark?**
A: Your Telegram bot will remain configured. Switch back by setting `PLATFORM=telegram`.

**Q: Do I need to update ALLOWED_USERS when switching platforms?**
A: Yes, different platforms use different user ID formats:
- Telegram: Numeric user ID (e.g., `123456789`)
- Lark: Open ID string (e.g., `ou_xxxxxxxxx`)

**Q: Can I run the bot without a platform configured?**
A: No, you must specify `PLATFORM` and provide the corresponding credentials.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for full version history.
