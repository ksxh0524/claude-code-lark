"""Lark/Feishu platform adapter implementation."""

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Union
from datetime import datetime

import structlog

try:
    from lark_oapi.api.auth.v3 import *
    from lark_oapi.api.contact.user.v3 import *
    from lark_oapi.api.im.v1 import *
    from lark_oapi.api.application.v6 import *
    from lark_oapi import *
except ImportError:
    # If SDK not installed yet, provide error
    LARK_AVAILABLE = False
else:
    LARK_AVAILABLE = True

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
    """Lark/Feishu platform adapter using lark-oapi."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Lark adapter.

        Args:
            config: Configuration dict with:
                - app_id: Lark app ID (cli_xxxxxxxxx)
                - app_secret: Lark app secret
                - encrypt_key: Optional encryption key
                - verification_token: Optional verification token
                - webhook_url: Optional webhook URL for receiving events
        """
        super().__init__(config)
        self.platform_type = PlatformType.LARK
        self.app_id = config["app_id"]
        self.app_secret = config["app_secret"]
        self.encrypt_key = config.get("encrypt_key")
        self.verification_token = config.get("verification_token")
        self.webhook_url = config.get("webhook_url")

        self.client: Optional[Client] = None
        self.is_running = False

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

        # Create Lark client
        self.client = Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .build()

        # Test connection by getting app info
        try:
            auth_req = GetAppInternalAppRequest.builder().build()
            response = await self._execute_async(
                self.client.auth.v3.appInternalApp.get,
                auth_req
            )
            if response.code == 0:
                logger.info("Lark adapter initialized successfully")
            else:
                logger.warning("Lark auth test failed", code=response.code, msg=response.msg)
        except Exception as e:
            logger.error("Failed to initialize Lark client", error=str(e))
            raise

    async def start(self) -> None:
        """Start receiving events from Lark (webhook mode)."""
        if self.is_running:
            logger.warning("Lark adapter already running")
            return

        await self.initialize()

        logger.info("Starting Lark adapter", mode="webhook")

        # Lark uses webhook mode exclusively
        # Events will be received via HTTP endpoint
        # The webhook server should be set up separately
        self.is_running = True
        logger.info("Lark adapter started (webhook mode)")

    async def stop(self) -> None:
        """Stop Lark adapter."""
        if not self.is_running:
            return

        logger.info("Stopping Lark adapter")
        self.is_running = False
        logger.info("Lark adapter stopped")

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

            request = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type(message_type)
                        .content(json.dumps(content))
                        .reply_to_message_id(reply_to_message_id)
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
        card: PlatformCard,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Send interactive card to Lark chat."""
        try:
            # Build Lark card format
            card_content = self._build_lark_card(card)

            request = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
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

    def is_running(self) -> bool:
        """Check if adapter is running."""
        return self.is_running
