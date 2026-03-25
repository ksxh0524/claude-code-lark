"""Telegram platform adapter implementation."""

import asyncio
from typing import Any, Callable, Dict, List, Optional, Union

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    Update,
    User,
)
from telegram.ext import Application, ContextTypes
from telegram.request import BaseRequest

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
import structlog

logger = structlog.get_logger()


class TelegramAdapter(PlatformAdapter):
    """Telegram platform adapter using python-telegram-bot."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Telegram adapter.

        Args:
            config: Configuration dict with:
                - token: Telegram bot token
                - webhook_url: Optional webhook URL
                - webhook_secret: Optional webhook secret
        """
        super().__init__(config)
        self.platform_type = PlatformType.TELEGRAM
        self.token = config["token"]
        self.webhook_url = config.get("webhook_url")
        self.webhook_secret = config.get("webhook_secret")

        self.app: Optional[Application] = None
        self.bot: Optional[Bot] = None
        self.is_running = False

    async def initialize(self) -> None:
        """Initialize Telegram application."""
        if self.app is not None:
            return

        logger.info("Initializing Telegram adapter")

        # Build application
        builder = Application.builder()
        builder.token(self.token)

        # Configure connection settings
        builder.connect_timeout(30)
        builder.read_timeout(30)
        builder.write_timeout(30)
        builder.pool_timeout(30)

        # Rate limiting
        from telegram.ext import AIORateLimiter

        builder.rate_limiter(AIORateLimiter(max_retries=1))

        self.app = builder.build()

        # Initialize to get bot instance
        await self.app.initialize()
        self.bot = self.app.bot

        logger.info("Telegram adapter initialized")

    async def start(self) -> None:
        """Start receiving updates from Telegram."""
        if self.is_running:
            logger.warning("Telegram adapter already running")
            return

        await self.initialize()

        logger.info("Starting Telegram adapter", mode="webhook" if self.webhook_url else "polling")

        self.is_running = True

        if self.webhook_url:
            await self.app.start()
            await self.app.bot.set_webhook(
                url=self.webhook_url,
                secret_token=self.webhook_secret,
            )
            logger.info("Webhook started", url=self.webhook_url)
        else:
            await self.app.start()
            await self.app.updater.start_polling()
            logger.info("Polling started")

    async def stop(self) -> None:
        """Stop Telegram adapter."""
        if not self.is_running:
            return

        logger.info("Stopping Telegram adapter")

        self.is_running = False

        if self.webhook_url:
            await self.bot.delete_webhook()

        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

        logger.info("Telegram adapter stopped")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: Optional[str] = None,
        disable_preview: bool = False,
        reply_to_message_id: Optional[str] = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Send text message to Telegram chat."""
        try:
            message = await self.bot.send_message(
                chat_id=int(chat_id),  # Telegram uses int chat_id
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_preview,
                reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
                **kwargs,
            )
            return PlatformResponse(
                success=True,
                message_id=str(message.message_id),
                raw={"message_id": message.message_id},
            )
        except Exception as e:
            logger.error("Failed to send message", error=str(e))
            return PlatformResponse(success=False, error=str(e))

    async def send_card(
        self,
        chat_id: str,
        card: PlatformCard,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Send interactive card as inline keyboard."""
        try:
            # Build inline keyboard from card elements
            keyboard = self._build_inline_keyboard(card)
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

            message = await self.bot.send_message(
                chat_id=int(chat_id),
                text=card.content or card.title or "",
                reply_markup=reply_markup,
                parse_mode="HTML",
                **kwargs,
            )

            return PlatformResponse(
                success=True,
                message_id=str(message.message_id),
                raw={"message_id": message.message_id},
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
            keyboard = self._build_inline_keyboard(card) if card else None
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

            message = await self.bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=text or (card.content if card else ""),
                reply_markup=reply_markup,
                parse_mode="HTML",
                **kwargs,
            )

            return PlatformResponse(
                success=True,
                message_id=str(message.message_id),
                raw={"message_id": message.message_id},
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
        """Delete message."""
        try:
            await self.bot.delete_message(
                chat_id=int(chat_id),
                message_id=int(message_id),
                **kwargs,
            )
            return PlatformResponse(success=True)
        except Exception as e:
            logger.error("Failed to delete message", error=str(e))
            return PlatformResponse(success=False, error=str(e))

    async def send_file(
        self,
        chat_id: str,
        file: Union[PlatformFile, bytes, str],
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Send file to Telegram chat."""
        try:
            if isinstance(file, PlatformFile):
                # Download from platform file_id
                file_data = await self.download_file(file.file_id)
                if not file_data:
                    return PlatformResponse(success=False, error="Failed to download file")
                file = file_data
                filename = filename or file.file_name

            message = await self.bot.send_document(
                chat_id=int(chat_id),
                document=file,
                filename=filename,
                caption=caption,
                **kwargs,
            )

            return PlatformResponse(
                success=True,
                message_id=str(message.message_id),
                raw={"message_id": message.message_id},
            )
        except Exception as e:
            logger.error("Failed to send file", error=str(e))
            return PlatformResponse(success=False, error=str(e))

    async def download_file(self, file_id: str, **kwargs: Any) -> Optional[bytes]:
        """Download file from Telegram."""
        try:
            file = await self.bot.get_file(file_id)
            return await file.download_as_bytearray()
        except Exception as e:
            logger.error("Failed to download file", error=str(e))
            return None

    def extract_user(self, event_data: Dict[str, Any]) -> PlatformUser:
        """Extract user from Telegram Update."""
        update = event_data.get("update") if isinstance(event_data, dict) else event_data

        if isinstance(update, Update):
            user = update.effective_user
        elif isinstance(event_data, dict) and "user" in event_data:
            user_data = event_data["user"]
            user = User(
                id=user_data.get("id", 0),
                is_bot=user_data.get("is_bot", False),
                first_name=user_data.get("first_name"),
                last_name=user_data.get("last_name"),
                username=user_data.get("username"),
            )
        else:
            raise ValueError("Invalid event data format")

        return PlatformUser(
            user_id=str(user.id),
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            is_bot=user.is_bot,
            language_code=user.language_code,
            platform=PlatformType.TELEGRAM,
        )

    def extract_message(self, event_data: Dict[str, Any]) -> Optional[PlatformMessage]:
        """Extract message from Telegram Update."""
        update = event_data.get("update") if isinstance(event_data, dict) else event_data

        if not isinstance(update, Update):
            return None

        if not update.effective_message:
            return None

        msg = update.effective_message
        user = self.extract_user(event_data)

        # Determine message type
        message_type = MessageType.TEXT
        file_obj = None

        if msg.text:
            message_type = MessageType.COMMAND if msg.text.startswith("/") else MessageType.TEXT
        elif msg.photo:
            message_type = MessageType.IMAGE
            # Get largest photo
            photo = msg.photo[-1]
            file_obj = PlatformFile(
                file_id=photo.file_id,
                platform=PlatformType.TELEGRAM,
            )
        elif msg.document:
            message_type = MessageType.FILE
            file_obj = PlatformFile(
                file_id=msg.document.file_id,
                file_name=msg.document.file_name,
                file_size=msg.document.file_size,
                mime_type=msg.document.mime_type,
                platform=PlatformType.TELEGRAM,
            )
        elif msg.voice:
            message_type = MessageType.VOICE
            file_obj = PlatformFile(
                file_id=msg.voice.file_id,
                file_size=msg.voice.file_size,
                mime_type=msg.voice.mime_type,
                platform=PlatformType.TELEGRAM,
            )
        elif msg.callback_query:
            message_type = MessageType.CARD_CALLBACK

        return PlatformMessage(
            message_id=str(msg.message_id),
            user=user,
            chat_id=str(msg.chat.id),
            content=msg.text or msg.caption or "",
            message_type=message_type,
            file=file_obj,
            reply_to_message_id=str(msg.reply_to_message.message_id) if msg.reply_to_message else None,
            platform=PlatformType.TELEGRAM,
            raw={"update": update},
        )

    def get_chat_id(self, event_data: Dict[str, Any]) -> str:
        """Extract chat ID from Telegram Update."""
        update = event_data.get("update") if isinstance(event_data, dict) else event_data

        if isinstance(update, Update) and update.effective_chat:
            return str(update.effective_chat.id)

        raise ValueError("Could not extract chat_id from event")

    async def send_action(
        self,
        chat_id: str,
        action: str = "typing",
        **kwargs: Any,
    ) -> None:
        """Send chat action."""
        try:
            # Map common actions to Telegram actions
            action_map = {
                "typing": "typing",
                "upload_photo": "upload_photo",
                "record_video": "record_video",
                "upload_video": "upload_video",
                "record_audio": "record_audio",
                "upload_audio": "upload_audio",
                "upload_document": "upload_document",
                "find_location": "find_location",
                "record_video_note": "record_video_note",
                "upload_video_note": "upload_video_note",
            }

            telegram_action = action_map.get(action, "typing")

            await self.bot.send_chat_action(
                chat_id=int(chat_id),
                action=telegram_action,
                **kwargs,
            )
        except Exception as e:
            logger.warning("Failed to send action", error=str(e))

    def _build_inline_keyboard(
        self, card: PlatformCard
    ) -> List[List[InlineKeyboardButton]]:
        """Build inline keyboard from PlatformCard."""
        keyboard = []

        if not card.elements:
            return keyboard

        # Group elements into rows
        current_row = []
        for element in card.elements:
            button = InlineKeyboardButton(
                text=element.text,
                callback_data=element.value,
                url=element.action if element.action and element.action.startswith("http") else None,
            )
            current_row.append(button)

            # Start new row after certain number of buttons
            if len(current_row) >= 2:
                keyboard.append(current_row)
                current_row = []

        if current_row:
            keyboard.append(current_row)

        return keyboard

    @property
    def platform_name(self) -> str:
        """Get platform name."""
        return "telegram"

    def get_application(self) -> Optional[Application]:
        """Get the underlying Telegram Application."""
        return self.app

    def is_running(self) -> bool:
        """Check if adapter is running."""
        return self.is_running
