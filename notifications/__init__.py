"""Package for outbound alerts (Telegram, etc.)."""

from .telegram import TelegramNotifier

__all__ = ["TelegramNotifier"]
