Abstract base class for platform adapters."""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Union

from src.bot.adapters.models import (
    PlatformCard,
    PlatformEvent,
    PlatformFile,
    PlatformMessage,
    PlatformResponse,
    PlatformType,
    PlatformUser,
)


class PlatformAdapter(ABC):
    """
    Abstract base class for platform adapters.

    Each platform (Telegram, Lark, etc.) must implement this interface
    to provide a unified API for the bot logic.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the platform adapter.

        Args:
            config: Platform-specific configuration
        """
        self.config = config
        self.platform_type: PlatformType = PlatformType.TELEGRAM

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the platform connection and resources."""

    @abstractmethod
    async def start(self) -> None:
        """Start receiving events from the platform."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the platform connection and cleanup resources."""

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: Optional[str] = None,
        disable_preview: bool = False,
        reply_to_message_id: Optional[str] = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """
        Send a text message to a chat.

        Args:
            chat_id: Target chat ID
            text: Message text
            parse_mode: Parse mode (HTML, Markdown, etc.)
            disable_preview: Disable link preview
            reply_to_message_id: Reply to specific message
            **kwargs: Platform-specific arguments

        Returns:
            PlatformResponse with sent message info
        """

    @abstractmethod
    async def send_card(
        self,
        chat_id: str,
        card: PlatformCard,
        **kwargs: Any,
    ) -> PlatformResponse:
        """
        Send an interactive card/message.

        Args:
            chat_id: Target chat ID
            card: Card content with interactive elements
            **kwargs: Platform-specific arguments

        Returns:
            PlatformResponse with sent message info
        """

    @abstractmethod
    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: Optional[str] = None,
        card: Optional[PlatformCard] = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """
        Edit an existing message.

        Args:
            chat_id: Chat ID
            message_id: Message ID to edit
            text: New text content
            card: New card content
            **kwargs: Platform-specific arguments

        Returns:
            PlatformResponse with updated message info
        """

    @abstractmethod
    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
        **kwargs: Any,
    ) -> PlatformResponse:
        """
        Delete a message.

        Args:
            chat_id: Chat ID
            message_id: Message ID to delete
            **kwargs: Platform-specific arguments

        Returns:
            PlatformResponse
        """

    @abstractmethod
    async def send_file(
        self,
        chat_id: str,
        file: Union[PlatformFile, bytes, str],
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """
        Send a file to a chat.

        Args:
            chat_id: Target chat ID
            file: File to send (PlatformFile, bytes, or path)
            filename: File name
            caption: File caption
            **kwargs: Platform-specific arguments

        Returns:
            PlatformResponse with sent message info
        """

    @abstractmethod
    async def download_file(
        self,
        file_id: str,
        **kwargs: Any,
    ) -> Optional[bytes]:
        """
        Download a file from the platform.

        Args:
            file_id: Platform file ID
            **kwargs: Platform-specific arguments

        Returns:
            File data as bytes, or None if failed
        """

    @abstractmethod
    def extract_user(self, event_data: Dict[str, Any]) -> PlatformUser:
        """
        Extract user information from platform event.

        Args:
            event_data: Raw event data from platform

        Returns:
            PlatformUser object
        """

    @abstractmethod
    def extract_message(self, event_data: Dict[str, Any]) -> Optional[PlatformMessage]:
        """
        Extract message from platform event.

        Args:
            event_data: Raw event data from platform

        Returns:
            PlatformMessage object, or None if not a message event
        """

    @abstractmethod
    def get_chat_id(self, event_data: Dict[str, Any]) -> str:
        """
        Extract chat ID from platform event.

        Args:
            event_data: Raw event data from platform

        Returns:
            Chat ID as string
        """

    async def send_action(
        self,
        chat_id: str,
        action: str = "typing",
        **kwargs: Any,
    ) -> None:
        """
        Send a chat action (typing, uploading, etc.).

        Args:
            chat_id: Target chat ID
            action: Action type (typing, uploading_photo, etc.)
            **kwargs: Platform-specific arguments
        """
        # Default implementation does nothing
        # Override if platform supports chat actions
        pass

    async def register_command_handler(
        self,
        commands: List[str],
        handler: Callable,
        **kwargs: Any,
    ) -> None:
        """
        Register command handlers.

        Args:
            commands: List of command strings (e.g., ['/start', '/help'])
            handler: Async handler function
            **kwargs: Platform-specific arguments
        """
        # Default implementation - override as needed
        pass

    async def register_callback_handler(
        self,
        handler: Callable,
        **kwargs: Any,
    ) -> None:
        """
        Register callback/button handler.

        Args:
            handler: Async handler function
            **kwargs: Platform-specific arguments
        """
        # Default implementation - override as needed
        pass

    async def register_message_handler(
        self,
        handler: Callable,
        **kwargs: Any,
    ) -> None:
        """
        Register message handler.

        Args:
            handler: Async handler function
            **kwargs: Platform-specific arguments
        """
        # Default implementation - override as needed
        pass

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Get platform name."""
        pass

    def is_running(self) -> bool:
        """Check if the platform adapter is running."""
        return True
