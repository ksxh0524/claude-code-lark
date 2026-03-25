# Lark/Feishu Platform Setup Guide

This guide will walk you through setting up Claude Code Bot on Lark (international version) or Feishu (Chinese version).

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Creating Your App](#creating-your-app)
3. [Configuring Permissions](#configuring-permissions)
4. [Configuring Events](#configuring-events)
5. [Configuring Bot Settings](#configuring-bot-settings)
6. [Setting Up Webhooks](#setting-up-webhooks)
7. [Getting User IDs](#getting-user-ids)
8. [Environment Configuration](#environment-configuration)
9. [Testing Your Bot](#testing-your-bot)

## Prerequisites

- A Lark or Feishu account
- Admin access to create apps in your organization
- Python 3.11+ installed
- Claude Code CLI installed

## Platform Differences

**Lark** (国际版): https://www.larksuite.com/
- For international users
- English interface
- Global servers

**Feishu** (飞书): https://www.feishu.cn/
- For users in China
- Chinese interface
- China-based servers

The setup process is identical for both platforms. This guide uses "Lark" to refer to both.

## Creating Your App

### Step 1: Access the Open Platform

**Lark**: https://open.larksuite.com/app
**Feishu**: https://open.feishu.cn/app

### Step 2: Create a New App

1. Click **"Create App"** (创建应用)
2. Select **"Enterprise Self-Built App"** (企业自建应用)
3. Click **"Create"** (创建)

### Step 3: Configure Basic Information

1. **App Name** (应用名称): e.g., "Claude Code Bot"
2. **App Description** (应用描述): e.g., "AI coding assistant powered by Claude"
3. **App Icon** (应用图标): Upload a bot icon
4. Click **"Save"** (保存)

### Step 4: Get Your Credentials

After creating the app, you'll see:

- **App ID** (应用ID): Starts with `cli_`
- **App Secret** (应用密钥): Click to reveal

Save these for later - you'll need them for configuration.

```bash
LARK_APP_ID=cli_xxxxxxxxx
LARK_APP_SECRET=xxxxxxxxx
```

## Configuring Permissions

### Step 1: Go to Permissions

From your app page, go to **"Permissions & Scopes"** (权限与 scope) or **"Permissions"** (权限管理).

### Step 2: Add Required Scopes

Add the following scopes (search and add them):

#### Required Scopes

| Scope | Description | Chinese Name |
|-------|-------------|--------------|
| `im:message` | Send and receive messages | 获取与发送消息 |
| `im:message:group_at_msg` | Receive group @messages | 读取群聊中@机器人的消息 |
| `im:chat` | Access chat information | 获取群组信息 |
| `contact:user.base:readonly` | Read user information | 获取用户基本信息 |
| `drive:drive:readonly` | Read files (optional) | 读取云文档信息 |

### Step 3: Request Approval

1. After adding scopes, click **"Bulk Apply for Permission"** (批量申请权限)
2. Select which permissions to request
3. Click **"Submit"** (提交)

**Note**: Some permissions may require admin approval. Contact your organization admin if needed.

## Configuring Events

### Step 1: Go to Events

From your app page, go to **"Events"** (事件) or **"Event Subscriptions"** (事件订阅).

### Step 2: Subscribe to Message Events

1. Click **"Add Event"** (添加事件)
2. Search for and add: **"Receive Message"** (`im.message.receive_v1`)
3. Click **"Add"** (添加)

This event is triggered when:
- A user sends a message to the bot
- A user @mentions the bot in a group

### Step 3: Configure Event Details

For **im.message.receive_v1**, you can optionally:
- Filter by message types
- Set up request URL (webhook)

## Configuring Bot Settings

### Step 1: Enable Bot

1. Go to **"Bot Configuration"** (机器人配置) or **"Bot"** in the left menu
2. Toggle **"Enable Bot"** (启用机器人) to ON
3. Fill in:
   - **Bot Name** (机器人名称): e.g., "Claude Code Bot"
   - **Bot Description** (机器人描述): e.g., "Your AI coding assistant"
   - **Bot Avatar** (机器人头像): Upload an image

### Step 2: Configure Bot Features

Enable these features:
- ✅ **Allow users to add bot to groups** (允许将机器人添加到群组)
- ✅ **Allow bot to view user information** (允许机器人查看用户信息)

## Setting Up Webhooks

Webhooks allow Lark to push events to your bot in real-time.

### Option 1: Public URL (Recommended for Production)

If you have a public server:

1. Go to **"Event Subscriptions"** (事件订阅)
2. Enter your **Request URL**:
   ```
   https://your-server.com/webhooks/lark
   ```
3. Lark will send a verification request
4. Your bot should handle the verification challenge

### Option 2: Local Development (Tunneling)

For local development, use a tunneling service:

**Using ngrok:**
```bash
# Install ngrok
brew install ngrok  # macOS
# or download from https://ngrok.com

# Start tunnel
ngrok http 8080

# You'll get a URL like: https://abc123.ngrok.io
```

Then use: `https://abc123.ngrok.io/webhooks/lark`

**Using localtunnel:**
```bash
# Install
npm install -g localtunnel

# Start tunnel
lt --port 8080
```

### Option 3: Polling Mode (Development Only)

For simple testing, you can skip webhooks and use polling mode:

```bash
# In .env
LARK_WEBHOOK_URL=
# Leave empty for polling mode
```

**Note**: Polling is not recommended for production as it's less efficient.

### Encryption Settings (Optional)

For enhanced security, enable encryption:

1. In **"Event Subscriptions"**, enable **"Encrypt Key"** (加密键)
2. Lark will generate an encryption key
3. Copy this key to your `.env`:
   ```bash
   LARK_ENCRYPT_KEY=your_encryption_key_here
   ```

### Verification Token (Optional)

Add an extra layer of verification:

1. In event settings, generate a **Verification Token**
2. Add to your `.env`:
   ```bash
   LARK_VERIFICATION_TOKEN=your_token_here
   ```

## Getting User IDs

To whitelist users, you need their Lark `open_id`.

### Method 1: From the Event Log

1. Have a user send a message to your bot
2. Check your bot logs
3. Look for the `sender.open_id` field in the event

Example event:
```json
{
  "sender": {
    "sender_id": {
      "open_id": "ou_xxxxxxxxxxxxxxxxxxxxxxxx"
    }
  }
}
```

### Method 2: Using Lark API

Use the API to get user info:

```python
from lark_oapi.api.contact.user.v3 import GetUserRequest

request = GetUserRequest.builder() \
    .user_id("user_id") \
    .user_id_type("open_id") \
    .build()

response = await client.contact.user.v3.get(request)
print(response.data.user.open_id)
```

### Method 3: From Lark Admin Console

1. Go to **Admin Console** (管理后台)
2. Navigate to **Contacts** (通讯录)
3. Click on a user
4. Look for their `open_id` in the URL or user details

## Environment Configuration

Update your `.env` file with all the Lark settings:

```bash
# Platform selection
PLATFORM=lark

# Lark/Feishu credentials
LARK_APP_ID=cli_xxxxxxxxxxxxxxxxxxxxxx
LARK_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx

# Optional: Webhook and security
LARK_WEBHOOK_URL=https://your-server.com/webhooks/lark
LARK_ENCRYPT_KEY=your_encryption_key
LARK_VERIFICATION_TOKEN=your_verification_token

# Common settings
APPROVED_DIRECTORY=/Users/yourname/projects
ALLOWED_USERS=ou_xxxxxxxxx,ou_yyyyyyyyy

# Claude settings
ANTHROPIC_API_KEY=sk-ant-xxxxx
USE_SDK=true

# Bot mode
AGENTIC_MODE=true
VERBOSE_LEVEL=1
```

## Testing Your Bot

### Step 1: Start Your Bot

```bash
make run-debug
```

You should see:
```
INFO: Initializing Lark adapter
INFO: Lark adapter initialized successfully
INFO: Starting Lark adapter
INFO: Lark adapter started (webhook mode)
```

### Step 2: Find Your Bot

1. Open Lark/Feishu
2. In the search bar, type your bot's name
3. Click on your bot to open a chat

### Step 3: Send a Test Message

Send a simple message like:
```
/start
```

You should receive a welcome message.

### Step 4: Test Basic Commands

Try these commands:
```
/help      - Show help message
/status    - Show bot status
/new       - Start a new Claude session
```

### Step 5: Test Claude Integration

Send a coding request:
```
What files are in this project?
```

The bot should respond with the project structure.

## Troubleshooting

### Bot Not Responding

1. **Check logs**: Run with `make run-debug` to see detailed logs
2. **Verify credentials**: Ensure `LARK_APP_ID` and `LARK_APP_SECRET` are correct
3. **Check permissions**: Verify all required scopes are approved
4. **Test webhook**: Use webhook test tools to verify your endpoint

### Events Not Received

1. **Verify event subscription**: Check that `im.message.receive_v1` is subscribed
2. **Check webhook URL**: Ensure your webhook URL is accessible
3. **Review encryption**: If using encryption, verify the key matches
4. **Test with ngrok**: Use a tunneling service to test locally

### Permission Denied Errors

1. **Review scopes**: Ensure all required scopes are added
2. **Request approval**: Submit permission requests to admin
3. **Check bot status**: Verify the bot is enabled in bot configuration

### Can't Find User ID

1. **Check event logs**: Look at incoming events in debug logs
2. **Use API**: Use the Lark API to query user information
3. **Ask user**: Have the user send `/start` and check the logs

## Next Steps

Once your bot is working:

1. **Add to groups**: Add the bot to group chats for collaborative coding
2. **Set up quick actions**: Configure commonly used commands
3. **Enable features**: Turn on file uploads, voice messages, etc.
4. **Monitor usage**: Check the database for usage statistics

## Additional Resources

- [Lark Open Platform Documentation](https://open.larksuite.com/document)
- [Feishu Open Platform Documentation](https://open.feishu.cn/document)
- [Lark Python SDK](https://github.com/larksuite-oapi/lark-oapi-python)
- [Claude Code Documentation](https://claude.ai/code)

## Support

If you encounter issues:

1. Check the [Troubleshooting](#troubleshooting) section
2. Review bot logs with `make run-debug`
3. Check Lark/Feishu Open Platform documentation
4. Open an issue on GitHub
