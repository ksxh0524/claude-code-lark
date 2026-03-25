"""Platform factory for creating platform adapters."""

from typing import Dict, Optional

from src.bot.adapters.base import PlatformAdapter
from src.bot.adapters.lark import LarkAdapter
from src.bot.adapters.telegram import TelegramAdapter
from src.config.settings import Settings
from src.exceptions import ConfigurationError


class PlatformFactory:
    """Factory for creating platform adapters based on configuration."""

    @staticmethod
    def create_adapter(settings: Settings) -> PlatformAdapter:
        """
        Create a platform adapter based on settings.

        Args:
            settings: Application settings

        Returns:
            PlatformAdapter instance

        Raises:
            ConfigurationError: If platform is not supported or configuration is invalid
        """
        platform = settings.platform.lower()

        if platform == "telegram":
            return PlatformFactory._create_telegram_adapter(settings)
        elif platform == "lark":
            return PlatformFactory._create_lark_adapter(settings)
        else:
            raise ConfigurationError(
                f"Unsupported platform: {platform}. "
                f"Supported platforms: telegram, lark"
            )

    @staticmethod
    def _create_telegram_adapter(settings: Settings) -> TelegramAdapter:
        """Create Telegram adapter."""
        config = {
            "token": settings.telegram_token_str,
            "webhook_url": settings.webhook_url,
            "webhook_secret": settings.webhook_secret,
        }
        return TelegramAdapter(config)

    @staticmethod
    def _create_lark_adapter(settings: Settings) -> LarkAdapter:
        """Create Lark/Feishu adapter."""
        config = {
            "app_id": settings.lark_app_id_str,
            "app_secret": settings.lark_app_secret_str,
            "encrypt_key": settings.lark_encrypt_key_str,
            "verification_token": settings.lark_verification_token_str,
            "webhook_url": settings.lark_webhook_url,
        }
        return LarkAdapter(config)
