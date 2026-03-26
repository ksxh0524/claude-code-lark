"""Lark/Feishu platform adapter implementation using WebSocket long polling."""

import asyncio
import json
import threading
import uuid
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
        self._command_handlers: List[Callable] = []  # Command handlers
        self._message_handlers: List[Callable] = []  # Message handlers
        self._callback_handlers: List[Callable] = []  # Callback handlers
        self._registered_commands: List[str] = []  # List of registered commands
        self._ws_thread: Optional[threading.Thread] = None
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self.core_engine: Optional[Any] = None  # CoreEngine instance
        self.settings: Optional[Any] = None  # Settings instance
        self._stop_callbacks: Dict[int, asyncio.Event] = {}  # user_id -> interrupt event

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
            else:
                # Unknown message type, skip
                logger.info("Skipping unsupported message type", msg_type=msg_type)
                return

            if not text and not file_info:
                return

            # Log the received event
            logger.info(
                "Received Lark message",
                chat_id=chat_id,
                msg_type=msg_type,
                text=text[:50] if text else "",
                sender_open_id=sender.get("open_id", ""),
            )

            # Dispatch to appropriate handler via main loop
            if self._main_loop and not self._main_loop.is_closed():
                # Check if message is a command (starts with /)
                is_command = text.strip().startswith("/")

                if is_command and self._command_handlers:
                    # Route to command handlers
                    logger.info("Routing to command handler", text=text[:30])
                    asyncio.run_coroutine_threadsafe(
                        self._dispatch_to_handlers(event_data, self._command_handlers),
                        self._main_loop
                    )
                elif self._message_handlers:
                    # Route to message handlers
                    logger.info("Routing to message handler", text=text[:30])
                    asyncio.run_coroutine_threadsafe(
                        self._dispatch_to_handlers(event_data, self._message_handlers),
                        self._main_loop
                    )
                else:
                    # Fallback: use core engine directly
                    logger.warning("No handlers registered, using core engine fallback")
                    asyncio.run_coroutine_threadsafe(
                        self._process_with_core_engine(chat_id, text, sender, file_info),
                        self._main_loop
                    )

        except Exception as e:
            logger.error("Error processing Lark message event", error=str(e), exc_info=True)

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
                    # Download and process image
                    image_key = file_info.get("image_key", "")

                    # Send processing message
                    await self.send_message(chat_id, "正在处理图片...")

                    image_data = await self.download_image(image_key)
                    if image_data:
                        # Process image
                        text = await self._process_image_content(image_data, text)
                    else:
                        await self.send_message(chat_id, "无法下载图片")
                        return

            except Exception as e:
                logger.error("Error processing file/image", error=str(e), exc_info=True)
                await self.send_message(chat_id, f"处理文件时出错: {str(e)}")
                return

        ctx = MessageContext(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            working_directory=self.settings.approved_directory,
            username=sender.get("user_id", ""),
            is_private=True,
            platform="lark",
        )

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
        update_sequence = [0]
        last_update_time = [0.0]  # Track last update time for throttling

        # Wait briefly for card to be ready before first update
        await asyncio.sleep(0.2)

        # Stream callback - accumulates content with throttled updates
        async def on_stream(event: StreamEvent) -> None:
            if interrupt_event.is_set():
                return

            content_changed = False
            if event.type == "progress" and event.tool_name:
                full_content[0] += f"[{event.tool_name}]\n"
                content_changed = True
            elif event.type == "response" and event.content:
                content = event.content
                if not (content.startswith("[ThinkingBlock") or content.startswith("[ContentBlock")):
                    full_content[0] += content
                    content_changed = True
            elif event.type == "error":
                full_content[0] += f"\n[Error] {event.content}\n"
                content_changed = True

            # Throttle updates: at most every 150ms (like OpenClaw's CARDKIT_MS)
            if content_changed:
                now = time.time()
                if now - last_update_time[0] >= 0.15:
                    await self._update_card_content(card_id, full_content[0] or "Thinking...", update_sequence[0])
                    update_sequence[0] += 1
                    last_update_time[0] = now

        self._stop_callbacks[user_id] = interrupt_event

        try:
            response = await self.core_engine.process_message(
                ctx, on_stream, interrupt_event=interrupt_event
            )

            elapsed = time.time() - start_time
            status = "Done" if response.success else ("Interrupted" if response.interrupted else "Error")

            # Always use response.content as final text (it's complete)
            final_text = response.content if response.content else full_content[0]
            if response.interrupted:
                final_text += "\n\n_(Interrupted)_"

            logger.info("Final response", content_len=len(final_text), response_len=len(response.content or ""))

            # Final update with status and elapsed time (only shown at end)
            final_content = f"**{status}** · {elapsed:.1f}s\n\n{final_text}"
            await self._update_card_content(card_id, final_content, update_sequence[0])
            await self._close_streaming_mode(card_id, update_sequence[0] + 1)

        except asyncio.CancelledError:
            elapsed = time.time() - start_time
            await self._update_card_content(card_id, f"**Cancelled** · {elapsed:.1f}s\n\n请求已取消", update_sequence[0])
            await self._close_streaming_mode(card_id, update_sequence[0] + 1)

        except Exception as e:
            logger.error("Error", error=str(e), exc_info=True)
            elapsed = time.time() - start_time
            await self._update_card_content(card_id, f"**Error** · {elapsed:.1f}s\n\n{str(e)}", update_sequence[0])
            await self._close_streaming_mode(card_id, update_sequence[0] + 1)

        finally:
            self._stop_callbacks.pop(user_id, None)

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
            card_json = {
                "schema": "2.0",
                "header": {
                    "title": {"content": "Claude Code", "tag": "plain_text"},
                    "subtitle": {"content": "Processing...", "tag": "plain_text"},
                    "template": "blue"
                },
                "config": {
                    "streaming_mode": True,
                    "streaming_config": {
                        "print_frequency_ms": {"default": 50},
                        "print_step": {"default": 2},
                        "print_strategy": "fast"
                    },
                    # Summary shown in notification/feed during processing
                    "summary": {
                        "content": "Processing...",
                        "i18n_content": {"zh_cn": "处理中...", "en_us": "Processing..."}
                    }
                },
                "body": {
                    "elements": [
                        # Main content element for streaming updates
                        {
                            "tag": "markdown",
                            "content": "Thinking...",
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
                                    "value": {"action": "stop", "user_id": user_id}
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

    async def _close_streaming_mode(self, card_id: str, sequence: int) -> bool:
        """Close streaming mode and remove the stop button and loading icon."""
        try:
            # First, delete the loading icon element
            await self._delete_card_element(card_id, "loading_icon", sequence)

            # Then, delete the stop button element
            await self._delete_card_element(card_id, "stop_button", sequence + 1)

            # Finally close streaming mode (sequence + 2 for next operation)
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
                if not (content.startswith("[ThinkingBlock") or content.startswith("[ContentBlock")):
                    full_content += content

        try:
            response = await self.core_engine.process_message(
                ctx, on_stream, interrupt_event=interrupt_event
            )

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
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        try:
            logger.info("Card action received", data_type=type(data).__name__)

            # Extract action value
            action_value = None
            if hasattr(data, 'event') and hasattr(data.event, 'action'):
                action = data.event.action
                if hasattr(action, 'value'):
                    action_value = action.value

            logger.info("Card action value", value=action_value)

            # Parse action - support both string and dict formats
            action_type = None
            user_id = None

            if isinstance(action_value, dict):
                # New schema 2.0 format: {"action": "stop", "user_id": 123}
                action_type = action_value.get("action")
                user_id = action_value.get("user_id")
            elif isinstance(action_value, str) and action_value.startswith("stop:"):
                # Legacy string format: "stop:123"
                action_type = "stop"
                try:
                    user_id = int(action_value.split(":")[1])
                except ValueError:
                    pass

            # Handle stop action
            if action_type == "stop" and user_id is not None:
                if user_id in self._stop_callbacks:
                    interrupt_event = self._stop_callbacks[user_id]
                    interrupt_event.set()
                    logger.info("Stop requested", user_id=user_id)
                    # Return success response with toast
                    return P2CardActionTriggerResponse({
                        "toast": {
                            "type": "info",
                            "content": "正在中断请求..."
                        }
                    })
                else:
                    logger.warning("No stop callback for user", user_id=user_id)
                    return P2CardActionTriggerResponse({
                        "toast": {
                            "type": "warning",
                            "content": "没有正在进行的请求"
                        }
                    })

            # Default success response
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "info",
                    "content": "操作已收到"
                }
            })

        except Exception as e:
            logger.error("Error processing card action", error=str(e), exc_info=True)
            # Return error response
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "error",
                    "content": f"处理失败: {str(e)}"
                }
            })

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

            # Parse action type and parameters - support both string and dict formats
            if isinstance(action_value, dict):
                # New schema 2.0 format: {"action": "stop", "user_id": 123}
                action_type = action_value.get("action", "")
                action_param = action_value.get("user_id", "") or action_value.get("param", "")
            elif ":" in action_value:
                # Legacy string format: "stop:123"
                action_type, action_param = action_value.split(":", 1)
            else:
                action_type = action_value
                action_param = ""

            # --- Stop action (interrupt active request) ---
            if action_type == "stop":
                try:
                    user_id = int(action_param) if action_param else 0
                    if user_id in self._stop_callbacks:
                        interrupt_event = self._stop_callbacks[user_id]
                        interrupt_event.set()
                        logger.info("Stop requested via card callback", user_id=user_id)
                        await self.send_message(open_chat_id, "⏹️ 已中断请求")
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
        """Send text message to Lark chat."""
        try:
            # Build message request
            content = {
                "text": text
            }

            message_type = "text"

            # Add reply if specified
            if reply_to_message_id:
                content["reply_in_thread"] = True

            # Use chat_id as receive_id_type since we receive chat_id from events
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type(message_type)
                        .content(json.dumps(content))
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

    def is_adapter_running(self) -> bool:
        """Check if adapter is running."""
        return self._is_running
