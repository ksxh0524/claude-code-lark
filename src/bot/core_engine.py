"""Core engine for Claude Code Bot - platform agnostic message processing.

This module provides a platform-independent layer for processing messages
through Claude. Platform adapters (Telegram, Lark) use this engine to:
1. Parse platform-specific events into MessageContext
2. Call process_message() to get Claude's response
3. Send the response back through the platform adapter
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger()


@dataclass
class MessageContext:
    """Platform-agnostic message context.

    All platform adapters must create this context from their events.
    """
    user_id: int  # Unique user identifier (hashed if string)
    chat_id: str  # Chat/conversation identifier
    text: str  # Message text content
    working_directory: str  # Directory for Claude to work in
    session_id: Optional[str] = None  # Claude session ID for continuation
    username: Optional[str] = None  # Display name
    is_private: bool = True  # Private chat vs group
    message_id: Optional[str] = None  # Original message ID for reply
    platform: str = "unknown"  # Platform name for logging


@dataclass
class StreamEvent:
    """Stream event from Claude execution.

    Platform adapters receive these events via on_stream callback
    to provide real-time feedback to users.
    """
    type: str  # "progress", "response", "error", "done"
    content: str = ""
    tool_name: Optional[str] = None
    tool_input: Optional[str] = None
    reasoning: Optional[str] = None


@dataclass
class ClaudeResponse:
    """Final response from Claude.

    Contains the complete response content and metadata.
    """
    content: str
    session_id: str
    success: bool = True
    error: Optional[str] = None
    interrupted: bool = False
    duration_ms: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


class CoreEngine:
    """
    Platform-agnostic core engine for Claude Code Bot.

    This engine handles all Claude interaction logic independent of
    the messaging platform (Telegram, Lark, etc.).

    Usage:
        engine = CoreEngine(claude_integration, settings, deps)

        # Platform adapter creates context
        ctx = MessageContext(
            user_id=12345,
            chat_id="oc_xxx",
            text="Write a hello world",
            working_directory="/home/user/projects",
        )

        # Process message
        response = await engine.process_message(ctx)

        # Or with streaming
        async def on_stream(event: StreamEvent):
            await adapter.update_progress(event.content)

        response = await engine.process_message(ctx, on_stream=on_stream)
    """

    def __init__(
        self,
        claude_integration,
        settings,
        deps: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize CoreEngine.

        Args:
            claude_integration: ClaudeIntegration instance
            settings: Application settings
            deps: Optional dependencies dict (rate_limiter, storage, etc.)
        """
        self.claude = claude_integration
        self.settings = settings
        self.deps = deps or {}
        self._active_requests: Dict[int, asyncio.Event] = {}

    async def process_message(
        self,
        ctx: MessageContext,
        on_stream: Optional[Callable[[StreamEvent], None]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> ClaudeResponse:
        """
        Process a message and return Claude's response.

        This is the main entry point for all platforms.

        Args:
            ctx: Message context with user_id, chat_id, text, etc.
            on_stream: Optional async callback for streaming events
            interrupt_event: Optional event to signal request interruption

        Returns:
            ClaudeResponse with content, session_id, and status
        """
        logger.info(
            "Processing message",
            user_id=ctx.user_id,
            chat_id=ctx.chat_id,
            text_len=len(ctx.text),
            platform=ctx.platform,
        )

        start_time = time.time()

        # Rate limit check
        rate_limiter = self.deps.get("rate_limiter")
        if rate_limiter:
            allowed, message = await rate_limiter.check_rate_limit(ctx.user_id, 0.001)
            if not allowed:
                return ClaudeResponse(
                    content=f"⏱️ {message}",
                    session_id=ctx.session_id or "",
                    success=False,
                    error=message,
                )

        # Validate working directory
        working_dir = Path(ctx.working_directory)
        if not working_dir.is_absolute():
            working_dir = Path(self.settings.approved_directory) / working_dir

        # Security check
        security = self.deps.get("security")
        if security:
            is_valid, resolved_path, error = security.validate_path(str(working_dir))
            if not is_valid:
                return ClaudeResponse(
                    content=f"❌ {error}",
                    session_id=ctx.session_id or "",
                    success=False,
                    error=error,
                )

        # Use provided interrupt_event or create a new one
        if interrupt_event is None:
            interrupt_event = asyncio.Event()
        self._active_requests[ctx.user_id] = interrupt_event

        tool_log: List[Dict[str, Any]] = []

        try:
            # Build stream callback wrapper
            stream_callback = self._make_stream_callback(
                on_stream, tool_log, ctx.verbose_level if hasattr(ctx, 'verbose_level') else 1
            ) if on_stream else None

            # Call Claude
            claude_response = await self.claude.run_command(
                prompt=ctx.text,
                working_directory=str(working_dir),
                user_id=ctx.user_id,
                session_id=ctx.session_id,
                on_stream=stream_callback,
                force_new=False,
                interrupt_event=interrupt_event,
            )

            duration_ms = int((time.time() - start_time) * 1000)

            # Log interaction
            storage = self.deps.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=ctx.user_id,
                        session_id=claude_response.session_id,
                        prompt=ctx.text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Notify stream done
            if on_stream:
                await on_stream(StreamEvent(type="done"))

            return ClaudeResponse(
                content=claude_response.content or "",
                session_id=claude_response.session_id or "",
                success=not claude_response.is_error,
                error=claude_response.error_type if claude_response.is_error else None,
                interrupted=claude_response.interrupted,
                duration_ms=duration_ms,
                tool_calls=tool_log,
            )

        except asyncio.CancelledError:
            logger.info("Request cancelled", user_id=ctx.user_id)
            return ClaudeResponse(
                content="⏹️ 请求已取消",
                session_id=ctx.session_id or "",
                success=False,
                error="Cancelled",
                interrupted=True,
            )

        except Exception as e:
            logger.error("Claude execution error", error=str(e), exc_info=True)
            return ClaudeResponse(
                content=f"❌ 执行出错: {str(e)}",
                session_id=ctx.session_id or "",
                success=False,
                error=str(e),
            )

        finally:
            self._active_requests.pop(ctx.user_id, None)

    def _make_stream_callback(
        self,
        on_stream: Callable[[StreamEvent], None],
        tool_log: List[Dict[str, Any]],
        verbose_level: int = 1,
    ) -> Callable:
        """Create stream callback that wraps platform callback."""
        async def callback(event) -> None:
            # Handle both dict and StreamUpdate object
            if hasattr(event, 'type'):
                # StreamUpdate object
                event_type = event.type
                content = getattr(event, 'content', None) or ""
                tool_calls = getattr(event, 'tool_calls', None) or []
            else:
                # Dict
                event_type = event.get("type", "")
                content = event.get("content", "")
                tool_calls = event.get("tool_calls", [])

            if event_type == "tool_use" or (tool_calls and len(tool_calls) > 0):
                for tc in tool_calls:
                    tool_name = tc.get("name", tc.get("tool_name", "unknown"))
                    tool_input = tc.get("input", tc.get("tool_input", {}))
                    tool_log.append({"tool": tool_name, "input": tool_input})

                    if verbose_level >= 1:
                        tool_input_str = str(tool_input)[:100] if tool_input else ""
                        await on_stream(StreamEvent(
                            type="progress",
                            content=f"[{tool_name}]",
                            tool_name=tool_name,
                            tool_input=tool_input_str,
                        ))

            elif event_type in ("thinking", "reasoning"):
                if verbose_level >= 2:
                    reasoning = content[:200] if content else ""
                    await on_stream(StreamEvent(
                        type="progress",
                        content="Thinking...",
                        reasoning=reasoning,
                    ))

            elif event_type in ("stream_delta", "assistant"):
                if content:
                    await on_stream(StreamEvent(
                        type="response",
                        content=content,  # Full content for streaming cards
                    ))

            elif event_type == "error":
                await on_stream(StreamEvent(
                    type="error",
                    content=content or "Unknown error",
                ))

        return callback

    def interrupt(self, user_id: int) -> bool:
        """Interrupt an active request for a user."""
        if user_id in self._active_requests:
            self._active_requests[user_id].set()
            logger.info("Request interrupted", user_id=user_id)
            return True
        return False

    def is_processing(self, user_id: int) -> bool:
        """Check if a user has an active request."""
        return user_id in self._active_requests
