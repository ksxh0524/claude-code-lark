"""Platform-agnostic data models for multi-platform support."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from datetime import datetime


class PlatformType(Enum):
    """Supported platform types."""

    TELEGRAM = "telegram"
    LARK = "lark"


class MessageType(Enum):
    """Message types."""

    TEXT = "text"
    COMMAND = "command"
    FILE = "file"
    IMAGE = "image"
    VOICE = "voice"
    CARD_CALLBACK = "card_callback"


@dataclass
class PlatformUser:
    """Platform-agnostic user representation."""

    user_id: str  # Platform-specific user ID (string for compatibility)
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_bot: bool = False
    language_code: Optional[str] = None
    platform: PlatformType = PlatformType.TELEGRAM
    raw: Optional[Dict[str, Any]] = None  # Raw platform data

    @property
    def full_name(self) -> str:
        """Get user's full name."""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.username or self.first_name or "Unknown"


@dataclass
class PlatformFile:
    """Platform-agnostic file representation."""

    file_id: str
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    file_url: Optional[str] = None  # For platforms with direct URL
    file_data: Optional[bytes] = None  # Downloaded file data
    platform: PlatformType = PlatformType.TELEGRAM
    raw: Optional[Dict[str, Any]] = None


@dataclass
class PlatformCardElement:
    """Card element for interactive components."""

    type: str  # button, select, input, etc.
    text: str
    value: Optional[str] = None
    action: Optional[str] = None  # callback_data, url, etc.
    style: Optional[str] = None  # primary, danger, etc.
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class PlatformCard:
    """Platform-agnostic card/message for rich interactive content."""

    title: Optional[str] = None
    content: Optional[str] = None
    elements: List[PlatformCardElement] = field(default_factory=list)
    image_url: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class PlatformMessage:
    """Platform-agnostic message representation."""

    message_id: str
    user: PlatformUser
    chat_id: str
    content: Optional[str] = None
    message_type: MessageType = MessageType.TEXT
    file: Optional[PlatformFile] = None
    reply_to_message_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    platform: PlatformType = PlatformType.TELEGRAM
    raw: Optional[Dict[str, Any]] = None
    # Thread/topic support
    thread_id: Optional[str] = None
    topic_id: Optional[str] = None


@dataclass
class PlatformResponse:
    """Platform-agnostic response from bot actions."""

    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class PlatformEvent:
    """Platform-agnostic event representation."""

    event_type: str  # message, callback, etc.
    platform: PlatformType
    data: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    raw: Optional[Dict[str, Any]] = None
