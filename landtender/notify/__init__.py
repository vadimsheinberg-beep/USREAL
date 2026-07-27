"""Каналы доставки сводки."""

from .telegram import TelegramNotifier, TelegramError

__all__ = ["TelegramNotifier", "TelegramError"]
