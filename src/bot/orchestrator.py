"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

logger = structlog.get_logger()

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


@dataclass
class ActiveRequest:
    """Tracks an in-flight Claude request so it can be interrupted."""

    user_id: int
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._active_requests: Dict[int, ActiveRequest] = {}
        self._known_commands: frozenset[str] = frozenset()

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands - include both agentic and utility commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("restart", command.restart_command),
            # Utility commands for file/directory operations
            ("ls", self._cmd_ls_handler),
            ("cd", self._cmd_cd_handler),
            ("pwd", self._cmd_pwd_handler),
            ("projects", self._cmd_projects_handler),
            ("export", self._cmd_export_handler),
            ("actions", self._cmd_actions_handler),
            ("git", self._cmd_git_handler),
            ("list", self._cmd_list_handler),
            ("help", self._cmd_help_handler),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        # Derive known commands dynamically — avoids drift when new commands are added
        self._known_commands: frozenset[str] = frozenset(cmd for cmd, _ in handlers)

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Claude (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0
        # (higher priority). This catches any /command not matched there
        # and forwards it to Claude, while skipping known commands to
        # avoid double-firing.
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Stop button callback (must be before cd: handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("list", "Show all available commands"),
                BotCommand("help", "Show help"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = str(current_dir)

        session_id = context.user_data.get("claude_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        await update.message.reply_text(
            f"📂 {dir_display} · Session: {session_status}{cost_str}"
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = mcp_images is not None and approved_directory is not None

        if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return

            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        )
                    except Exception:
                        pass

        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)

        # Create Stop button and interrupt event
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )
        progress_msg = await update.message.reply_text(
            "Working...", reply_markup=stop_kb
        )

        # Register active request for stop callback
        active_request = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )
        self._active_requests[user_id] = active_request

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            self._active_requests.pop(user_id, None)
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration.",
                reply_markup=None,
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            reply_markup=stop_kb,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            interrupt_event=interrupt_event,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        success = True
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)

            response_content = claude_response.content
            if claude_response.interrupted:
                response_content = (
                    response_content or ""
                ) + "\n\n_(Interrupted by user)_"

            formatted_messages = formatter.format_claude_response(response_content)

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_doc: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_doc,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
            )

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_doc

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Transcribing...")

        try:
            voice = update.message.voice
            processed_voice = await voice_handler.process_voice_message(
                voice, update.message.caption
            )

            await progress_msg.edit_text("Working...")
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
    ) -> None:
        """Run a media-derived prompt through Claude and send responses."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
            )
        finally:
            heartbeat.cancel()

        if force_new:
            context.user_data["force_new_session"] = False

        context.user_data["claude_session_id"] = claude_response.session_id

        from .handlers.message import _update_working_directory_from_claude_response

        _update_working_directory_from_claude_response(
            claude_response, context, self.settings, user_id
        )

        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        formatted_messages = formatter.format_claude_response(claude_response.content)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls).
        images: List[ImageAttachment] = mcp_images_media

        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Claude in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but
        returns immediately when the command is registered, preventing
        double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Claude as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = context.bot_data.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request."""
        query = update.callback_query
        target_user_id = int(query.data.split(":", 1)[1])

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer(
                "Only the requesting user can stop this.", show_alert=True
            )
            return

        active = self._active_requests.get(target_user_id)
        if not active:
            await query.answer("Already completed.", show_alert=False)
            return
        if active.interrupted:
            await query.answer("Already stopping...", show_alert=False)
            return

        active.interrupt_event.set()
        active.interrupted = True
        await query.answer("Stopping...", show_alert=False)

        try:
            await active.progress_msg.edit_text("Stopping...", reply_markup=None)
        except Exception:
            pass

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )

    # --- Lark platform handlers ---

    async def handle_message(self, event_data: Dict[str, Any]) -> None:
        """Handle incoming message from Lark platform.

        Uses adapter's CoreEngine-based processing with streaming cards and Stop button.
        """
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

        user_id = sender.get("open_id", "unknown")
        logger.info("Lark message", chat_id=chat_id, user_id=user_id, text=text[:50])

        # Skip if command (handled by handle_command)
        if text.startswith("/"):
            await self.handle_command(event_data)
            return

        # Get adapter
        adapter = self.deps.get("adapter")
        if not adapter:
            logger.error("No adapter available")
            return

        # Check if adapter has _process_with_core_engine (Lark adapter with streaming card support)
        if hasattr(adapter, "_process_with_core_engine") and callable(adapter._process_with_core_engine):
            # Use the adapter's streaming card implementation with Stop button support
            await adapter._process_with_core_engine(chat_id, text, sender)
        else:
            # Fallback for other adapters - simple message processing
            claude_integration = self.deps.get("claude_integration")
            if not claude_integration:
                logger.error("Missing claude_integration")
                await adapter.send_message(chat_id, "❌ Claude 未配置")
                return

            await adapter.send_message(chat_id, "⏳ 正在处理...")

            try:
                response = await claude_integration.run_command(
                    prompt=text,
                    working_directory=Path(self.settings.approved_directory),
                    user_id=hash(user_id) % 1000000,
                )
                if response.content:
                    await adapter.send_message(chat_id, response.content)
            except Exception as e:
                logger.error("Claude execution error", error=str(e))
                await adapter.send_message(chat_id, f"❌ 执行出错: {str(e)}")

    async def handle_command(self, event_data: Dict[str, Any]) -> None:
        """Handle command from Lark platform.

        Supports both Agentic and Classic mode commands:
        - Agentic: /start, /new, /status, /verbose, /repo, /restart, /sync_threads
        - Classic: /help, /continue, /end, /ls, /cd, /pwd, /projects, /export, /actions, /git
        """
        import structlog
        logger = structlog.get_logger()

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

        logger.info(
            "Lark command received",
            chat_id=chat_id,
            text=text[:50] if text else "",
            sender_open_id=sender.get("open_id", ""),
        )

        if not text:
            return

        # Get adapter to send reply
        adapter = self.deps.get("adapter")
        if not adapter:
            logger.error("No adapter available for Lark reply")
            return

        # Parse command and args
        parts = text.strip().lstrip("/").split(maxsplit=2)
        command = parts[0].lower() if parts else ""
        args = parts[1:] if len(parts) > 1 else []

        # Get dependencies
        settings = self.deps.get("settings")
        storage = self.deps.get("storage")
        claude_integration = self.deps.get("claude_integration")
        audit_logger = self.deps.get("audit_logger")

        # Get user info
        open_id = sender.get("open_id", "")
        user_id = hash(open_id) % 1000000

        # Known commands - if not in this list, pass to message handler (Claude)
        known_commands = {
            "start", "help", "new", "status", "verbose", "repo", "restart",
            "sync_threads", "continue", "end", "ls", "cd", "pwd", "projects",
            "export", "actions", "git", "list",
        }

        if command not in known_commands:
            # Unknown command, pass to message handler (treat as Claude prompt)
            await self.handle_message(event_data)
            return

        # --- Agentic Mode Commands ---

        if command == "start":
            await self._cmd_start(adapter, chat_id, sender, settings)

        elif command == "new":
            await self._cmd_new(adapter, chat_id, user_id, storage, audit_logger)

        elif command == "status":
            await self._cmd_status(adapter, chat_id, user_id, storage, claude_integration)

        elif command == "verbose":
            level = int(args[0]) if args and args[0].isdigit() else None
            await self._cmd_verbose(adapter, chat_id, user_id, level, storage)

        elif command == "repo":
            repo_name = args[0] if args else None
            await self._cmd_repo(adapter, chat_id, user_id, repo_name, settings, storage)

        elif command == "restart":
            await self._cmd_restart(adapter, chat_id)

        elif command == "sync_threads":
            await self._cmd_sync_threads(adapter, chat_id, settings)

        # --- Classic Mode Commands ---

        elif command == "help":
            await self._cmd_help(adapter, chat_id)

        elif command == "continue":
            prompt = args[0] if args else None
            await self._cmd_continue(adapter, chat_id, user_id, prompt, storage, event_data)

        elif command == "end":
            await self._cmd_end(adapter, chat_id, user_id, storage, audit_logger)

        elif command == "ls":
            await self._cmd_ls(adapter, chat_id, user_id, settings, storage)

        elif command == "cd":
            directory = args[0] if args else None
            await self._cmd_cd(adapter, chat_id, user_id, directory, settings, storage)

        elif command == "pwd":
            await self._cmd_pwd(adapter, chat_id, user_id, storage)

        elif command == "projects":
            await self._cmd_projects(adapter, chat_id, settings)

        elif command == "export":
            format_type = args[0] if args else "markdown"
            await self._cmd_export(adapter, chat_id, user_id, format_type, storage)

        elif command == "actions":
            await self._cmd_actions(adapter, chat_id, user_id, settings, storage)

        elif command == "git":
            git_cmd = args[0] if args else "status"
            await self._cmd_git(adapter, chat_id, user_id, git_cmd, settings, storage)

        elif command == "list":
            await self._cmd_list(adapter, chat_id)

    # --- Command Implementation Methods ---

    async def _cmd_start(self, adapter, chat_id: str, sender: dict, settings) -> None:
        """Handle /start command."""
        username = sender.get("nickname", sender.get("user_id", "用户"))
        welcome = (
            f"👋 欢迎使用 Claude Code Bot, {username}!\n\n"
            f"🤖 我可以帮助你通过飞书远程访问 Claude Code。\n\n"
            f"<b>可用命令:</b>\n"
            f"• /new - 开始新会话\n"
            f"• /status - 查看会话状态\n"
            f"• /verbose [0|1|2] - 设置输出详细程度\n"
            f"• /repo [name] - 列出/切换工作目录\n"
            f"• /help - 显示完整帮助\n"
            f"• /ls - 列出文件\n"
            f"• /cd &lt;dir&gt; - 切换目录\n"
            f"• /pwd - 显示当前目录\n"
            f"• /projects - 显示所有项目\n"
            f"• /export - 导出会话\n"
            f"• /git - Git 命令\n\n"
            f"💡 发送任意消息与 Claude 对话！"
        )
        await adapter.send_message(chat_id, welcome)

    async def _cmd_new(self, adapter, chat_id: str, user_id: int, storage, audit_logger) -> None:
        """Handle /new command - start fresh session."""
        try:
            if storage:
                # Clear session for this user
                await storage.clear_user_session(user_id)
            # Also clear adapter internal state
            if hasattr(adapter, '_set_session_id'):
                adapter._set_session_id(user_id, None)
            msg = "🆕 已开始新会话，上下文已清除。"
            await adapter.send_message(chat_id, msg)
            if audit_logger:
                await audit_logger.log_command(user_id, "new", [], True)
        except Exception as e:
            await adapter.send_message(chat_id, f"❌ 重置会话失败: {str(e)}")

    async def _cmd_status(self, adapter, chat_id: str, user_id: int, storage, claude_integration) -> None:
        """Handle /status command."""
        try:
            status_parts = ["📊 <b>会话状态</b>\n"]

            # Get state from adapter
            work_dir = adapter._get_working_directory(user_id) if hasattr(adapter, '_get_working_directory') else "未知"
            session_id = adapter._get_session_id(user_id) if hasattr(adapter, '_get_session_id') else None

            if session_id:
                status_parts.append(f"• 会话 ID: <code>{session_id[:16]}...</code>")
            elif storage:
                session = await storage.get_active_session(user_id)
                if session:
                    status_parts.append(f"• 会话 ID: <code>{session.session_id[:16]}...</code>")
                else:
                    status_parts.append("• 无活跃会话")
            else:
                status_parts.append("• 无活跃会话")

            if work_dir and work_dir != "未知":
                status_parts.append(f"• 工作目录: <code>{work_dir}</code>")
            elif storage:
                session = await storage.get_active_session(user_id)
                if session:
                    status_parts.append(f"• 工作目录: <code>{session.working_directory}</code>")

            # Try to get created time from storage
            if storage:
                session = await storage.get_active_session(user_id)
                if session:
                    status_parts.append(f"• 创建时间: {session.created_at.strftime('%Y-%m-%d %H:%M')}")

            # Get cost info
            if claude_integration:
                cost_info = await claude_integration.get_user_cost(user_id)
                if cost_info:
                    status_parts.append(f"\n💰 <b>使用统计</b>")
                    status_parts.append(f"• 总成本: ${cost_info.get('total_cost', 0):.4f}")
                    status_parts.append(f"• 请求数: {cost_info.get('request_count', 0)}")

            status_parts.append(f"\n✅ Bot 运行正常")
            status_parts.append(f"📍 平台: Lark/飞书")

            await adapter.send_message(chat_id, "\n".join(status_parts))
        except Exception as e:
            await adapter.send_message(chat_id, f"✅ Bot 运行正常\n平台: Lark/飞书\n\n获取详细状态失败: {str(e)}")

    async def _cmd_verbose(self, adapter, chat_id: str, user_id: int, level: Optional[int], storage) -> None:
        """Handle /verbose command - set output verbosity."""
        # Use in-memory cache for user settings (like Telegram's context.user_data)
        if not hasattr(self, "_user_settings"):
            self._user_settings: Dict[int, Dict[str, Any]] = {}

        try:
            if level is None:
                # Get current level from memory cache
                current = self._user_settings.get(user_id, {}).get("verbose_level", 1)
                msg = (
                    f"📢 <b>当前详细程度: {current}</b>\n\n"
                    f"• 0 = 静默 (仅最终响应)\n"
                    f"• 1 = 正常 (显示工具名称)\n"
                    f"• 2 = 详细 (显示工具输入)"
                )
            else:
                if level not in (0, 1, 2):
                    await adapter.send_message(chat_id, "❌ 详细程度必须是 0、1 或 2")
                    return
                # Store in memory cache
                if user_id not in self._user_settings:
                    self._user_settings[user_id] = {}
                self._user_settings[user_id]["verbose_level"] = level
                msg = f"✅ 已设置详细程度为 {level}"
            await adapter.send_message(chat_id, msg)
        except Exception as e:
            await adapter.send_message(chat_id, f"❌ 设置失败: {str(e)}")

    async def _cmd_repo(self, adapter, chat_id: str, user_id: int, repo_name: Optional[str], settings, storage) -> None:
        """Handle /repo command - list or switch workspace."""
        # Use in-memory cache for user settings (like Telegram's context.user_data)
        if not hasattr(self, "_user_settings"):
            self._user_settings: Dict[int, Dict[str, Any]] = {}

        try:
            if settings is None:
                await adapter.send_message(chat_id, "⚠️ 配置不可用")
                return

            approved_dir = Path(settings.approved_directory)

            if repo_name:
                # Try to switch to specified repo
                target_dir = approved_dir / repo_name
                if target_dir.exists() and target_dir.is_dir():
                    # Store in memory cache
                    if user_id not in self._user_settings:
                        self._user_settings[user_id] = {}
                    self._user_settings[user_id]["working_directory"] = str(target_dir)

                    # Also update adapter's internal state
                    adapter._set_working_directory(user_id, str(target_dir))
                    adapter._set_session_id(user_id, None)  # Force new session for new directory

                    # Try to find a resumable session for the new directory
                    session_info = ""
                    claude_integration = self.deps.get("claude_integration")
                    if claude_integration:
                        try:
                            resumable = await claude_integration._find_resumable_session(user_id, target_dir)
                            if resumable:
                                adapter._set_session_id(user_id, resumable.session_id)
                                session_info = "\n\n📋 已恢复之前的会话"
                        except Exception:
                            session_info = ""

                    await adapter.send_message(chat_id, f"✅ 已切换到: <code>{repo_name}</code>{session_info}")
                else:
                    await adapter.send_message(chat_id, f"❌ 目录不存在: <code>{repo_name}</code>")
            else:
                # List available repos with git indicators
                repos = []
                current_dir = self._user_settings.get(user_id, {}).get("working_directory", str(approved_dir))
                for item in sorted(approved_dir.iterdir(), key=lambda x: x.name.lower()):
                    if item.is_dir() and not item.name.startswith("."):
                        has_git = (item / ".git").exists()
                        marker = " ◀" if str(item) == current_dir else ""
                        repos.append((item.name, has_git, marker))

                if repos:
                    repos_text = "\n".join(
                        f"• {'📦' if has_git else '📁'} <code>{name}</code>{marker}"
                        for name, has_git, marker in repos[:20]
                    )
                    msg = f"📁 <b>可用项目:</b>\n\n{repos_text}"
                    if len(repos) > 20:
                        msg += f"\n\n... 还有 {len(repos) - 20} 个项目"
                else:
                    msg = "📁 没有找到项目"
                await adapter.send_message(chat_id, msg)
        except Exception as e:
            await adapter.send_message(chat_id, f"❌ 获取项目列表失败: {str(e)}")

    async def _cmd_restart(self, adapter, chat_id: str) -> None:
        """Handle /restart command."""
        import os
        import signal
        await adapter.send_message(chat_id, "🔄 正在重启机器人...")
        os.kill(os.getpid(), signal.SIGTERM)

    async def _cmd_sync_threads(self, adapter, chat_id: str, settings) -> None:
        """Handle /sync_threads command."""
        if not settings or not getattr(settings, "enable_project_threads", False):
            await adapter.send_message(chat_id, "ℹ️ 项目主题功能未启用")
            return
        await adapter.send_message(chat_id, "🔄 项目主题同步功能需要通过 Telegram 使用")

    async def _cmd_help(self, adapter, chat_id: str) -> None:
        """Handle /help command."""
        help_text = (
            "🤖 <b>Claude Code Bot 帮助</b>\n\n"
            "<b>会话命令:</b>\n"
            "• /new - 开始新会话\n"
            "• /status - 查看会话状态\n"
            "• /verbose [0|1|2] - 设置输出详细程度\n\n"
            "<b>导航命令:</b>\n"
            "• /ls - 列出文件\n"
            "• /cd &lt;dir&gt; - 切换目录\n"
            "• /pwd - 显示当前目录\n"
            "• /repo [name] - 列出/切换项目\n"
            "• /projects - 显示所有项目\n\n"
            "<b>其他命令:</b>\n"
            "• /export - 导出会话\n"
            "• /git - Git 命令\n"
            "• /restart - 重启机器人\n\n"
            "<b>使用提示:</b>\n"
            "• 发送任意文本与 Claude 对话\n"
            "• 上传文件让 Claude 分析\n"
            "• 上传图片让 Claude 查看\n\n"
            "需要帮助? 联系管理员。"
        )
        await adapter.send_message(chat_id, help_text)

    async def _cmd_continue(self, adapter, chat_id: str, user_id: int, prompt: Optional[str], storage, event_data) -> None:
        """Handle /continue command - continue last session."""
        # Simply pass to message handler with optional prompt
        if prompt:
            # Modify event data to include just the prompt
            modified_event = dict(event_data)
            message = dict(event_data.get("message", {}))
            content = json.loads(message.get("content", "{}"))
            content["text"] = prompt
            message["content"] = json.dumps(content)
            modified_event["message"] = message
            await self.handle_message(modified_event)
        else:
            await adapter.send_message(chat_id, "请提供继续会话的内容，例如: /continue 请继续")

    async def _cmd_end(self, adapter, chat_id: str, user_id: int, storage, audit_logger) -> None:
        """Handle /end command - end current session."""
        try:
            if storage:
                await storage.clear_user_session(user_id)
            if hasattr(adapter, '_set_session_id'):
                adapter._set_session_id(user_id, None)
            await adapter.send_message(chat_id, "🏁 会话已结束，上下文已清除。")
            if audit_logger:
                await audit_logger.log_command(user_id, "end", [], True)
        except Exception as e:
            await adapter.send_message(chat_id, f"❌ 结束会话失败: {str(e)}")

    async def _cmd_ls(self, adapter, chat_id: str, user_id: int, settings, storage) -> None:
        """Handle /ls command - list files."""
        try:
            if settings is None:
                await adapter.send_message(chat_id, "⚠️ 配置不可用")
                return

            # Get working directory from adapter state first
            work_dir_str = adapter._get_working_directory(user_id) if hasattr(adapter, '_get_working_directory') else None
            if not work_dir_str:
                work_dir = settings.approved_directory
                if storage:
                    work_dir_str = await storage.get_user_setting(user_id, "working_directory", str(work_dir))
            work_dir = work_dir_str or str(settings.approved_directory)

            work_path = Path(work_dir)
            if not work_path.exists():
                await adapter.send_message(chat_id, f"❌ 目录不存在: <code>{work_dir}</code>")
                return

            # List files
            dirs = []
            files = []
            for item in sorted(work_path.iterdir(), key=lambda x: x.name.lower()):
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    dirs.append(item.name)
                else:
                    files.append(item.name)

            total_items = len(dirs) + len(files)

            # Check if adapter supports cards (Lark)
            if hasattr(adapter, 'platform_name') and adapter.platform_name == 'lark':
                # Build Lark card for file listing
                card_elements = []

                # Directory section
                if dirs:
                    dir_items = dirs[:30]
                    dir_content = "\n".join([f"📁 `{name}`" for name in dir_items])
                    if len(dirs) > 30:
                        dir_content += f"\n\n_... 还有 {len(dirs) - 30} 个文件夹_"
                    card_elements.append({
                        "tag": "div",
                        "text": {"content": f"**文件夹 ({len(dirs)})**\n{dir_content}", "tag": "lark_md"}
                    })

                # Files section
                if files:
                    file_items = files[:30]
                    file_content = "\n".join([f"📄 `{name}`" for name in file_items])
                    if len(files) > 30:
                        file_content += f"\n\n_... 还有 {len(files) - 30} 个文件_"
                    card_elements.append({
                        "tag": "div",
                        "text": {"content": f"**文件 ({len(files)})**\n{file_content}", "tag": "lark_md"}
                    })

                if not dirs and not files:
                    card_elements.append({
                        "tag": "div",
                        "text": {"content": "📁 目录为空", "tag": "lark_md"}
                    })

                card = {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"content": f"📁 {work_path.name}", "tag": "plain_text"},
                        "subtitle": {"content": f"{total_items} 项 · {work_dir}", "tag": "plain_text"},
                        "template": "blue"
                    },
                    "elements": card_elements
                }

                await adapter.send_card(chat_id, card)
            else:
                # Fallback to text for other platforms
                items = []
                for name in dirs:
                    items.append(f"📁 <code>{name}</code>")
                for name in files:
                    items.append(f"📄 <code>{name}</code>")

                if items:
                    msg = f"📁 <b>{work_path.name}</b>\n\n" + "\n".join(items[:50])
                    if len(items) > 50:
                        msg += f"\n\n... 还有 {len(items) - 50} 项"
                else:
                    msg = "📁 目录为空"
                await adapter.send_message(chat_id, msg)

        except Exception as e:
            await adapter.send_message(chat_id, f"❌ 列出文件失败: {str(e)}")

    async def _cmd_cd(self, adapter, chat_id: str, user_id: int, directory: Optional[str], settings, storage) -> None:
        """Handle /cd command - change directory."""
        if not directory:
            await adapter.send_message(chat_id, "请指定目录，例如: /cd myproject")
            return

        try:
            if settings is None:
                await adapter.send_message(chat_id, "⚠️ 配置不可用")
                return

            # Get current working directory
            current_dir = Path(settings.approved_directory)
            if storage:
                current_dir = Path(await storage.get_user_setting(user_id, "working_directory", str(current_dir)))

            # Resolve target directory
            if directory == "..":
                target_dir = current_dir.parent
            elif directory.startswith("/"):
                target_dir = Path(directory)
            else:
                target_dir = current_dir / directory

            # Security check - must be within approved directory
            approved_path = Path(settings.approved_directory).resolve()
            try:
                target_dir.resolve().relative_to(approved_path)
            except ValueError:
                await adapter.send_message(chat_id, "❌ 不能访问批准目录之外的路径")
                return

            if not target_dir.exists():
                await adapter.send_message(chat_id, f"❌ 目录不存在: <code>{directory}</code>")
                return

            if not target_dir.is_dir():
                await adapter.send_message(chat_id, f"❌ 不是目录: <code>{directory}</code>")
                return

            # Update working directory
            if storage:
                await storage.set_user_setting(user_id, "working_directory", str(target_dir))

            # Also update adapter's internal state
            adapter._set_working_directory(user_id, str(target_dir))
            adapter._set_session_id(user_id, None)  # Force new session for new directory

            # Try to find a resumable session for the new directory
            claude_integration = self.deps.get("claude_integration")
            if claude_integration:
                try:
                    resumable = await claude_integration._find_resumable_session(user_id, target_dir)
                    if resumable:
                        adapter._set_session_id(user_id, resumable.session_id)
                        session_info = "\n\n📋 已恢复之前的会话"
                    else:
                        session_info = ""
                except Exception:
                    session_info = ""
            else:
                session_info = ""

            await adapter.send_message(chat_id, f"✅ 已切换到: <code>{target_dir.relative_to(approved_path)}</code>{session_info}")
        except Exception as e:
            await adapter.send_message(chat_id, f"❌ 切换目录失败: {str(e)}")

    async def _cmd_pwd(self, adapter, chat_id: str, user_id: int, storage) -> None:
        """Handle /pwd command - print working directory."""
        try:
            work_dir = adapter._get_working_directory(user_id) if hasattr(adapter, '_get_working_directory') else None
            if not work_dir and storage:
                work_dir = await storage.get_user_setting(user_id, "working_directory", "未知")
            if not work_dir:
                work_dir = "未知"
            await adapter.send_message(chat_id, f"📁 当前目录: <code>{work_dir}</code>")
        except Exception as e:
            await adapter.send_message(chat_id, f"❌ 获取目录失败: {str(e)}")

    async def _cmd_projects(self, adapter, chat_id: str, settings) -> None:
        """Handle /projects command - show all projects."""
        try:
            if settings is None:
                await adapter.send_message(chat_id, "⚠️ 配置不可用")
                return

            approved_dir = Path(settings.approved_directory)
            projects = []

            for item in approved_dir.iterdir():
                if item.is_dir() and not item.name.startswith("."):
                    # Check if it looks like a project (has certain files)
                    is_project = any(
                        (item / f).exists()
                        for f in [".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "requirements.txt"]
                    )
                    projects.append((item.name, is_project))

            if projects:
                lines = []
                for name, is_proj in sorted(projects):
                    icon = "📦" if is_proj else "📁"
                    lines.append(f"{icon} <code>{name}</code>")
                msg = "📁 <b>所有项目:</b>\n\n" + "\n".join(lines[:30])
                if len(projects) > 30:
                    msg += f"\n\n... 还有 {len(projects) - 30} 个目录"
            else:
                msg = "📁 没有找到项目"
            await adapter.send_message(chat_id, msg)
        except Exception as e:
            await adapter.send_message(chat_id, f"❌ 获取项目失败: {str(e)}")

    async def _cmd_export(self, adapter, chat_id: str, user_id: int, format_type: str, storage) -> None:
        """Handle /export command - show format selection card or export directly."""
        try:
            if format_type in ("markdown", "json", "html"):
                # Direct export with specified format
                if storage is None:
                    await adapter.send_message(chat_id, "⚠️ 存储服务不可用")
                    return

                # Get session messages
                messages = await storage.get_session_messages(user_id)
                if not messages:
                    await adapter.send_message(chat_id, "没有可导出的会话内容")
                    return

                # Format based on type
                if format_type == "json":
                    import json
                    content = json.dumps(messages, indent=2, ensure_ascii=False, default=str)
                else:
                    # Markdown format
                    lines = ["# 会话导出\n"]
                    for msg in messages:
                        role = msg.get("role", "unknown")
                        content = msg.get("content", "")
                        lines.append(f"## {role}\n\n{content}\n")
                    content = "\n".join(lines)

                # Send as file
                await adapter.send_file(chat_id, content.encode(), filename=f"session_export.{format_type}")
            else:
                # Show format selection card
                export_card = {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"content": "📤 导出会话", "tag": "plain_text"},
                        "template": "violet"
                    },
                    "elements": [
                        {"tag": "div", "text": {"content": "请选择导出格式:", "tag": "lark_md"}},
                        {
                            "tag": "action_list",
                            "list": [
                                {"tag": "button", "text": {"content": "📝 Markdown", "tag": "plain_text"}, "type": "primary", "value": {"action": "export", "format": "markdown"}},
                                {"tag": "button", "text": {"content": "📊 JSON", "tag": "plain_text"}, "type": "default", "value": {"action": "export", "format": "json"}},
                                {"tag": "button", "text": {"content": "🌐 HTML", "tag": "plain_text"}, "type": "default", "value": {"action": "export", "format": "html"}},
                            ]
                        }
                    ]
                }
                await adapter.send_card(chat_id, export_card)
        except Exception as e:
            await adapter.send_message(chat_id, f"❌ 导出失败: {str(e)}")

    async def _cmd_actions(self, adapter, chat_id: str, user_id: int, settings, storage) -> None:
        """Handle /actions command - show quick actions as interactive card."""
        actions_card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": "⚡ 快速操作", "tag": "plain_text"},
                "template": "turquoise"
            },
            "elements": [
                {
                    "tag": "action_list",
                    "list": [
                        {"tag": "button", "text": {"content": "🔍 代码审查", "tag": "plain_text"}, "type": "primary", "value": {"action": "quick", "quick": "review"}},
                        {"tag": "button", "text": {"content": "🧪 运行测试", "tag": "plain_text"}, "type": "primary", "value": {"action": "quick", "quick": "test"}},
                        {"tag": "button", "text": {"content": "📝 生成文档", "tag": "plain_text"}, "type": "primary", "value": {"action": "quick", "quick": "docs"}},
                        {"tag": "button", "text": {"content": "🔧 修复问题", "tag": "plain_text"}, "type": "primary", "value": {"action": "quick", "quick": "fix"}},
                        {"tag": "button", "text": {"content": "🏗️ 构建", "tag": "plain_text"}, "type": "default", "value": {"action": "quick", "quick": "build"}},
                        {"tag": "button", "text": {"content": "🚀 启动服务", "tag": "plain_text"}, "type": "default", "value": {"action": "quick", "quick": "start"}},
                        {"tag": "button", "text": {"content": "🔍 Lint", "tag": "plain_text"}, "type": "default", "value": {"action": "quick", "quick": "lint"}},
                        {"tag": "button", "text": {"content": "✨ 格式化", "tag": "plain_text"}, "type": "default", "value": {"action": "quick", "quick": "format"}},
                    ]
                }
            ]
        }
        await adapter.send_card(chat_id, actions_card)

    async def _cmd_git(self, adapter, chat_id: str, user_id: int, git_cmd: str, settings, storage) -> None:
        """Handle /git command - git operations."""
        try:
            if settings is None:
                await adapter.send_message(chat_id, "⚠️ 配置不可用")
                return

            # Get working directory from adapter state first, then storage
            work_dir_str = adapter._get_working_directory(user_id) if hasattr(adapter, '_get_working_directory') else None
            if not work_dir_str:
                work_dir = Path(settings.approved_directory)
                if storage:
                    work_dir_str = await storage.get_user_setting(user_id, "working_directory", str(work_dir))
            work_dir = Path(work_dir_str) if work_dir_str else Path(settings.approved_directory)

            # Check if it's a git repo
            git_dir = work_dir / ".git"
            if not git_dir.exists():
                await adapter.send_message(chat_id, "❌ 当前目录不是 Git 仓库")
                return

            import subprocess

            if git_cmd == "status":
                result = subprocess.run(["git", "status", "--short"], cwd=work_dir, capture_output=True, text=True)
                msg = f"📋 <b>Git Status</b>\n\n<pre>{result.stdout or '工作区干净'}</pre>"
            elif git_cmd == "log":
                result = subprocess.run(["git", "log", "--oneline", "-10"], cwd=work_dir, capture_output=True, text=True)
                msg = f"📜 <b>Git Log (最近10条)</b>\n\n<pre>{result.stdout or '无提交历史'}</pre>"
            elif git_cmd == "branch":
                result = subprocess.run(["git", "branch"], cwd=work_dir, capture_output=True, text=True)
                msg = f"🌿 <b>Git Branches</b>\n\n<pre>{result.stdout or '无分支'}</pre>"
            elif git_cmd == "diff":
                result = subprocess.run(["git", "diff", "--stat"], cwd=work_dir, capture_output=True, text=True)
                msg = f"📊 <b>Git Diff</b>\n\n<pre>{result.stdout or '无变更'}</pre>"
            else:
                msg = (
                    "🌳 <b>Git 命令</b>\n\n"
                    "• /git status - 查看状态\n"
                    "• /git log - 查看提交历史\n"
                    "• /git branch - 查看分支\n"
                    "• /git diff - 查看变更统计"
                )

            await adapter.send_message(chat_id, msg)
        except Exception as e:
            await adapter.send_message(chat_id, f"❌ Git 命令失败: {str(e)}")

    async def _cmd_list(self, adapter, chat_id: str) -> None:
        """Handle /list command - show all available commands with descriptions."""
        commands_list = (
            "📋 <b>所有可用命令</b>\n\n"
            "<b>会话管理:</b>\n"
            "• /start - 开始使用机器人，显示欢迎信息\n"
            "• /new - 开始新的 Claude 会话\n"
            "• /status - 查看当前会话状态\n"
            "• /continue [prompt] - 继续上一次会话\n"
            "• /end - 结束当前会话\n"
            "• /restart - 重启机器人\n\n"
            "<b>输出控制:</b>\n"
            "• /verbose [0|1|2] - 设置输出详细程度\n"
            "  • 0 = 静默（仅最终结果）\n"
            "  • 1 = 正常（显示工具名称）\n"
            "  • 2 = 详细（显示工具输入和推理）\n\n"
            "<b>目录导航:</b>\n"
            "• /ls - 列出当前目录文件\n"
            "• /cd &lt;dir&gt; - 切换工作目录\n"
            "• /pwd - 显示当前工作目录\n"
            "• /repo [name] - 列出/切换项目仓库\n"
            "• /projects - 显示所有可用项目\n\n"
            "<b>Git 操作:</b>\n"
            "• /git [status|log|branch] - 执行 Git 命令\n\n"
            "<b>其他功能:</b>\n"
            "• /export [format] - 导出会话记录\n"
            "  • markdown (默认)\n"
            "  • json\n"
            "  • txt\n"
            "• /actions - 显示快速操作提示\n"
            "• /help - 显示帮助信息\n"
            "• /list - 显示此命令列表\n\n"
            "<b>同步功能:</b>\n"
            "• /sync_threads - 同步项目线程\n\n"
            "💡 <i>发送任意文本与 Claude 对话，上传文件/图片让 Claude 分析</i>"
        )
        await adapter.send_message(chat_id, commands_list)

    # --- Telegram Handler Wrappers for Utility Commands ---
    # These methods adapt Telegram handler signatures to the existing command implementations

    async def _cmd_ls_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /ls command in agentic mode (Telegram)."""
        from .handlers import command
        await command.list_files(update, context)

    async def _cmd_cd_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /cd command in agentic mode (Telegram)."""
        from .handlers import command
        await command.change_directory(update, context)

    async def _cmd_pwd_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /pwd command in agentic mode (Telegram)."""
        from .handlers import command
        await command.print_working_directory(update, context)

    async def _cmd_projects_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /projects command in agentic mode (Telegram)."""
        from .handlers import command
        await command.show_projects(update, context)

    async def _cmd_export_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /export command in agentic mode (Telegram)."""
        from .handlers import command
        await command.export_session(update, context)

    async def _cmd_actions_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /actions command in agentic mode (Telegram)."""
        from .handlers import command
        await command.quick_actions(update, context)

    async def _cmd_git_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /git command in agentic mode (Telegram)."""
        from .handlers import command
        await command.git_command(update, context)

    async def _cmd_list_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /list command in agentic mode (Telegram)."""
        # Show all available commands
        commands_text = (
            "📋 <b>所有可用命令</b>\n\n"
            "<b>会话管理:</b>\n"
            "• /start - 开始使用机器人\n"
            "• /new - 开始新会话\n"
            "• /status - 查看会话状态\n"
            "• /verbose [0|1|2] - 设置输出详细程度\n\n"
            "<b>目录导航:</b>\n"
            "• /ls - 列出文件\n"
            "• /cd &lt;dir&gt; - 切换目录\n"
            "• /pwd - 显示当前目录\n"
            "• /repo [name] - 列出/切换项目\n"
            "• /projects - 显示所有项目\n\n"
            "<b>其他功能:</b>\n"
            "• /export - 导出会话\n"
            "• /actions - 快速操作\n"
            "• /git - Git 命令\n"
            "• /restart - 重启机器人\n"
            "• /help - 显示帮助\n"
            "• /list - 显示此列表\n\n"
            "💡 <i>发送任意文本与 Claude 对话</i>"
        )
        await update.message.reply_text(commands_text, parse_mode="HTML")

    async def _cmd_help_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command in agentic mode (Telegram)."""
        # In agentic mode, show simple help
        help_text = (
            "🤖 <b>Claude Code Bot</b>\n\n"
            "我是你的 AI 编程助手。你可以:\n"
            "• 发送消息让我帮你写代码、分析问题\n"
            "• 上传文件让我分析\n"
            "• 上传图片让我查看\n\n"
            "<b>常用命令:</b>\n"
            "• /new - 开始新会话\n"
            "• /status - 查看状态\n"
            "• /verbose [0|1|2] - 设置详细程度\n"
            "• /repo - 列出/切换项目\n"
            "• /list - 显示所有命令\n"
        )
        await update.message.reply_text(help_text, parse_mode="HTML")

    async def handle_callback(self, event_data: Dict[str, Any]) -> None:
        """Handle callback from Lark platform (button clicks)."""
        import structlog
        logger = structlog.get_logger()

        logger.info("Lark callback received", event_data=event_data)
