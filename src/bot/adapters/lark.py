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
        self._event_handlers: List[Callable] = []
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
            content_raw = message.get("content", "{}")

            # Parse content
            try:
                content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
                text = content.get("text", "")
            except json.JSONDecodeError:
                text = ""

            if not text:
                return

            # Log the received event
            logger.info(
                "Received Lark message",
                chat_id=chat_id,
                text=text[:50],
                sender_open_id=sender.get("open_id", ""),
            )

            # Dispatch to core engine via main loop
            if self._main_loop and not self._main_loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._process_with_core_engine(chat_id, text, sender),
                    self._main_loop
                )

        except Exception as e:
            logger.error("Error processing Lark message event", error=str(e), exc_info=True)

    async def _process_with_core_engine(
        self,
        chat_id: str,
        text: str,
        sender: Dict[str, Any],
    ) -> None:
        """Process message using CoreEngine with streaming card."""
        if not self.core_engine or not self.settings:
            logger.error("CoreEngine or Settings not configured")
            await self.send_message(chat_id, "系统未正确配置")
            return

        from src.bot.core_engine import MessageContext, StreamEvent
        import time

        open_id = sender.get("open_id", "")
        user_id = hash(open_id) % 1000000

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

        # Step 1: Create streaming card
        card_id, message_id = await self._create_streaming_card(chat_id)
        if not card_id:
            # Fallback to simple text
            await self._process_with_fallback(chat_id, ctx, user_id, start_time, interrupt_event)
            return

        # Track content and update state
        full_content = [""]
        update_sequence = [0]
        last_update_time = [0.0]

        # Heartbeat for real-time progress updates (every 1s)
        async def update_heartbeat():
            try:
                while not interrupt_event.is_set():
                    await asyncio.sleep(1.0)
                    if interrupt_event.is_set():
                        break
                    elapsed = time.time() - start_time
                    content = f"Working... ({elapsed:.0f}s)\n\n{full_content[0]}"
                    await self._update_card_content(card_id, content, update_sequence[0])
                    update_sequence[0] += 1
            except asyncio.CancelledError:
                pass

        heartbeat_task = asyncio.create_task(update_heartbeat())

        # Stream callback
        async def on_stream(event: StreamEvent) -> None:
            if interrupt_event.is_set():
                return

            if event.type == "progress" and event.tool_name:
                full_content[0] += f"[{event.tool_name}]\n"
            elif event.type == "response" and event.content:
                content = event.content
                if not (content.startswith("[ThinkingBlock") or content.startswith("[ContentBlock")):
                    full_content[0] += content
            elif event.type == "error":
                full_content[0] += f"\n[Error] {event.content}\n"

        self._stop_callbacks[user_id] = interrupt_event

        try:
            response = await self.core_engine.process_message(
                ctx, on_stream, interrupt_event=interrupt_event
            )

            # Signal heartbeat to stop and wait for it
            interrupt_event.set()
            try:
                await asyncio.wait_for(heartbeat_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

            elapsed = time.time() - start_time
            status = "Done" if response.success else ("Interrupted" if response.interrupted else "Error")

            # Always use response.content as final text (it's complete)
            final_text = response.content if response.content else full_content[0]
            if response.interrupted:
                final_text += "\n\n_(Interrupted)_"

            logger.info("Final response", content_len=len(final_text), response_len=len(response.content or ""))

            final_content = f"[{status}] {elapsed:.1f}s\n\n{final_text}"
            await self._update_card_content(card_id, final_content, update_sequence[0])
            await self._close_streaming_mode(card_id, update_sequence[0] + 1)

        except asyncio.CancelledError:
            heartbeat_task.cancel()
            elapsed = time.time() - start_time
            await self._update_card_content(card_id, f"[Cancelled] {elapsed:.1f}s\n\n请求已取消", update_sequence[0])
            await self._close_streaming_mode(card_id, update_sequence[0] + 1)

        except Exception as e:
            heartbeat_task.cancel()
            logger.error("Error", error=str(e), exc_info=True)
            elapsed = time.time() - start_time
            await self._update_card_content(card_id, f"[Error] {elapsed:.1f}s\n\n{str(e)}", update_sequence[0])
            await self._close_streaming_mode(card_id, update_sequence[0] + 1)

        finally:
            self._stop_callbacks.pop(user_id, None)

    async def _create_streaming_card(self, chat_id: str) -> tuple[Optional[str], Optional[str]]:
        """Create streaming card and send as message."""
        try:
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
                    }
                },
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": "Working...", "element_id": "content_element"}
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
                logger.warning("Card update failed", code=response.code)
                return False
            return True

        except Exception as e:
            logger.error("Error updating card", error=str(e))
            return False

    async def _close_streaming_mode(self, card_id: str, sequence: int) -> bool:
        """Close streaming mode."""
        try:
            settings_request = SettingsCardRequest.builder() \
                .card_id(card_id) \
                .request_body(
                    SettingsCardRequestBody.builder()
                        .settings(json.dumps({"config": {"streaming_mode": False}}))
                        .uuid(str(uuid.uuid4()))
                        .sequence(sequence)
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

        # Build event handler
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_message_received) \
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

    def _on_card_action(self, data: Any) -> None:
        """Handle card button callbacks."""
        try:
            logger.info("Card action received", data_type=type(data).__name__)

            # Extract action value
            action_value = ""
            if hasattr(data, 'event') and hasattr(data.event, 'action'):
                action = data.event.action
                if hasattr(action, 'value'):
                    action_value = action.value

            logger.info("Card action value", value=action_value)

            # Handle stop action
            if action_value.startswith("stop:"):
                user_id_str = action_value.split(":")[1]
                try:
                    user_id = int(user_id_str)
                    if user_id in self._stop_callbacks:
                        interrupt_event = self._stop_callbacks[user_id]
                        interrupt_event.set()
                        logger.info("Stop requested", user_id=user_id)
                except ValueError:
                    logger.warning("Invalid user_id in stop action", value=action_value)

        except Exception as e:
            logger.error("Error processing card action", error=str(e), exc_info=True)

    async def handle_card_callback(self, payload: Dict[str, Any]) -> None:
        """Handle card action callback from webhook.

        Args:
            payload: Card action payload from Lark webhook
        """
        logger.info("Handling card callback", payload=payload)

        try:
            # Extract action value from Lark card callback format
            action = payload.get("action", {})
            action_value = action.get("value", "")

            logger.info("Card callback action value", value=action_value)

            # Handle stop action
            if action_value.startswith("stop:"):
                user_id_str = action_value.split(":")[1]
                try:
                    user_id = int(user_id_str)
                    if user_id in self._stop_callbacks:
                        interrupt_event = self._stop_callbacks[user_id]
                        interrupt_event.set()
                        logger.info("Stop requested via card callback", user_id=user_id)
                    else:
                        logger.warning("No active request for user", user_id=user_id)
                except ValueError:
                    logger.warning("Invalid user_id in stop action", value=action_value)

        except Exception as e:
            logger.error("Error handling card callback", error=str(e), exc_info=True)

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
        self._event_handlers.append(handler)
        logger.info("Registered message handler for Lark WebSocket")

    async def register_command_handler(
        self,
        commands: List[str],
        handler: Callable,
        **kwargs: Any,
    ) -> None:
        """Register command handler - commands are handled as messages in Lark."""
        # In Lark, commands are just messages starting with /
        # They will be processed by the message handler
        self._event_handlers.append(handler)
        logger.info("Registered command handler for Lark", commands=commands)

    async def register_callback_handler(
        self,
        handler: Callable,
        **kwargs: Any,
    ) -> None:
        """Register callback handler for card button interactions."""
        self._event_handlers.append(handler)
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
        """Send file to Lark chat."""
        try:
            # For Lark, files need to be uploaded first
            # This is a simplified version - full implementation would need
            # to handle file upload API calls

            if isinstance(file, PlatformFile):
                file_data = file.file_data
                filename = filename or file.file_name
            elif isinstance(file, bytes):
                file_data = file
            else:
                # File path
                with open(file, 'rb') as f:
                    file_data = f.read()

            # Upload file to Lark
            # Note: This requires using the upload API
            # For now, return a simplified response
            logger.warning("File upload not fully implemented for Lark yet")
            return PlatformResponse(
                success=False,
                error="File upload not fully implemented for Lark yet",
            )
        except Exception as e:
            logger.error("Failed to send file", error=str(e))
            return PlatformResponse(success=False, error=str(e))

    async def download_file(self, file_id: str, **kwargs: Any) -> Optional[bytes]:
        """Download file from Lark."""
        try:
            # Lark file download requires getting file URL then downloading
            # This is a placeholder for full implementation
            logger.warning("File download not fully implemented for Lark yet")
            return None
        except Exception as e:
            logger.error("Failed to download file", error=str(e))
            return None

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
