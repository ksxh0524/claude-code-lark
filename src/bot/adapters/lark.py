"""Lark/Feishu platform adapter implementation using WebSocket long polling."""

import asyncio
import json
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from datetime import datetime

import structlog

try:
    import lark_oapi as lark
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        DeleteMessageRequest,
        UpdateMessageRequest,
        UpdateMessageRequestBody,
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        GetFileRequest,
        GetImageRequest,
    )
    from lark_oapi.api.cardkit.v1 import (
        CreateCardRequest,
        CreateCardRequestBody,
        SettingsCardRequest,
        SettingsCardRequestBody,
        ContentCardElementRequest,
        ContentCardElementRequestBody,
    )
    LARK_AVAILABLE = True
except ImportError:
    # If SDK not installed yet, provide error
    LARK_AVAILABLE = False
    lark = None  # type: ignore
    Client = None  # type: ignore
    CreateMessageRequest = None  # type: ignore
    CreateMessageRequestBody = None  # type: ignore
    DeleteMessageRequest = None  # type: ignore
    UpdateMessageRequest = None  # type: ignore
    UpdateMessageRequestBody = None  # type: ignore
    CreateFileRequest = None  # type: ignore
    CreateFileRequestBody = None  # type: ignore
    CreateImageRequest = None  # type: ignore
    CreateImageRequestBody = None  # type: ignore
    GetFileRequest = None  # type: ignore
    GetImageRequest = None  # type: ignore
    CreateCardRequest = None  # type: ignore
    CreateCardRequestBody = None  # type: ignore
    SettingsCardRequest = None  # type: ignore
    SettingsCardRequestBody = None  # type: ignore
    ContentCardElementRequest = None  # type: ignore
    ContentCardElementRequestBody = None  # type: ignore

from src.bot.adapters.base import PlatformAdapter
from src.bot.adapters.models import (
    MessageType,
    PlatformCard,
    PlatformCardElement,
    PlatformEvent,
    PlatformFile,
    PlatformMessage,
    PlatformResponse,
    PlatformType,
    PlatformUser,
)

logger = structlog.get_logger()


@dataclass
class QueuedMessage:
    """A message waiting in the per-user processing queue."""

    event_data: Dict[str, Any]
    text: str
    chat_id: str
    sender: Dict[str, Any]
    file_info: Optional[Dict[str, Any]] = None
    is_command: bool = False


class LarkAdapter(PlatformAdapter):
    """Lark/Feishu platform adapter using WebSocket long polling."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Lark adapter.

        Args:
            config: Configuration dict with:
                - app_id: Lark app ID (cli_xxxxxxxxx)
                - app_secret: Lark app secret
                - encrypt_key: Optional encryption key
                - verification_token: Optional verification token
        """
        super().__init__(config)
        self.platform_type = PlatformType.LARK
        self.app_id = config["app_id"]
        self.app_secret = config["app_secret"]
        self.encrypt_key = config.get("encrypt_key", "")
        self.verification_token = config.get("verification_token", "")

        self.client: Optional[Client] = None
        self.ws_client: Optional[Any] = None  # lark.ws.Client
        self._is_running = False
        self._stop_event = asyncio.Event()
        self._event_handlers: List[Callable] = []  # Event handlers
        self._command_handlers: List[Callable] = []  # Command handlers
        self._message_handlers: List[Callable] = []  # Message handlers
        self._callback_handlers: List[Callable] = []  # Callback handlers
        self._registered_commands: List[str] = []  # List of registered commands
        self._ws_thread: Optional[threading.Thread] = None
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self.core_engine: Optional[Any] = None  # CoreEngine instance
        self.settings: Optional[Any] = None  # Settings instance
        self._stop_callbacks: Dict[int, asyncio.Event] = {}  # user_id -> interrupt event
        self._streaming_cards: Dict[int, tuple] = {}  # user_id -> (card_id, message_id, update_seq)
        self._streaming_start_time: Dict[int, float] = {}  # user_id -> start timestamp

        # Per-user state: working directory, session ID, etc.
        # Mirrors Telegram's context.user_data for platform-agnostic features.
        self._user_data: Dict[int, Dict[str, Any]] = {}

        # Reference to orchestrator for user settings access
        self._orchestrator_ref: Optional[Any] = None

        # Per-user message queue: messages are processed sequentially
        self._message_queues: Dict[int, deque[QueuedMessage]] = defaultdict(deque)
        self._queue_events: Dict[int, asyncio.Event] = {}  # signals "queue has items"
        self._user_locks: Dict[int, bool] = {}  # True = currently processing
        self._auth_manager: Optional[Any] = None  # Set by MultiPlatformBot
        self._security_validator: Optional[Any] = None  # SecurityValidator reference

        # Authentication manager for user authorization
        self._auth_manager: Optional[Any] = None

    @property
    def orchestrator(self):
        """Get orchestrator reference."""
        return self._orchestrator_ref

    @orchestrator.setter
    def orchestrator(self, value):
        """Set orchestrator reference."""
        self._orchestrator_ref = value

    @property
    def auth_manager(self):
        """Get auth manager reference."""
        return self._auth_manager

    @auth_manager.setter
    def auth_manager(self, value):
        """Set auth manager reference."""
        self._auth_manager = value

    @property
    def security_validator(self):
        """Get security validator reference."""
        return self._security_validator

    @security_validator.setter
    def security_validator(self, value):
        """Set security validator reference."""
        self._security_validator = value

    async def initialize(self) -> None:
        """Initialize Lark client."""
        if not LARK_AVAILABLE:
            raise ImportError(
                "lark-oapi SDK is not installed. "
                "Install it with: pip install lark-oapi"
            )

        if self.client is not None:
            return

        logger.info("Initializing Lark adapter", app_id=self.app_id)

        # Create Lark client for sending messages
        self.client = Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .build()

        logger.info("Lark adapter initialized successfully")

    def _on_message_received(self, data: Any) -> None:
        """Handle incoming message event from WebSocket."""
        # Debug: log raw event data
        logger.info("=== _on_message_received called ===", data_type=type(data).__name__)
        try:
            # Parse lark event to dict
            event_data = self._parse_lark_event(data)
            logger.info("Parsed event data", event_data=event_data)
            message = event_data.get("message", {})
            sender = event_data.get("sender", {})
            chat_id = message.get("chat_id", "")
            msg_type = message.get("msg_type", "text")
            content_raw = message.get("content", "{}")
            open_id = sender.get("open_id", "")

            # Parse content
            try:
                content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
            except json.JSONDecodeError:
                content = {}

            # Handle different message types
            text = ""
            file_info = None

            if msg_type == "text":
                text = content.get("text", "")
            elif msg_type == "file":
                # Extract file info
                file_key = content.get("file_key", "")
                file_name = content.get("file_name", "unknown")

                # Security: validate filename (sync check)
                if self._security_validator:
                    is_valid, error = self._security_validator.validate_filename(file_name)
                    if not is_valid:
                        logger.warning("Blocked unsafe file upload", file_name=file_name, error=error)
                        if self._main_loop and not self._main_loop.is_closed():
                            asyncio.run_coroutine_threadsafe(
                                self.send_message(chat_id, f"⛔ 文件被拒绝: {error}"),
                                self._main_loop
                            )
                        return

                file_info = {
                    "type": "file",
                    "file_key": file_key,
                    "file_name": file_name,
                }
                text = f"[用户上传了文件: {file_name}]"
                logger.info("Received file message", file_key=file_key, file_name=file_name)
            elif msg_type == "image":
                # Extract image info
                image_key = content.get("image_key", "")
                file_info = {
                    "type": "image",
                    "image_key": image_key,
                }
                text = "[用户上传了一张图片]"
                logger.info("Received image message", image_key=image_key)
            elif msg_type == "audio":
                # Voice/audio message
                file_key = content.get("file_key", "")
                file_info = {
                    "type": "audio",
                    "file_key": file_key,
                    "duration": content.get("duration", 0),
                }
                text = "[语音消息]"
                logger.info("Received audio message", file_key=file_key)
            else:
                # Unknown message type, skip
                logger.info("Skipping unsupported message type", msg_type=msg_type)
                return

            if not text and not file_info:
                return

            # Security: reject extremely long messages (potential DoS)
            MAX_MESSAGE_LENGTH = 50000
            if len(text) > MAX_MESSAGE_LENGTH:
                logger.warning(
                    "Message too long, rejecting",
                    length=len(text),
                    chat_id=chat_id,
                    sender_open_id=sender.get("open_id", ""),
                )
                if self._main_loop and not self._main_loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        self.send_message(chat_id, "消息过长，请缩短后重试。"),
                        self._main_loop,
                    )
                return

            # Log the received event (sanitize text for logging)
            safe_text = text[:50].replace("\n", " ").replace("\r", " ") if text else ""
            logger.info(
                "Received Lark message",
                chat_id=chat_id,
                msg_type=msg_type,
                text=safe_text,
                sender_open_id=sender.get("open_id", ""),
            )

            # Enqueue message for sequential processing via main loop
            if self._main_loop and not self._main_loop.is_closed():
                open_id = sender.get("open_id", "")
                user_id = hash(open_id) % 1000000

                # Voice messages bypass queue (handled separately)
                if file_info and file_info.get("type") == "audio":
                    asyncio.run_coroutine_threadsafe(
                        self._handle_voice_message(chat_id, file_info, sender),
                        self._main_loop
                    )
                    return

                is_command = text.strip().startswith("/")
                queued = QueuedMessage(
                    event_data=event_data,
                    text=text,
                    chat_id=chat_id,
                    sender=sender,
                    file_info=file_info,
                    is_command=is_command,
                )
                self._message_queues[user_id].append(queued)
                queue_size = len(self._message_queues[user_id])
                logger.info(
                    "Message enqueued",
                    user_id=user_id,
                    queue_size=queue_size,
                    is_command=is_command,
                )

                # If user not currently processing, kick off the queue consumer
                if not self._user_locks.get(user_id, False):
                    asyncio.run_coroutine_threadsafe(
                        self._process_queue(user_id), self._main_loop
                    )
                elif queue_size > 1:
                    # Notify user they are queued
                    asyncio.run_coroutine_threadsafe(
                        self.send_message(
                            chat_id,
                            f"⏳ 排队中 (前方还有 {queue_size - 1} 条消息)",
                        ),
                        self._main_loop,
                    )

        except Exception as e:
            logger.error("Error processing Lark message event", error=str(e), exc_info=True)

    async def _process_queue(self, user_id: int) -> None:
        """Process messages from user's queue sequentially.

        This is the core consumer loop. Only one instance runs per user
        at a time, guarded by _user_locks.
        """
        if self._user_locks.get(user_id, False):
            return  # Already processing

        self._user_locks[user_id] = True
        try:
            while self._message_queues[user_id]:
                msg = self._message_queues[user_id].popleft()
                try:
                    await self._dispatch_queued_message(msg, user_id)
                except Exception as e:
                    logger.error(
                        "Error processing queued message",
                        user_id=user_id,
                        error=str(e),
                        exc_info=True,
                    )
                    try:
                        await self.send_message(
                            msg.chat_id, f"❌ 处理消息时出错: {str(e)[:200]}"
                        )
                    except Exception:
                        pass
        finally:
            self._user_locks[user_id] = False
            logger.info("Queue consumer done", user_id=user_id)

    async def _dispatch_queued_message(
        self, msg: QueuedMessage, user_id: int
    ) -> None:
        """Dispatch a single queued message to the appropriate handler."""
        # Auth check
        open_id = msg.sender.get("open_id", "")
        if not await self._check_lark_auth(open_id, msg.chat_id):
            return

        # Input security validation (non-command messages only)
        if not msg.is_command and self._security_validator:
            sanitized = self._security_validator.sanitize_command_input(msg.text)
            if sanitized != msg.text and len(sanitized) < len(msg.text) * 0.5:
                logger.warning(
                    "Input heavily sanitized, rejecting",
                    user_id=user_id,
                    original_len=len(msg.text),
                    sanitized_len=len(sanitized),
                )
                await self.send_message(msg.chat_id, "⛔ 输入包含过多不安全字符，已被拒绝。")
                return

        # /stop command: interrupt current task
        if msg.text.strip().lower() in ("/stop", "/stop\n"):
            stopped = self.stop_current_task(user_id)
            if stopped:
                await self.send_message(msg.chat_id, "⏹ 已停止当前任务。")
            else:
                await self.send_message(msg.chat_id, "ℹ️ 没有正在进行的任务。")
            return

        # Dispatch based on type
        if msg.is_command:
            if self._command_handlers:
                for handler in self._command_handlers:
                    await handler(msg.event_data)
            else:
                logger.warning("No command handlers registered", text=msg.text[:30])
        else:
            if self._message_handlers:
                for handler in self._message_handlers:
                    await handler(msg.event_data)
            elif hasattr(self, "_process_with_core_engine") and self.core_engine:
                await self._process_with_core_engine(
                    msg.chat_id, msg.text, msg.sender, file_info=msg.file_info
                )

    def stop_current_task(self, user_id: int) -> bool:
        """Interrupt the currently running Claude task for a user.

        Returns True if a task was interrupted, False if nothing running.
        """
        interrupt_event = self._stop_callbacks.get(user_id)
        if interrupt_event and not interrupt_event.is_set():
            interrupt_event.set()
            logger.info("Task interrupted via /stop", user_id=user_id)
            return True
        return False

    async def _check_lark_auth(self, open_id: str, chat_id: str) -> bool:
        """Check if Lark user is authorized.

        Derives a numeric user_id from the Lark open_id and checks
        against the authentication manager's whitelist.

        Args:
            open_id: Lark user open_id
            chat_id: Chat ID to send rejection message to

        Returns:
            True if authorized, False if rejected
        """
        if not self._auth_manager:
            return True  # No auth configured, allow all

        user_id_hash = hash(f"lark:{open_id}") & 0xFFFFFFFF
        if self._auth_manager.is_authenticated(user_id_hash):
            return True

        # Try to authenticate (checks whitelist providers)
        authenticated = await self._auth_manager.authenticate_user(user_id_hash)
        if not authenticated:
            logger.warning("Unauthorized Lark user", open_id=open_id)
            await self.send_message(chat_id, "⛔ 未授权。请联系管理员将你添加到白名单。")
            return False
        return True

    async def _dispatch_to_handlers(
        self,
        event_data: Dict[str, Any],
        handlers: List[Callable],
    ) -> None:
        """Dispatch event to all registered handlers."""
        for handler in handlers:
            try:
                await handler(event_data)
            except Exception as e:
                logger.error("Handler error", error=str(e), exc_info=True)

    async def _handle_voice_message(
        self,
        chat_id: str,
        file_info: Dict[str, Any],
        sender: Dict[str, Any],
    ) -> None:
        """Handle voice message by transcribing and sending to Claude."""
        try:
            file_key = file_info.get("file_key", "")
            if not file_key:
                await self.send_message(chat_id, "无法获取语音文件")
                return

            # Download audio
            audio_bytes = await self.download_file(file_key)
            if not audio_bytes:
                await self.send_message(chat_id, "语音下载失败")
                return

            # Transcribe using configured provider
            transcription = await self._transcribe_audio(audio_bytes)
            if not transcription:
                await self.send_message(chat_id, "语音转文字失败")
                return

            # Send transcription as message to Claude
            text = f"[语音转文字]: {transcription}"

            await self.send_message(chat_id, f"语音识别: {transcription}")

            # Process with core engine
            await self._process_with_core_engine(chat_id, text, sender)

        except Exception as e:
            logger.error("Voice message handling failed", error=str(e))
            await self.send_message(chat_id, f"语音处理失败: {str(e)}")

    async def _transcribe_audio(self, audio_bytes: bytes) -> Optional[str]:
        """Transcribe audio using configured provider."""
        import base64

        settings = self.settings
        if not settings:
            return None

        provider = getattr(settings, 'voice_provider', 'mistral')

        try:
            if provider == "openai" and getattr(settings, 'openai_api_key', None):
                import httpx
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
                        files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                        data={"model": getattr(settings, 'resolved_voice_model', 'whisper-1')},
                    )
                    if response.status_code == 200:
                        return response.json().get("text", "")

            elif provider == "mistral" and getattr(settings, 'mistral_api_key', None):
                import httpx
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        "https://api.mistral.ai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {settings.mistral_api_key.get_secret_value()}"},
                        files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                        data={"model": getattr(settings, 'resolved_voice_model', 'mistral')},
                    )
                    if response.status_code == 200:
                        return response.json().get("text", "")
        except Exception as e:
            logger.error("Audio transcription failed", provider=provider, error=str(e))

        return None

    async def _process_with_core_engine(
        self,
        chat_id: str,
        text: str,
        sender: Dict[str, Any],
        file_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Process message using CoreEngine with streaming card.

        Args:
            chat_id: Chat ID from Lark
            text: Text content or description
            sender: Sender information dict
            file_info: Optional file/image info if message contains media
        """
        if not self.core_engine or not self.settings:
            logger.error("CoreEngine or Settings not configured")
            await self.send_message(chat_id, "系统未正确配置")
            return

        from src.bot.core_engine import MessageContext, StreamEvent
        import time

        open_id = sender.get("open_id", "")
        user_id = hash(open_id) % 1000000

        # Handle file/image if present
        if file_info:
            try:
                if file_info.get("type") == "file":
                    # Download and process file
                    file_key = file_info.get("file_key", "")
                    file_name = file_info.get("file_name", "unknown")

                    # Send processing message
                    await self.send_message(chat_id, f"正在处理文件: {file_name}...")

                    file_data = await self.download_file(file_key)
                    if file_data:
                        # Process file content based on type
                        text = await self._process_file_content(file_data, file_name, text)
                    else:
                        await self.send_message(chat_id, f"无法下载文件: {file_name}")
                        return

                elif file_info.get("type") == "image":
                    # Image will be processed below together with base64 preparation
                    # (single download, reuse for both text processing and Claude API)
                    pass

            except Exception as e:
                logger.error("Error processing file/image", error=str(e), exc_info=True)
                await self.send_message(chat_id, f"处理文件时出错: {str(e)}")
                return

        # Prepare base64 image data for Claude's multimodal API
        # Download image once and reuse for both text processing and Claude API
        images = None
        img_bytes = None  # Track downloaded image bytes for reuse
        if file_info and file_info.get("type") == "image":
            image_key = file_info.get("image_key", "")
            if image_key:
                try:
                    img_bytes = await self.download_image(image_key)
                    if img_bytes:
                        import base64
                        # Process image for text description first
                        text = await self._process_image_content(img_bytes, text)
                        # Then prepare base64 for Claude's multimodal API
                        images = [{
                            "data": base64.b64encode(img_bytes).decode("utf-8"),
                            "media_type": "image/png",
                        }]
                except Exception as e:
                    logger.warning("Failed to prepare base64 image", error=str(e))

        # Get verbose level from orchestrator's user settings
        verbose_level = 1
        if hasattr(self, '_orchestrator_ref') and self._orchestrator_ref:
            user_settings = getattr(self._orchestrator_ref, '_user_settings', {})
            verbose_level = user_settings.get(user_id, {}).get("verbose_level", 1)

        working_dir = self._get_working_directory(user_id)
        session_id = self._get_session_id(user_id)

        # Security: validate working directory stays within approved directory
        approved_dir = Path(self.settings.approved_directory).resolve()
        try:
            working_dir_resolved = Path(working_dir).resolve()
            working_dir_resolved.relative_to(approved_dir)
        except (ValueError, OSError):
            logger.warning(
                "Invalid working directory, resetting to approved directory",
                working_dir=working_dir,
                approved_dir=str(approved_dir),
                user_id=user_id,
            )
            working_dir = str(approved_dir)
            self._set_working_directory(user_id, working_dir)

        ctx = MessageContext(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            working_directory=working_dir,
            username=sender.get("nickname", sender.get("user_id", "")),
            is_private=True,
            platform="lark",
            session_id=session_id,
        )
        # Attach verbose_level and images as attributes
        ctx.verbose_level = verbose_level  # type: ignore[attr-defined]
        if images:
            ctx.images = images  # type: ignore[attr-defined]

        interrupt_event = asyncio.Event()
        start_time = time.time()

        # Step 1: Create streaming card with Stop button
        card_id, message_id = await self._create_streaming_card(chat_id, user_id)
        if not card_id:
            # Fallback to simple text
            await self._process_with_fallback(chat_id, ctx, user_id, start_time, interrupt_event)
            return

        # Track content and update state
        full_content = [""]
        tool_lines: List[str] = []  # Track tool calls for nice display
        update_sequence = [1]  # Start from 1 (0 might fail - card not ready)
        last_update_time = [0.0]  # Track last update time for throttling

        # Store streaming state for card action handler access
        self._streaming_cards[user_id] = (card_id, message_id, update_sequence)
        self._streaming_start_time[user_id] = start_time

        # Wait for card to be ready before first update (1s to ensure card is fully created)
        await asyncio.sleep(1.0)

        # Tool icon mapping (mirrors Telegram's display style)
        TOOL_ICONS: Dict[str, str] = {
            "Read": "📖",
            "read_file": "📖",
            "Write": "✏️",
            "write": "✏️",
            "create_file": "✏️",
            "Edit": "✏️",
            "edit_file": "✏️",
            "Bash": "💻",
            "bash": "💻",
            "shell": "💻",
            "Glob": "🔍",
            "glob": "🔍",
            "Grep": "🔍",
            "grep": "🔍",
            "search": "🔍",
            "WebFetch": "🌐",
            "web_fetch": "🌐",
            "WebSearch": "🔍",
            "web_search": "🔍",
            "List": "📂",
            "list": "📂",
            "directory": "📂",
            "mcp__": "🔌",
            "TodoRead": "📋",
            "TodoWrite": "📋",
        }

        def _get_tool_icon(tool_name: str) -> str:
            """Get emoji icon for a tool name."""
            for key, icon in TOOL_ICONS.items():
                if tool_name.lower().startswith(key.lower()):
                    return icon
            return "🔧"

        def _build_display_content() -> str:
            """Build the full display content combining tool lines and text."""
            parts = []
            # Add recent tool calls (last 8)
            recent_tools = tool_lines[-8:] if tool_lines else []
            for tl in recent_tools:
                parts.append(tl)
            # Add response text
            if full_content[0]:
                parts.append(full_content[0])
            return "\n\n".join(parts) if parts else "Thinking..."

        # Helper to format content with timer/status prefix
        def format_with_status(content: str, status: str, elapsed: float) -> str:
            """Format content with status header."""
            return f"**{status} · {elapsed:.0f}s**\n\n{content}"

        # Stream callback - accumulates content with throttled updates
        async def on_stream(event: StreamEvent) -> None:
            if interrupt_event.is_set():
                return

            content_changed = False
            if event.type == "progress" and event.tool_name:
                # Format tool call with emoji icon and input preview
                icon = _get_tool_icon(event.tool_name)
                tool_input_preview = ""
                if event.tool_input:
                    # Show a short preview of the tool input
                    preview = str(event.tool_input).replace("\n", " ")
                    if len(preview) > 80:
                        preview = preview[:77] + "..."
                    tool_input_preview = f" — `{preview}`"

                tool_line = f"{icon} **{event.tool_name}**{tool_input_preview}"
                tool_lines.append(tool_line)
                content_changed = True
            elif event.type == "response" and event.content:
                content = event.content
                # Filter out SDK internal messages
                if content.startswith("[ThinkingBlock") or content.startswith("[ContentBlock"):
                    return
                if content.startswith("[") and "]" in content[:30] and not content.startswith("[Error"):
                    # Likely an SDK metadata line, skip it
                    return
                full_content[0] += content
                content_changed = True
            elif event.type == "error":
                full_content[0] += f"\n❌ **Error:** {event.content}\n"
                content_changed = True

            # Throttle updates: at most every 150ms (like OpenClaw's CARDKIT_MS)
            if content_changed:
                now = time.time()
                if now - last_update_time[0] >= 0.15:
                    elapsed = now - start_time
                    display = _build_display_content()
                    display = format_with_status(display, "⏱ 处理中...", elapsed)
                    await self._update_card_content(card_id, display, update_sequence[0])
                    update_sequence[0] += 1
                    last_update_time[0] = now

        self._stop_callbacks[user_id] = interrupt_event

        try:
            response = await self.core_engine.process_message(
                ctx, on_stream, interrupt_event=interrupt_event
            )

            elapsed = time.time() - start_time
            # Chinese status messages
            if response.success:
                status = "✅ 完成"
            elif response.interrupted:
                status = "⏹ 用户中断"
            else:
                status = "❌ 出错"

            # Save session_id for continuation
            if response.session_id:
                self._set_session_id(user_id, response.session_id)

                # Also persist working directory and session to storage if available
                if self.settings and hasattr(self, 'storage') and self.storage:
                    try:
                        await self.storage.set_user_setting(user_id, "working_directory", str(working_dir))
                        await self.storage.set_user_setting(user_id, "claude_session_id", response.session_id)
                    except Exception as e:
                        logger.warning("Failed to persist session", error=str(e))

            # Always use response.content as final text (it's complete)
            final_text = response.content if response.content else full_content[0]
            if response.interrupted:
                final_text += "\n\n_(用户中断)_"

            logger.info("Final response", content_len=len(final_text), response_len=len(response.content or ""))

            # Final update with status — build full display including tool lines
            if response.content:
                full_content[0] = response.content
            final_display = _build_display_content()
            final_display = format_with_status(final_display, status, elapsed)
            await self._update_card_content(card_id, final_display, update_sequence[0])
            await self._close_streaming_mode(card_id, update_sequence[0] + 1)

        except asyncio.CancelledError:
            elapsed = time.time() - start_time
            final_display = format_with_status("请求已中断", "⏹ 用户中断", elapsed)
            await self._update_card_content(card_id, final_display, update_sequence[0])
            await self._close_streaming_mode(card_id, update_sequence[0] + 1)

        except Exception as e:
            logger.error("Error", error=str(e), exc_info=True)
            elapsed = time.time() - start_time
            final_display = format_with_status(str(e), "❌ 出错", elapsed)
            await self._update_card_content(card_id, final_display, update_sequence[0])
            await self._close_streaming_mode(card_id, update_sequence[0] + 1)

        finally:
            self._stop_callbacks.pop(user_id, None)
            self._streaming_cards.pop(user_id, None)
            self._streaming_start_time.pop(user_id, None)

    async def _create_streaming_card(
        self, chat_id: str, user_id: int = 0
    ) -> tuple[Optional[str], Optional[str]]:
        """Create streaming card with Stop button and send as message.

        Follows OpenClaw's STREAMING_THINKING_CARD pattern:
        - streaming_mode with summary for processing state
        - loading icon element for visual feedback
        - Stop button for interruption

        Args:
            chat_id: Chat ID to send card to
            user_id: User ID for stop button callback

        Returns:
            tuple of (card_id, message_id) or (None, None) on failure
        """
        try:
            # Schema 2.0: button goes directly in elements, not in "action" container
            # Use behaviors with type "callback" for button click handling
            # Pattern based on OpenClaw's STREAMING_THINKING_CARD
            #
            # NOTE: We combine timer and content into ONE element because
            # the card_element.content API (error 300317) doesn't work reliably
            # for separate markdown elements. One combined element is simpler.
            card_json = {
                "schema": "2.0",
                "header": {
                    "title": {"content": "Claude Code", "tag": "plain_text"},
                    "template": "blue"
                },
                "config": {
                    "wide_screen_mode": True,
                    "streaming_mode": True
                },
                "body": {
                    "elements": [
                        # Combined timer + content element
                        # Timer is included in content, updated together
                        {
                            "tag": "markdown",
                            "content": "⏱ 处理中... 0s\n\nThinking...",
                            "element_id": "content_element",
                            "text_align": "left",
                            "text_size": "normal_v2"
                        },
                        # Loading indicator (like OpenClaw's loading_icon)
                        {
                            "tag": "markdown",
                            "content": " ",
                            "icon": {
                                "tag": "custom_icon",
                                "img_key": "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg",
                                "size": "16px 16px"
                            },
                            "element_id": "loading_icon"
                        },
                        # Stop button for interruption
                        {
                            "tag": "button",
                            "element_id": "stop_button",
                            "text": {"content": "Stop", "tag": "plain_text"},
                            "type": "danger",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": json.dumps({"action": "stop", "user_id": user_id})
                                }
                            ]
                        }
                    ]
                }
            }

            create_request = CreateCardRequest.builder() \
                .request_body(
                    CreateCardRequestBody.builder()
                        .type("card_json")
                        .data(json.dumps(card_json))
                        .build()
                ).build()

            create_response = await self._execute_async(
                self.client.cardkit.v1.card.create, create_request
            )

            if create_response.code != 0:
                logger.error("Failed to create card", code=create_response.code)
                return None, None

            card_id = create_response.data.card_id

            # Send card as message
            send_content = json.dumps({"type": "card", "data": {"card_id": card_id}})
            message_request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("interactive")
                        .content(send_content)
                        .build()
                ).build()

            message_response = await self._execute_async(
                self.client.im.v1.message.create, message_request
            )

            if message_response.code != 0:
                logger.error("Failed to send card", code=message_response.code)
                return card_id, None

            logger.info("Created streaming card", card_id=card_id, message_id=message_response.data.message_id)
            return card_id, message_response.data.message_id

        except Exception as e:
            logger.error("Error creating streaming card", error=str(e))
            return None, None

    async def _update_card_content(self, card_id: str, content: str, sequence: int) -> bool:
        """Update card content via streaming API."""
        try:
            if len(content) > 7000:
                content = content[:7000] + "\n\n... (truncated)"

            # Direct markdown content, not JSON wrapped
            update_request = ContentCardElementRequest.builder() \
                .card_id(card_id) \
                .element_id("content_element") \
                .request_body(
                    ContentCardElementRequestBody.builder()
                        .content(content)
                        .uuid(str(uuid.uuid4()))
                        .sequence(sequence)
                        .build()
                ).build()

            response = await self._execute_async(
                self.client.cardkit.v1.card_element.content, update_request
            )

            if response.code != 0:
                logger.warning("Card content update failed", code=response.code)
                return False
            return True

        except Exception as e:
            logger.error("Error updating card content", error=str(e))
            return False

    async def _update_card_timer(self, card_id: str, timer_text: str, sequence: int) -> bool:
        """Update timer element via streaming API.

        Uses the same card_element.content API as content updates,
        but targets the timer_element instead of content_element.
        """
        try:
            update_request = ContentCardElementRequest.builder() \
                .card_id(card_id) \
                .element_id("timer_element") \
                .request_body(
                    ContentCardElementRequestBody.builder()
                        .content(timer_text)
                        .uuid(str(uuid.uuid4()))
                        .sequence(sequence)
                        .build()
                ).build()

            response = await self._execute_async(
                self.client.cardkit.v1.card_element.content, update_request
            )

            if response.code != 0:
                logger.warning("Timer update failed", code=response.code)
                return False
            return True

        except Exception as e:
            logger.error("Error updating timer", error=str(e))
            return False

    async def _update_card_subtitle(
        self, card_id: str, subtitle: str, template: str = "blue"
    ) -> bool:
        """Update card header subtitle and color via cardkit API.

        Uses cardkit.card.update to update the card's header subtitle.
        This must be called AFTER streaming mode is closed.

        Args:
            card_id: Card ID to update
            subtitle: New subtitle text (e.g., "Done · 31.0s")
            template: Header color template (blue/green/red/yellow)
        """
        try:
            # Build card with just header update
            card_json = {
                "schema": "2.0",
                "header": {
                    "title": {"content": "Claude Code", "tag": "plain_text"},
                    "subtitle": {"content": subtitle, "tag": "plain_text"},
                    "template": template
                }
            }

            from lark_oapi.api.cardkit.v1 import (
                UpdateCardRequest,
                UpdateCardRequestBody,
            )

            update_request = UpdateCardRequest.builder() \
                .card_id(card_id) \
                .request_body(
                    UpdateCardRequestBody.builder()
                        .card(card_json)  # Use card() not type()/data()
                        .uuid(str(uuid.uuid4()))
                        .build()
                ).build()

            response = await self._execute_async(
                self.client.cardkit.v1.card.update, update_request
            )

            if response.code != 0:
                logger.warning("Card subtitle update failed", code=response.code, msg=response.msg)
                return False
            return True

        except Exception as e:
            logger.error("Error updating card subtitle", error=str(e))
            return False

    async def _close_streaming_mode(self, card_id: str, sequence: int) -> bool:
        """Close streaming mode and remove the stop button and loading icon.

        Keeps the timer element to show final status/time.
        """
        try:
            # Delete the loading icon element
            await self._delete_card_element(card_id, "loading_icon", sequence)

            # Delete the stop button element
            await self._delete_card_element(card_id, "stop_button", sequence + 1)

            # Close streaming mode (sequence + 2 for next operation)
            settings_request = SettingsCardRequest.builder() \
                .card_id(card_id) \
                .request_body(
                    SettingsCardRequestBody.builder()
                        .settings(json.dumps({"config": {"streaming_mode": False}}))
                        .uuid(str(uuid.uuid4()))
                        .sequence(sequence + 2)
                        .build()
                ).build()

            response = await self._execute_async(
                self.client.cardkit.v1.card.settings, settings_request
            )

            if response.code != 0:
                logger.warning("Failed to close streaming", code=response.code)
                return False

            logger.info("Closed streaming mode", card_id=card_id)
            return True

        except Exception as e:
            logger.error("Error closing streaming", error=str(e))
            return False

    async def _delete_card_element(self, card_id: str, element_id: str, sequence: int) -> bool:
        """Delete a card element using DELETE API with sequence."""
        try:
            from lark_oapi.api.cardkit.v1 import (
                DeleteCardElementRequest,
                DeleteCardElementRequestBody,
            )

            delete_request = DeleteCardElementRequest.builder() \
                .card_id(card_id) \
                .element_id(element_id) \
                .request_body(
                    DeleteCardElementRequestBody.builder()
                    .sequence(sequence)
                    .build()
                ).build()

            response = await self._execute_async(
                self.client.cardkit.v1.card_element.delete, delete_request
            )

            if response.code != 0:
                logger.warning("Failed to delete card element", code=response.code, element_id=element_id)
                return False

            logger.info("Deleted card element", card_id=card_id, element_id=element_id, sequence=sequence)
            return True

        except Exception as e:
            logger.error("Error deleting card element", error=str(e), element_id=element_id)
            return False

    async def _remove_stop_button(self, card_id: str, sequence: int = 1) -> bool:
        """Delete the stop button element using DELETE API with sequence."""
        return await self._delete_card_element(card_id, "stop_button", sequence)

    async def _process_with_fallback(
        self,
        chat_id: str,
        ctx,
        user_id: int,
        start_time: float,
        interrupt_event: asyncio.Event,
    ) -> None:
        """Fallback to simple text message when card creation fails."""
        from src.bot.core_engine import StreamEvent
        import time

        # Send initial message
        progress_msg = await self.send_message(chat_id, "Working...")

        full_content = ""

        async def on_stream(event: StreamEvent) -> None:
            nonlocal full_content
            if interrupt_event.is_set():
                return
            if event.type == "response" and event.content:
                content = event.content
                # Filter out SDK internal messages
                if content.startswith("[ThinkingBlock") or content.startswith("[ContentBlock"):
                    return
                if content.startswith("[") and "]" in content[:30] and not content.startswith("[Error"):
                    return
                full_content += content

        try:
            response = await self.core_engine.process_message(
                ctx, on_stream, interrupt_event=interrupt_event
            )

            # Save session_id for continuation
            if response.session_id:
                self._set_session_id(user_id, response.session_id)

            elapsed = time.time() - start_time
            status = "Done" if response.success else ("Interrupted" if response.interrupted else "Error")
            final_content = response.content if response.content else full_content

            # Send final response
            await self.send_message(chat_id, f"[{status}] {elapsed:.1f}s\n\n{final_content}")

        except Exception as e:
            elapsed = time.time() - start_time
            await self.send_message(chat_id, f"[Error] {elapsed:.1f}s\n\n{str(e)}")

    def _build_progress_card(
        self,
        elapsed: float,
        activities: List[Dict[str, Any]],
        is_running: bool,
        user_id: int,
    ) -> Dict[str, Any]:
        """Build Lark card for progress display."""
        # Build activity text
        activity_lines = []
        for entry in activities[-10:]:
            kind = entry.get("kind", "tool")
            if kind == "tool":
                name = entry.get("name", "unknown")
                detail = entry.get("detail", "")
                if detail:
                    activity_lines.append(f"[{name}]\n{detail[:80]}")
                else:
                    activity_lines.append(f"[{name}]")
            elif kind == "text":
                activity_lines.append(f"{entry.get('detail', '')[:80]}")
            elif kind == "error":
                activity_lines.append(f"[Error] {entry.get('detail', '')[:80]}")

        activity_text = "\n\n".join(activity_lines) if activity_lines else "等待响应..."

        # Build card
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": "Claude Code", "tag": "plain_text"},
                "subtitle": {"content": f"{elapsed:.1f}s", "tag": "plain_text"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"content": activity_text, "tag": "plain_text"},
                },
            ],
        }

        return card

    def _build_result_card(
        self,
        content: str,
        elapsed: float,
        success: bool,
        interrupted: bool,
        tool_count: int,
    ) -> Dict[str, Any]:
        """Build Lark card for final result display."""
        # Truncate if too long
        if len(content) > 3500:
            content = content[:3500] + "\n\n... (内容过长，已截断)"

        # Build header based on status
        if interrupted:
            header_title = "已中断"
        elif success:
            header_title = "完成"
        else:
            header_title = "出错"

        header_subtitle = f"{elapsed:.1f}s | {tool_count} tools"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": header_title, "tag": "plain_text"},
                "subtitle": {"content": header_subtitle, "tag": "plain_text"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"content": content, "tag": "plain_text"},
                },
            ],
        }

        return card

    def _get_tool_icon(self, tool_name: str) -> str:
        """Get icon for a tool (no emoji)."""
        return f"[{tool_name}]"

    def _parse_lark_event(self, data: Any) -> Dict[str, Any]:
        """Parse lark event data to dict format."""
        # Handle P2ImMessageReceiveV1 event
        if hasattr(data, 'event'):
            event = data.event
            result = {
                "type": "im.message.receive_v1",
                "message": {},
                "sender": {},
            }

            # Extract message info
            if hasattr(event, 'message'):
                msg = event.message
                result["message"] = {
                    "message_id": getattr(msg, 'message_id', ''),
                    "chat_id": getattr(msg, 'chat_id', ''),
                    "msg_type": getattr(msg, 'message_type', 'text'),
                    "content": getattr(msg, 'content', '{}'),
                    "create_time": getattr(msg, 'create_time', ''),
                }

            # Extract sender info - note: sender_id is nested
            if hasattr(event, 'sender'):
                sender = event.sender
                sender_id = getattr(sender, 'sender_id', None)
                if sender_id:
                    result["sender"] = {
                        "open_id": getattr(sender_id, 'open_id', ''),
                        "user_id": getattr(sender_id, 'user_id', ''),
                        "union_id": getattr(sender_id, 'union_id', ''),
                    }
                else:
                    # Fallback for different event formats
                    result["sender"] = {
                        "open_id": getattr(sender, 'open_id', ''),
                        "user_id": getattr(sender, 'user_id', ''),
                        "union_id": getattr(sender, 'union_id', ''),
                    }

            logger.debug("Parsed Lark event", event_data=result)
            return result

        # Fallback: try to marshal to JSON
        try:
            if lark and hasattr(lark, 'JSON'):
                parsed = json.loads(lark.JSON.marshal(data))
                logger.debug("Parsed Lark event via JSON marshal", event_data=parsed)
                return parsed
        except Exception as e:
            logger.warning("Failed to parse via JSON marshal", error=str(e))

        return {"type": "unknown", "raw": str(data)}

    async def start(self) -> None:
        """Start receiving events from Lark using WebSocket long polling."""
        if self._is_running:
            logger.warning("Lark adapter already running")
            return

        await self.initialize()

        # Save the main event loop reference for thread-safe handler dispatch
        self._main_loop = asyncio.get_running_loop()

        # Monkey-patch SDK: CallBackAction.value is typed as Dict[str, Any] but
        # Lark cardkit actually returns a string. Change to Any to prevent
        # deserialization errors in EventDispatcherHandler.do_without_validation().
        from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackAction
        CallBackAction._types["value"] = Any

        logger.info("Starting Lark adapter", mode="websocket_long_polling")

        # Build event handler - register both message and card action handlers
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_message_received) \
            .register_p2_card_action_trigger(self._on_card_action) \
            .build()

        # Create WebSocket client
        self.ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG,
        )

        self._is_running = True

        # Run WebSocket client in a separate thread
        # because lark.ws.Client.start() is blocking
        def run_ws_client():
            try:
                logger.info("WebSocket client starting...")
                self.ws_client.start()
            except Exception as e:
                logger.error("WebSocket client error", error=str(e))
                self._is_running = False

        self._ws_thread = threading.Thread(target=run_ws_client, daemon=True)
        self._ws_thread.start()

        logger.info("Lark adapter started (WebSocket mode)")

        # Wait for stop signal
        await self._stop_event.wait()

    def _on_card_action(self, data: Any) -> Any:
        """Handle card button callbacks. Must return P2CardActionTriggerResponse."""
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse, CallBackToast

        try:
            logger.info("Card action received", data_type=type(data).__name__)

            # Extract action value
            action_value = None
            if hasattr(data, 'event') and hasattr(data.event, 'action'):
                action = data.event.action
                if hasattr(action, 'value'):
                    action_value = action.value

            logger.info("Card action value", value=action_value, value_type=type(action_value).__name__)

            # Parse action - support both string and dict formats
            action_type = None
            user_id = None

            if isinstance(action_value, dict):
                # New schema 2.0 format: {"action": "stop", "user_id": 123}
                action_type = action_value.get("action")
                user_id = action_value.get("user_id")
            elif isinstance(action_value, str):
                # Lark cardkit double-encodes JSON strings: the value is itself a JSON
                # string encoding of another JSON string. Parse twice.
                try:
                    parsed = json.loads(action_value)
                    if isinstance(parsed, str):
                        parsed = json.loads(parsed)
                    if isinstance(parsed, dict):
                        action_type = parsed.get("action")
                        user_id = parsed.get("user_id")
                except (json.JSONDecodeError, ValueError):
                    # Legacy string format: 'stop:123'
                    if action_value.startswith("stop:"):
                        action_type = "stop"
                        try:
                            user_id = int(action_value.split(":")[1])
                        except ValueError:
                            pass

            logger.info("Parsed card action", action_type=action_type, user_id=user_id)

            # Handle stop action
            if action_type == "stop" and user_id is not None:
                if user_id in self._stop_callbacks:
                    import time
                    interrupt_event = self._stop_callbacks[user_id]
                    interrupt_event.set()
                    logger.info("Stop requested", user_id=user_id)

                    # Immediately update card to show interrupting status
                    if user_id in self._streaming_cards:
                        card_id, _, update_seq = self._streaming_cards[user_id]
                        if card_id:
                            elapsed = time.time() - self._streaming_start_time.get(user_id, time.time())
                            status_text = "⏹ 正在中断..."
                            # Inline format_with_status (defined in _process_with_core_engine scope, not accessible here)
                            interrupting_display = f"**{status_text} · {elapsed:.0f}s**\n\n用户已请求中断，正在停止 Claude...\n\n请稍候。"
                            async def _update_stop_card():
                                try:
                                    await self._update_card_content(card_id, interrupting_display, update_seq[0] + 1)
                                except Exception as e:
                                    logger.warning("Failed to update card on stop", error=str(e))
                            asyncio.ensure_future(_update_stop_card())

                    resp = P2CardActionTriggerResponse()
                    resp.toast = CallBackToast({"type": "info", "content": "正在中断请求..."})
                    return resp
                else:
                    logger.warning("No stop callback for user", user_id=user_id)
                    resp = P2CardActionTriggerResponse()
                    resp.toast = CallBackToast({"type": "warning", "content": "没有正在进行的请求"})
                    return resp

            # Handle other actions via async dispatch to main loop
            action_param = action_value.get("param", "") if isinstance(action_value, dict) else ""
            open_chat_id = ""
            if hasattr(data, 'event') and hasattr(data.event, 'operator_id'):
                # Try to extract chat_id from the event
                pass

            if action_type and self._main_loop and not self._main_loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._handle_card_action_async(action_type, action_param, user_id),
                    self._main_loop
                )

            # Default response
            resp = P2CardActionTriggerResponse()
            resp.toast = CallBackToast({"type": "info", "content": "操作已收到"})
            return resp

        except Exception as e:
            logger.error("Error processing card action", error=str(e), exc_info=True)
            # Return error response
            resp = P2CardActionTriggerResponse()
            resp.toast = CallBackToast({"type": "error", "content": f"处理失败: {str(e)}"})
            return resp

    async def _handle_card_action_async(
        self, action_type: str, action_param: str, user_id: Optional[int]
    ) -> None:
        """Handle card button actions asynchronously (dispatched from WebSocket and webhook)."""
        if not self.settings:
            return

        try:
            if action_type == "cd":
                # CD action — change directory
                if not action_param:
                    return
                user_data = self._get_user_data(user_id) if user_id else {}
                current_dir = Path(self._get_working_directory(user_id)) if user_id else Path(self.settings.approved_directory)
                approved_dir = Path(self.settings.approved_directory)

                try:
                    if action_param == "/":
                        resolved = approved_dir
                    elif action_param == "..":
                        resolved = current_dir.parent
                        try:
                            resolved.relative_to(approved_dir.resolve())
                        except ValueError:
                            resolved = approved_dir
                    else:
                        resolved = (current_dir / action_param).resolve()
                        try:
                            resolved.relative_to(approved_dir.resolve())
                        except ValueError:
                            return

                    if resolved.exists() and resolved.is_dir():
                        if user_id:
                            self._set_working_directory(user_id, str(resolved))
                            self._set_session_id(user_id, None)
                        try:
                            relative = resolved.relative_to(approved_dir)
                            display = "/" if str(relative) == "." else f"{relative}/"
                        except ValueError:
                            display = str(resolved)
                        logger.info("CD via card callback", directory=str(resolved))
                    else:
                        logger.warning("CD target not found", target=action_param)
                except Exception as e:
                    logger.error("CD callback error", error=str(e))

            elif action_type == "action":
                # Generic action — dispatch as command through command handlers
                action_map = {
                    "show_projects": "/projects",
                    "help": "/help",
                    "new_session": "/new",
                    "status": "/status",
                    "ls": "/ls",
                    "refresh_status": "/status",
                    "refresh_ls": "/ls",
                }
                cmd_text = action_map.get(action_param)
                if cmd_text and self._command_handlers:
                    # Simulate as a message event and dispatch through command handlers
                    event_data = {
                        "type": "im.message.receive_v1",
                        "message": {
                            "chat_id": "",
                            "content": json.dumps({"text": cmd_text}),
                            "msg_type": "text",
                        },
                        "sender": {"open_id": str(user_id or 0)},
                    }
                    for handler in self._command_handlers:
                        try:
                            await handler(event_data)
                        except Exception as e:
                            logger.error("Card action handler error", error=str(e))
                else:
                    logger.warning("Unknown card action or no command handlers", action=action_param)

            elif action_type == "quick":
                # Quick actions — send prompt to Claude
                quick_prompts = {
                    "review": "请审查当前目录的代码",
                    "test": "请运行项目测试",
                    "docs": "请为项目生成 README 文档",
                    "fix": "请检查并修复代码问题",
                    "build": "请构建项目",
                    "start": "请启动开发服务器",
                    "lint": "请运行代码检查",
                    "format": "请格式化代码",
                }
                prompt = quick_prompts.get(action_param)
                if prompt and user_id:
                    # Route through core engine
                    sender = {"open_id": str(user_id)}
                    await self._process_with_core_engine("", prompt, sender)
                else:
                    logger.warning("Unknown quick action", action=action_param)

            else:
                logger.debug("Unrecognized card action type", action_type=action_type)

        except Exception as e:
            logger.error("Error in card action handler", error=str(e), exc_info=True)

    async def handle_card_callback(self, payload: Dict[str, Any]) -> None:
        """Handle card action callback from webhook.

        Supports multiple callback types:
        - stop:user_id - Interrupt active request
        - cd:directory - Change directory
        - action:name - Execute named action
        - quick:name - Quick action
        - git:command - Git operation
        - export:format - Export session
        - confirm:yes/no - Confirmation response

        Supports both string format ("stop:123") and dict format ({"action": "stop", "user_id": 123})

        Args:
            payload: Card action payload from Lark webhook
        """
        logger.info("Handling card callback", payload=payload)

        try:
            # Extract action value from Lark card callback format
            action = payload.get("action", {})
            action_value = action.get("value", "")
            open_message_id = payload.get("open_message_id", "")
            open_chat_id = payload.get("open_chat_id", "")

            logger.info("Card callback action value", value=action_value, chat_id=open_chat_id)

            if not action_value:
                logger.warning("Empty action value in card callback")
                return

            # Parse action type and parameters - support dict, JSON string, and legacy string formats
            if isinstance(action_value, dict):
                # New schema 2.0 format: {"action": "stop", "user_id": 123}
                action_type = action_value.get("action", "")
                action_param = action_value.get("user_id", "") or action_value.get("param", "")
            elif isinstance(action_value, str):
                # Lark cardkit double-encodes JSON strings. Parse twice.
                try:
                    parsed = json.loads(action_value)
                    if isinstance(parsed, str):
                        parsed = json.loads(parsed)
                    if isinstance(parsed, dict):
                        action_type = parsed.get("action", "")
                        action_param = parsed.get("user_id", "") or parsed.get("param", "")
                    else:
                        action_type = action_value
                        action_param = ""
                except (json.JSONDecodeError, ValueError):
                    # Legacy string format: "stop:123"
                    if ":" in action_value:
                        action_type, action_param = action_value.split(":", 1)
                    else:
                        action_type = action_value
                        action_param = ""
            else:
                action_type = action_value
                action_param = ""

            # --- Stop action (interrupt active request) ---
            if action_type == "stop":
                try:
                    import time
                    user_id = int(action_param) if action_param else 0
                    if user_id in self._stop_callbacks:
                        interrupt_event = self._stop_callbacks[user_id]
                        interrupt_event.set()
                        logger.info("Stop requested via card callback", user_id=user_id)
                        # Update streaming card if available
                        if user_id in self._streaming_cards:
                            card_id, _, update_seq = self._streaming_cards[user_id]
                            if card_id:
                                elapsed = time.time() - self._streaming_start_time.get(user_id, time.time())
                                stop_display = f"**⏹ 正在中断... · {elapsed:.0f}s**\n\n用户已请求中断，正在停止 Claude..."
                                await self._update_card_content(card_id, stop_display, update_seq[0] + 1)
                    else:
                        logger.warning("No active request for user", user_id=user_id)
                        await self.send_message(open_chat_id, "⚠️ 没有正在进行的请求")
                except ValueError:
                    logger.warning("Invalid user_id in stop action", value=action_value)

            # --- CD action (change directory) ---
            elif action_type == "cd":
                if not action_param:
                    await self.send_message(open_chat_id, "❌ 未指定目录")
                    return

                directory = action_param
                # This would be handled by the orchestrator's _cmd_cd
                # For now, just acknowledge
                await self.send_message(open_chat_id, f"📁 切换目录: {directory}")
                logger.info("CD action requested", directory=directory)

            # --- Action action (generic action) ---
            elif action_type == "action":
                action_name = action_param
                action_handlers = {
                    "show_projects": "请列出所有项目",
                    "help": "显示帮助信息",
                    "new_session": "请开始新会话",
                    "status": "显示会话状态",
                }
                if action_name in action_handlers:
                    # Trigger the action as a message
                    await self.send_message(open_chat_id, f"⚡ 执行操作: {action_name}")
                else:
                    await self.send_message(open_chat_id, f"❌ 未知操作: {action_name}")
                logger.info("Action triggered", action=action_name)

            # --- Quick action ---
            elif action_type == "quick":
                quick_name = action_param
                quick_actions = {
                    "review": "请审查当前目录的代码",
                    "test": "请运行项目测试",
                    "docs": "请为项目生成 README 文档",
                    "fix": "请检查并修复代码问题",
                }
                if quick_name in quick_actions:
                    await self.send_message(open_chat_id, f"⚡ 快速操作: {quick_actions[quick_name]}")
                else:
                    await self.send_message(open_chat_id, f"❌ 未知快速操作: {quick_name}")
                logger.info("Quick action triggered", action=quick_name)

            # --- Git action ---
            elif action_type == "git":
                git_cmd = action_param or "status"
                await self.send_message(open_chat_id, f"🔧 执行 Git 命令: /git {git_cmd}")
                logger.info("Git action triggered", command=git_cmd)

            # --- Export action ---
            elif action_type == "export":
                export_format = action_param or "markdown"
                await self.send_message(open_chat_id, f"📤 导出会话格式: {export_format}")
                logger.info("Export action triggered", format=export_format)

            # --- Confirm action ---
            elif action_type == "confirm":
                response = action_param.lower() if action_param else "no"
                if response in ("yes", "y", "true", "1"):
                    await self.send_message(open_chat_id, "✅ 已确认")
                else:
                    await self.send_message(open_chat_id, "❌ 已取消")
                logger.info("Confirm action", response=response)

            # --- Followup action (suggested next steps) ---
            elif action_type == "followup":
                followup_text = action_param
                if followup_text:
                    await self.send_message(open_chat_id, f"💡 后续建议: {followup_text}")
                logger.info("Followup action", text=followup_text)

            # --- Unknown action type ---
            else:
                logger.warning("Unknown action type in card callback", action_type=action_type)
                await self.send_message(open_chat_id, f"❓ 未知操作类型: {action_type}")

        except Exception as e:
            logger.error("Error handling card callback", error=str(e), exc_info=True)
            try:
                chat_id = payload.get("open_chat_id", "")
                if chat_id:
                    await self.send_message(chat_id, f"❌ 处理回调时出错: {str(e)}")
            except Exception:
                pass

    async def stop(self) -> None:
        """Stop Lark adapter."""
        if not self._is_running:
            return

        logger.info("Stopping Lark adapter")
        self._is_running = False

        # Signal the start() method to exit
        self._stop_event.set()

        # WebSocket client doesn't have a clean stop method
        # The thread is daemon, so it will be killed when main thread exits
        logger.info("Lark adapter stopped")

    def register_event_handler(self, handler: Callable) -> None:
        """Register an event handler to receive Lark events."""
        self._event_handlers.append(handler)

    async def register_message_handler(
        self,
        handler: Callable,
        **kwargs: Any,
    ) -> None:
        """Register message handler for WebSocket events."""
        self._message_handlers.append(handler)
        logger.info("Registered message handler for Lark WebSocket")

    async def register_command_handler(
        self,
        commands: List[str],
        handler: Callable,
        **kwargs: Any,
    ) -> None:
        """Register command handler - commands are messages starting with /."""
        self._command_handlers.append(handler)
        self._registered_commands.extend(commands)
        logger.info("Registered command handler for Lark", commands=commands)

    async def register_callback_handler(
        self,
        handler: Callable,
        **kwargs: Any,
    ) -> None:
        """Register callback handler for card button interactions."""
        self._callback_handlers.append(handler)
        logger.info("Registered callback handler for Lark")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: Optional[str] = None,
        disable_preview: bool = False,
        reply_to_message_id: Optional[str] = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Send text message to Lark chat as an interactive card.

        All messages are converted to card format for better visual presentation.
        """
        try:
            # Build card JSON from text
            # Parse emoji icons from the beginning of the text
            icon = "💬"
            title = "消息"
            content_text = text

            # Extract emoji icon from the beginning
            if text and len(text) > 2:
                first_char = text[0]
                if first_char in "👋🆕📊📁📄✅❌⚠️💡⚡💰📍🔧📤🔄ℹ️🏁🧵📋🌳🚫🎯":
                    icon = first_char
                    # Find title (first line after icon)
                    rest = text[1:].strip()
                    if "\n" in rest:
                        first_line_end = rest.find("\n")
                        title = rest[:first_line_end].strip()
                        content_text = rest[first_line_end + 1:].strip()
                    else:
                        title = rest[:50] if len(rest) > 50 else rest
                        content_text = ""
                else:
                    content_text = text

            # Build card
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"content": f"{icon} {title}", "tag": "plain_text"},
                    "template": self._get_card_template(icon)
                },
                "elements": []
            }

            # Add content if exists
            if content_text:
                # Convert HTML to Lark markdown format
                card_content = content_text.replace("<b>", "**").replace("</b>", "**")
                card_content = card_content.replace("<code>", "`").replace("</code>", "`")
                card["elements"].append({
                    "tag": "markdown",
                    "content": card_content
                })
            else:
                # Just show title if no content
                card["elements"].append({
                    "tag": "markdown",
                    "content": title
                })

            # Send as interactive card
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("interactive")
                        .content(json.dumps(card))
                        .build()
                ) \
                .build()

            response = await self._execute_async(
                self.client.im.v1.message.create,
                request
            )

            if response.code == 0:
                return PlatformResponse(
                    success=True,
                    message_id=response.data.message_id,
                    raw={"message_id": response.data.message_id},
                )
            else:
                return PlatformResponse(
                    success=False,
                    error=f"Code {response.code}: {response.msg}",
                )
        except Exception as e:
            logger.error("Failed to send message", error=str(e))
            return PlatformResponse(success=False, error=str(e))

    def _get_card_template(self, icon: str) -> str:
        """Get card header template color based on message icon.

        Args:
            icon: Emoji icon from the message

        Returns:
            Template color string
        """
        template_map = {
            "👋": "blue",      # Welcome
            "🆕": "turquoise", # New
            "📊": "purple",    # Status
            "📁": "blue",      # Directory
            "📄": "blue",      # File
            "✅": "green",     # Success
            "❌": "red",       # Error
            "⚠️": "yellow",    # Warning
            "💡": "wathet",    # Tip
            "⚡": "indigo",    # Action
            "💰": "orange",    # Cost
            "📍": "blue",      # Location
            "🔧": "blue",      # Settings
            "📤": "turquoise", # Export
            "💬": "blue",      # Message (default)
        }
        return template_map.get(icon, "blue")

    async def send_card(
        self,
        chat_id: str,
        card: Union[PlatformCard, Dict[str, Any]],
        **kwargs: Any,
    ) -> PlatformResponse:
        """Send interactive card to Lark chat.

        Args:
            chat_id: Chat ID to send to
            card: Either a PlatformCard object or a dict with Lark card format
        """
        try:
            # Build Lark card format - accept both PlatformCard and dict
            if isinstance(card, dict):
                card_content = card
            else:
                card_content = self._build_lark_card(card)

            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("interactive")
                        .content(json.dumps(card_content))
                        .build()
                ) \
                .build()

            response = await self._execute_async(
                self.client.im.v1.message.create,
                request
            )

            if response.code == 0:
                return PlatformResponse(
                    success=True,
                    message_id=response.data.message_id,
                    raw={"message_id": response.data.message_id},
                )
            else:
                return PlatformResponse(
                    success=False,
                    error=f"Code {response.code}: {response.msg}",
                )
        except Exception as e:
            logger.error("Failed to send card", error=str(e))
            return PlatformResponse(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: Optional[str] = None,
        card: Optional[PlatformCard] = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Edit existing message."""
        try:
            # Build content based on type
            if card:
                content = self._build_lark_card(card)
                msg_type = "interactive"
            else:
                content = {"text": text or ""}
                msg_type = "text"

            request = UpdateMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    UpdateMessageRequestBody.builder()
                        .msg_type(msg_type)
                        .content(json.dumps(content))
                        .build()
                ) \
                .build()

            response = await self._execute_async(
                self.client.im.v1.message.update,
                request
            )

            if response.code == 0:
                return PlatformResponse(success=True)
            else:
                return PlatformResponse(
                    success=False,
                    error=f"Code {response.code}: {response.msg}",
                )
        except Exception as e:
            logger.error("Failed to edit message", error=str(e))
            return PlatformResponse(success=False, error=str(e))

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Delete a message."""
        try:
            request = DeleteMessageRequest.builder() \
                .message_id(message_id) \
                .build()

            response = await self._execute_async(
                self.client.im.v1.message.delete,
                request
            )

            if response.code == 0:
                return PlatformResponse(success=True)
            else:
                return PlatformResponse(
                    success=False,
                    error=f"Code {response.code}: {response.msg}",
                )
        except Exception as e:
            logger.error("Failed to delete message", error=str(e))
            return PlatformResponse(success=False, error=str(e))

    async def edit_card(
        self,
        chat_id: str,
        message_id: str,
        card: Dict[str, Any],
        **kwargs: Any,
    ) -> PlatformResponse:
        """Edit message to show a card (accepts dict card format)."""
        try:
            request = UpdateMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    UpdateMessageRequestBody.builder()
                        .msg_type("interactive")
                        .content(json.dumps(card))
                        .build()
                ) \
                .build()

            response = await self._execute_async(
                self.client.im.v1.message.update,
                request
            )

            if response.code == 0:
                return PlatformResponse(success=True)
            else:
                return PlatformResponse(
                    success=False,
                    error=f"Code {response.code}: {response.msg}",
                )
        except Exception as e:
            logger.error("Failed to edit card", error=str(e))
            return PlatformResponse(success=False, error=str(e))

    async def send_file(
        self,
        chat_id: str,
        file: Union[PlatformFile, bytes, str],
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Send file to Lark chat.

        Args:
            chat_id: Target chat ID
            file: Either PlatformFile, bytes, or file path string
            filename: Optional filename (required for bytes input)
            caption: Optional caption text

        Returns:
            PlatformResponse with success status and message_id
        """
        import aiohttp
        import os

        try:
            # Prepare file data
            if isinstance(file, PlatformFile):
                file_data = file.file_data
                filename = filename or file.file_name
            elif isinstance(file, bytes):
                file_data = file
                if not filename:
                    filename = f"file_{uuid.uuid4().hex[:8]}"
            else:
                # File path
                with open(file, 'rb') as f:
                    file_data = f.read()
                filename = filename or os.path.basename(file)

            # Get tenant access token
            tenant_token = await self._get_tenant_access_token()
            if not tenant_token:
                return PlatformResponse(
                    success=False,
                    error="Failed to get tenant access token"
                )

            # Upload file using Lark API
            upload_url = "https://open.larksuite.com/open-apis/im/v1/files"
            headers = {
                "Authorization": f"Bearer {tenant_token}",
            }

            # Determine file type based on extension
            file_ext = os.path.splitext(filename)[1].lower()
            if file_ext in {'.png', '.jpg', '.jpeg', '.gif', '.bmp'}:
                file_type = "image"
            elif file_ext in {'.mp4', '.mov', '.avi'}:
                file_type = "video"
            elif file_ext in {'.mp3', '.wav', '.aac'}:
                file_type = "audio"
            else:
                file_type = "stream"

            form_data = aiohttp.FormData()
            form_data.add_field(
                'file',
                file_data,
                filename=filename,
                content_type='application/octet-stream'
            )
            form_data.add_field('file_name', filename)
            form_data.add_field('file_type', file_type)

            async with aiohttp.ClientSession() as session:
                async with session.post(upload_url, headers=headers, data=form_data) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error("File upload failed", status=resp.status, error=error_text)
                        return PlatformResponse(
                            success=False,
                            error=f"Upload failed: {resp.status}"
                        )

                    result = await resp.json()
                    if result.get("code") != 0:
                        logger.error("File upload API error", response=result)
                        return PlatformResponse(
                            success=False,
                            error=f"API error: {result.get('msg', 'Unknown error')}"
                        )

                    file_key = result.get("data", {}).get("file", {}).get("file_key")
                    if not file_key:
                        return PlatformResponse(
                            success=False,
                            error="No file_key in response"
                        )

            # Send file message
            content = json.dumps({
                "file_key": file_key
            })

            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("file")
                        .content(content)
                        .build()
                ) \
                .build()

            response = await self._execute_async(
                self.client.im.v1.message.create,
                request
            )

            if response.code == 0:
                logger.info("File sent successfully", file_key=file_key, message_id=response.data.message_id)
                return PlatformResponse(
                    success=True,
                    message_id=response.data.message_id,
                    raw={"file_key": file_key, "message_id": response.data.message_id},
                )
            else:
                return PlatformResponse(
                    success=False,
                    error=f"Message send failed: Code {response.code}: {response.msg}",
                )

        except Exception as e:
            logger.error("Failed to send file", error=str(e), exc_info=True)
            return PlatformResponse(success=False, error=str(e))

    async def download_file(self, file_key: str, **kwargs: Any) -> Optional[bytes]:
        """Download file from Lark.

        Args:
            file_key: The file_key from Lark file message

        Returns:
            File content as bytes, or None if download failed
        """
        import aiohttp

        try:
            # Get tenant access token
            tenant_token = await self._get_tenant_access_token()
            if not tenant_token:
                logger.error("Failed to get tenant access token")
                return None

            # Get file download URL
            headers = {
                "Authorization": f"Bearer {tenant_token}",
            }

            # Use GetFileRequest to get file info
            request = GetFileRequest.builder() \
                .file_key(file_key) \
                .build()

            response = await self._execute_async(
                self.client.im.v1.file.get,
                request
            )

            if response.code != 0:
                logger.error("Failed to get file info", code=response.code, msg=response.msg)
                return None

            # Get download URL from response
            file_url = None
            if hasattr(response, 'data') and response.data:
                # The file object contains temporary_download_url
                if hasattr(response.data, 'file') and response.data.file:
                    file_url = getattr(response.data.file, 'temporary_download_url', None)

            if not file_url:
                logger.error("No download URL in response")
                return None

            # Download the file
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as resp:
                    if resp.status != 200:
                        logger.error("File download failed", status=resp.status)
                        return None
                    return await resp.read()

        except Exception as e:
            logger.error("Failed to download file", error=str(e), exc_info=True)
            return None

    async def _get_tenant_access_token(self) -> Optional[str]:
        """Get tenant access token for Lark API."""
        try:
            # Use the internal auth API to get tenant token
            import aiohttp

            url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
            payload = {
                "app_id": self.app_id,
                "app_secret": self.app_secret
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        logger.error("Failed to get tenant token", status=resp.status)
                        return None

                    result = await resp.json()
                    if result.get("code") != 0:
                        logger.error("Auth API error", response=result)
                        return None

                    return result.get("tenant_access_token")

        except Exception as e:
            logger.error("Error getting tenant token", error=str(e))
            return None

    async def send_image(
        self,
        chat_id: str,
        image: Union[bytes, str],
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Send image to Lark chat.

        Args:
            chat_id: Target chat ID
            image: Either bytes or file path string
            caption: Optional caption text

        Returns:
            PlatformResponse with success status and message_id
        """
        import aiohttp
        import os

        try:
            # Prepare image data
            if isinstance(image, bytes):
                image_data = image
            else:
                # File path
                with open(image, 'rb') as f:
                    image_data = f.read()

            # Get tenant access token
            tenant_token = await self._get_tenant_access_token()
            if not tenant_token:
                return PlatformResponse(
                    success=False,
                    error="Failed to get tenant access token"
                )

            # Upload image using Lark API
            upload_url = "https://open.larksuite.com/open-apis/im/v1/images"
            headers = {
                "Authorization": f"Bearer {tenant_token}",
            }

            form_data = aiohttp.FormData()
            form_data.add_field(
                'image',
                image_data,
                filename='image.png',
                content_type='image/png'
            )
            form_data.add_field('image_type', 'message')

            async with aiohttp.ClientSession() as session:
                async with session.post(upload_url, headers=headers, data=form_data) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error("Image upload failed", status=resp.status, error=error_text)
                        return PlatformResponse(
                            success=False,
                            error=f"Upload failed: {resp.status}"
                        )

                    result = await resp.json()
                    if result.get("code") != 0:
                        logger.error("Image upload API error", response=result)
                        return PlatformResponse(
                            success=False,
                            error=f"API error: {result.get('msg', 'Unknown error')}"
                        )

                    image_key = result.get("data", {}).get("image", {}).get("image_key")
                    if not image_key:
                        return PlatformResponse(
                            success=False,
                            error="No image_key in response"
                        )

            # Send image message
            content = json.dumps({
                "image_key": image_key
            })

            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("image")
                        .content(content)
                        .build()
                ) \
                .build()

            response = await self._execute_async(
                self.client.im.v1.message.create,
                request
            )

            if response.code == 0:
                logger.info("Image sent successfully", image_key=image_key, message_id=response.data.message_id)
                return PlatformResponse(
                    success=True,
                    message_id=response.data.message_id,
                    raw={"image_key": image_key, "message_id": response.data.message_id},
                )
            else:
                return PlatformResponse(
                    success=False,
                    error=f"Message send failed: Code {response.code}: {response.msg}",
                )

        except Exception as e:
            logger.error("Failed to send image", error=str(e), exc_info=True)
            return PlatformResponse(success=False, error=str(e))

    async def download_image(self, image_key: str) -> Optional[bytes]:
        """Download image from Lark.

        Args:
            image_key: The image_key from Lark image message

        Returns:
            Image content as bytes, or None if download failed
        """
        import aiohttp

        try:
            # Get tenant access token
            tenant_token = await self._get_tenant_access_token()
            if not tenant_token:
                logger.error("Failed to get tenant access token")
                return None

            # Get image download URL
            request = GetImageRequest.builder() \
                .image_key(image_key) \
                .build()

            response = await self._execute_async(
                self.client.im.v1.image.get,
                request
            )

            if response.code != 0:
                logger.error("Failed to get image info", code=response.code, msg=response.msg)
                return None

            # Get download URL from response
            image_url = None
            if hasattr(response, 'data') and response.data:
                if hasattr(response.data, 'image') and response.data.image:
                    image_url = getattr(response.data.image, 'temporary_download_url', None)

            if not image_url:
                logger.error("No download URL in response")
                return None

            # Download the image
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        logger.error("Image download failed", status=resp.status)
                        return None
                    return await resp.read()

        except Exception as e:
            logger.error("Failed to download image", error=str(e), exc_info=True)
            return None

    async def _process_file_content(
        self,
        file_data: bytes,
        file_name: str,
        original_text: str,
    ) -> str:
        """Process uploaded file content and return formatted text for Claude.

        Args:
            file_data: Raw file bytes
            file_name: Original file name
            original_text: User's message text (if any)

        Returns:
            Formatted text containing file content for Claude to analyze
        """
        import os
        import tempfile

        try:
            # Security: file size limit (10MB)
            MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
            if len(file_data) > MAX_FILE_SIZE:
                return f"{original_text}\n\n[文件过大: {file_name} ({len(file_data) // 1024 // 1024}MB)，最大允许 10MB]".strip()

            # Detect file type from extension
            ext = os.path.splitext(file_name)[1].lower()

            # Code file extensions
            code_extensions = {
                ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cpp", ".c", ".h",
                ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".r", ".jl",
                ".lua", ".pl", ".sh", ".bash", ".zsh", ".sql", ".html", ".css",
                ".scss", ".sass", ".less", ".vue", ".yaml", ".yml", ".json", ".xml",
                ".toml", ".ini", ".cfg", ".md", ".txt", ".rst",
            }

            # Archive extensions
            archive_extensions = {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z"}

            if ext in archive_extensions:
                # Handle archive files
                return await self._process_archive_content(file_data, file_name, original_text)

            elif ext in code_extensions or ext == "":
                # Handle code/text files
                try:
                    content = file_data.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        content = file_data.decode("latin-1")
                    except Exception:
                        content = f"[Binary file: {file_name}]"

                # Build prompt
                language_map = {
                    ".py": "python", ".js": "javascript", ".ts": "typescript",
                    ".java": "java", ".cpp": "cpp", ".c": "c", ".go": "go",
                    ".rs": "rust", ".rb": "ruby", ".php": "php", ".swift": "swift",
                    ".kt": "kotlin", ".scala": "scala", ".r": "r", ".jl": "julia",
                    ".sh": "bash", ".bash": "bash", ".sql": "sql", ".html": "html",
                    ".css": "css", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
                    ".xml": "xml", ".toml": "toml", ".md": "markdown",
                }
                lang = language_map.get(ext, "text")

                prompt = f"{original_text}\n\nFile: {file_name}\n\n```{lang}\n{content}\n```"
                return prompt.strip()

            else:
                # Unknown file type, just report it
                return f"{original_text}\n\n用户上传了一个文件: {file_name} ({len(file_data)} bytes)".strip()

        except Exception as e:
            logger.error("Error processing file content", error=str(e), exc_info=True)
            return f"{original_text}\n\n[处理文件时出错: {str(e)}]".strip()

    async def _process_archive_content(
        self,
        file_data: bytes,
        file_name: str,
        original_text: str,
    ) -> str:
        """Process archive file content and return formatted text.

        Args:
            file_data: Raw archive bytes
            file_name: Archive file name
            original_text: User's message text

        Returns:
            Formatted text with archive structure for Claude
        """
        import zipfile
        import tarfile
        import tempfile
        import os
        from io import BytesIO

        try:
            # Security: zip bomb detection
            MAX_ARCHIVE_RATIO = 100  # Max compression ratio
            MAX_EXTRACTED_FILES = 1000  # Max files in archive

            if len(file_data) > 0 and file_name.endswith(".zip"):
                try:
                    with zipfile.ZipFile(BytesIO(file_data)) as zf:
                        total_uncompressed = sum(
                            info.file_size for info in zf.filelist if not info.is_dir()
                        )
                        if total_uncompressed > len(file_data) * MAX_ARCHIVE_RATIO:
                            ratio = total_uncompressed // max(len(file_data), 1)
                            return f"{original_text}\n\n[⚠️ 压缩包可能为 zip 炸弹，已拒绝处理 (压缩比: {ratio}:1)]".strip()
                        if len(zf.filelist) > MAX_EXTRACTED_FILES:
                            return f"{original_text}\n\n[⚠️ 压缩包包含过多文件 ({len(zf.filelist)})，已拒绝处理]".strip()
                except Exception:
                    pass

            file_list = []

            # Try to extract file list
            if file_name.endswith(".zip"):
                try:
                    with zipfile.ZipFile(BytesIO(file_data)) as zf:
                        for info in zf.filelist:
                            if not info.is_dir():
                                file_list.append(info.filename)
                except Exception as e:
                    logger.warning("Failed to read zip file", error=str(e))

            elif file_name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
                try:
                    mode = "r:gz" if file_name.endswith((".tar.gz", ".tgz")) else \
                           "r:bz2" if file_name.endswith(".tar.bz2") else "r"
                    with tarfile.open(fileobj=BytesIO(file_data), mode=mode) as tf:
                        for member in tf.getmembers():
                            if member.isfile():
                                file_list.append(member.name)
                except Exception as e:
                    logger.warning("Failed to read tar file", error=str(e))

            # Build file tree
            if file_list:
                tree = "\n".join(f"  - {f}" for f in sorted(file_list)[:50])  # Limit to 50 files
                if len(file_list) > 50:
                    tree += f"\n  ... and {len(file_list) - 50} more files"
                return f"{original_text}\n\n用户上传了一个压缩包: {file_name}\n包含 {len(file_list)} 个文件:\n{tree}".strip()
            else:
                return f"{original_text}\n\n用户上传了一个压缩包: {file_name} ({len(file_data)} bytes)".strip()

        except Exception as e:
            logger.error("Error processing archive", error=str(e), exc_info=True)
            return f"{original_text}\n\n[处理压缩包时出错: {str(e)}]".strip()

    async def _process_image_content(
        self,
        image_data: bytes,
        original_text: str,
    ) -> str:
        """Process uploaded image content.

        Args:
            image_data: Raw image bytes
            original_text: User's message text

        Returns:
            Text with image info for Claude (image will be handled by Claude's vision)
        """
        import base64
        import tempfile
        import os

        try:
            # Save image to temp file for Claude to process
            temp_dir = tempfile.gettempdir()
            temp_path = os.path.join(temp_dir, f"lark_image_{uuid.uuid4().hex[:8]}.png")

            with open(temp_path, "wb") as f:
                f.write(image_data)

            # Build prompt with image reference
            # Note: Claude Code SDK can handle image files
            prompt = f"{original_text}\n\n[用户上传了一张图片，保存在: {temp_path}]"
            return prompt.strip()

        except Exception as e:
            logger.error("Error processing image", error=str(e), exc_info=True)
            return f"{original_text}\n\n[处理图片时出错: {str(e)}]".strip()

    def extract_user(self, event_data: Dict[str, Any]) -> PlatformUser:
        """Extract user from Lark event."""
        # Lark event structure
        sender = event_data.get("sender", {})

        return PlatformUser(
            user_id=sender.get("user_id", sender.get("open_id", "")),
            username=sender.get("nickname"),
            first_name=sender.get("name"),
            platform=PlatformType.LARK,
            raw={"sender": sender},
        )

    def extract_message(self, event_data: Dict[str, Any]) -> Optional[PlatformMessage]:
        """Extract message from Lark event."""
        # Check if this is a message event
        if event_data.get("type") != "im.message.receive_v1":
            return None

        message = event_data.get("message", {})
        sender = event_data.get("sender", {})

        # Parse message content
        content_str = message.get("content", "{}")
        try:
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
        except json.JSONDecodeError:
            content = {}

        # Determine message type
        msg_type = message.get("msg_type", "text")
        message_type = MessageType.TEXT

        if msg_type == "text":
            message_type = MessageType.TEXT
        elif msg_type == "image":
            message_type = MessageType.IMAGE
        elif msg_type == "file":
            message_type = MessageType.FILE
        elif msg_type == "audio":
            message_type = MessageType.VOICE

        user = self.extract_user(event_data)

        return PlatformMessage(
            message_id=message.get("message_id", ""),
            user=user,
            chat_id=message.get("chat_id", ""),
            content=content.get("text", "") if msg_type == "text" else "",
            message_type=message_type,
            platform=PlatformType.LARK,
            raw={"event": event_data},
        )

    def get_chat_id(self, event_data: Dict[str, Any]) -> str:
        """Extract chat ID from Lark event."""
        # In Lark, chat_id is in the message
        if event_data.get("type") == "im.message.receive_v1":
            message = event_data.get("message", {})
            return message.get("chat_id", "")
        return ""

    async def send_action(
        self,
        chat_id: str,
        action: str = "typing",
        **kwargs: Any,
    ) -> None:
        """Send chat action - Lark doesn't support typing indicators."""
        # Lark doesn't have typing indicators like Telegram
        # We can send a temporary "processing..." message if needed
        pass

    def _build_lark_card(self, card: PlatformCard) -> Dict[str, Any]:
        """Build Lark card format from PlatformCard."""
        # Lark card structure
        lark_card = {
            "config": {
                "wide_screen_mode": True
            },
            "elements": []
        }

        # Add header if title exists
        if card.title:
            lark_card["header"] = {
                "title": {
                    "content": card.title,
                    "tag": "plain_text"
                }
            }

        # Add content if exists
        if card.content:
            lark_card["elements"].append({
                "tag": "div",
                "text": {
                    "content": card.content,
                    "tag": "lark_md"
                }
            })

        # Add interactive elements
        if card.elements:
            action_group = {
                "tag": "action",
                "actions": []
            }

            for element in card.elements:
                button = self._build_lark_button(element)
                if button:
                    action_group["actions"].append(button)

            if action_group["actions"]:
                lark_card["elements"].append(action_group)

        return lark_card

    def _build_lark_button(self, element: PlatformCardElement) -> Optional[Dict[str, Any]]:
        """Build Lark button from card element."""
        button = {
            "tag": "button",
            "text": {
                "content": element.text,
                "tag": "plain_text"
            }
        }

        # Set button type/style
        if element.style == "primary":
            button["type"] = "primary"
        elif element.style == "danger":
            button["type"] = "danger"
        else:
            button["type"] = "default"

        # Set button action
        if element.action:
            if element.action.startswith("http"):
                # URL button
                button["url"] = element.action
            else:
                # Callback button
                button["type"] = element.style or "default"
                button["value"] = element.value or element.action

        return button

    async def _execute_async(self, func, *args, **kwargs):
        """Execute Lark API call asynchronously."""
        # Lark SDK is synchronous, so we need to run in thread pool
        loop = kwargs.pop('_loop', None)
        if loop is None:
            loop = asyncio.get_event_loop()

        return await loop.run_in_executor(None, func, *args, **kwargs)

    @property
    def platform_name(self) -> str:
        """Get platform name."""
        return "lark"

    def get_client(self) -> Optional[Client]:
        """Get the underlying Lark client."""
        return self.client

    def _get_user_data(self, user_id: int) -> Dict[str, Any]:
        """Get or create per-user state dict (mirrors Telegram's context.user_data)."""
        if user_id not in self._user_data:
            self._user_data[user_id] = {
                "current_directory": None,
                "claude_session_id": None,
                "session_started": False,
                "force_new_session": False,
            }
        return self._user_data[user_id]

    def _get_working_directory(self, user_id: int) -> str:
        """Get current working directory for a user."""
        user_data = self._get_user_data(user_id)
        return user_data.get("current_directory") or self.settings.approved_directory

    def _set_working_directory(self, user_id: int, path: str) -> None:
        """Set current working directory for a user."""
        self._get_user_data(user_id)["current_directory"] = path

    def _get_session_id(self, user_id: int) -> Optional[str]:
        """Get Claude session ID for a user."""
        return self._get_user_data(user_id).get("claude_session_id")

    def _set_session_id(self, user_id: int, session_id: Optional[str]) -> None:
        """Set Claude session ID for a user."""
        self._get_user_data(user_id)["claude_session_id"] = session_id

    def is_adapter_running(self) -> bool:
        """Check if adapter is running."""
        return self._is_running
