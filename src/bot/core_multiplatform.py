"""Multi-platform bot core supporting Telegram and Lark."""

import asyncio
from typing import Any, Callable, Dict, Optional

import structlog

from src.bot.adapters.base import PlatformAdapter
from src.bot.adapters.factory import PlatformFactory
from src.config.settings import Settings
from src.exceptions import ClaudeCodeTelegramError
from .core_engine import CoreEngine
from .features.registry import FeatureRegistry
from .orchestrator import MessageOrchestrator

logger = structlog.get_logger()


class MultiPlatformBot:
    """Multi-platform bot orchestrator supporting Telegram and Lark."""

    def __init__(self, settings: Settings, dependencies: Dict[str, Any]):
        """Initialize multi-platform bot with settings and dependencies."""
        self.settings = settings
        self.deps = dependencies
        self.adapter: Optional[PlatformAdapter] = None
        self.is_running = False
        self.feature_registry: Optional[FeatureRegistry] = None
        self.orchestrator = MessageOrchestrator(settings, dependencies)
        self.core_engine: Optional[CoreEngine] = None

    async def initialize(self) -> None:
        """Initialize bot application. Idempotent — safe to call multiple times."""
        if self.adapter is not None:
            return

        logger.info("Initializing multi-platform bot", platform=self.settings.platform)

        # Create CoreEngine
        self.core_engine = CoreEngine(
            claude_integration=self.deps.get("claude_integration"),
            settings=self.settings,
            deps={
                "rate_limiter": self.deps.get("rate_limiter"),
                "security": self.deps.get("security_validator"),
                "storage": self.deps.get("storage"),
            },
        )
        self.deps["core_engine"] = self.core_engine

        # Create platform adapter
        self.adapter = PlatformFactory.create_adapter(self.settings)

        # Initialize platform adapter
        await self.adapter.initialize()

        # Inject CoreEngine and settings for Lark
        if hasattr(self.adapter, "core_engine"):
            self.adapter.core_engine = self.core_engine
        if hasattr(self.adapter, "settings"):
            self.adapter.settings = self.settings
        # Inject orchestrator reference (for verbose_level and user settings)
        if hasattr(self.adapter, "orchestrator"):
            self.adapter.orchestrator = self.orchestrator
        # Inject storage reference (for session persistence)
        if hasattr(self.adapter, "storage"):
            self.adapter.storage = self.deps.get("storage")
        # Inject security validator reference (for file upload validation)
        if hasattr(self.adapter, "security_validator"):
            self.adapter.security_validator = self.deps.get("security_validator")

        # Add adapter to deps so handlers can reply
        self.deps["adapter"] = self.adapter
        self.orchestrator.deps["adapter"] = self.adapter
        self.orchestrator.deps["core_engine"] = self.core_engine
        self.orchestrator.deps["settings"] = self.settings

        # Initialize feature registry
        self.feature_registry = FeatureRegistry(
            config=self.settings,
            storage=self.deps.get("storage"),
            security=self.deps.get("security"),
        )

        # Add feature registry to dependencies
        self.deps["features"] = self.feature_registry

        # Register handlers with adapter
        await self._register_handlers()

        logger.info(
            "Multi-platform bot initialized",
            platform=self.settings.platform,
            adapter=type(self.adapter).__name__,
        )

    async def _register_handlers(self) -> None:
        """Register message and command handlers."""
        logger.info("Registering platform handlers")

        # Register command handlers
        commands = await self.orchestrator.get_bot_commands()
        command_list = [cmd.command for cmd in commands]

        async def command_handler(event_data: Dict[str, Any]):
            """Handle commands."""
            await self.orchestrator.handle_command(event_data)

        await self.adapter.register_command_handler(command_list, command_handler)

        # Register message handler
        async def message_handler(event_data: Dict[str, Any]):
            """Handle messages."""
            await self.orchestrator.handle_message(event_data)

        await self.adapter.register_message_handler(message_handler)

        # Register callback handler
        async def callback_handler(event_data: Dict[str, Any]):
            """Handle button callbacks."""
            await self.orchestrator.handle_callback(event_data)

        await self.adapter.register_callback_handler(callback_handler)

        logger.info("Handlers registered", commands=command_list)

    async def start(self) -> None:
        """Start the bot."""
        if self.is_running:
            logger.warning("Bot is already running")
            return

        await self.initialize()

        logger.info(
            "Starting multi-platform bot",
            platform=self.settings.platform,
            mode="webhook" if self.settings.webhook_url or self.settings.lark_webhook_url else "polling",
        )

        try:
            self.is_running = True
            await self.adapter.start()

            logger.info("Bot started successfully", platform=self.settings.platform)
        except Exception as e:
            logger.error("Failed to start bot", error=str(e))
            self.is_running = False
            raise ClaudeCodeTelegramError(f"Failed to start bot: {e}")

    async def stop(self) -> None:
        """Stop the bot."""
        if not self.is_running:
            logger.warning("Bot is not running")
            return

        logger.info("Stopping bot")

        self.is_running = False

        if self.adapter:
            await self.adapter.stop()

        logger.info("Bot stopped")

    def get_adapter(self) -> Optional[PlatformAdapter]:
        """Get the platform adapter instance."""
        return self.adapter

    async def send_message(
        self,
        chat_id: str,
        text: str,
        **kwargs: Any,
    ) -> None:
        """Send a message through the platform adapter."""
        if not self.adapter:
            raise ClaudeCodeTelegramError("Bot not initialized")

        await self.adapter.send_message(chat_id, text, **kwargs)

    async def send_card(
        self,
        chat_id: str,
        card,
        **kwargs: Any,
    ) -> None:
        """Send a card through the platform adapter."""
        if not self.adapter:
            raise ClaudeCodeTelegramError("Bot not initialized")

        await self.adapter.send_card(chat_id, card, **kwargs)
