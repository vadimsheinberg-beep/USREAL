"""Отправка дневной сводки в Telegram.

Ограничения, которые здесь учтены:
  * одно сообщение — не длиннее 4096 символов;
  * при 429 Telegram сам говорит, сколько ждать (``retry_after``);
  * блоки не режутся посередине строки — иначе ломается HTML-разметка.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LEN = 4096
#: Запас на случай, если Telegram считает длину чуть иначе.
SAFE_LEN = 3900


class TelegramError(RuntimeError):
    """Не удалось доставить сообщение."""


def chunk_blocks(blocks: Sequence[str], limit: int = SAFE_LEN) -> list[str]:
    """Склеивает блоки в сообщения, не превышающие лимит.

    Слишком длинный блок режется по строкам, а не по символам, чтобы не
    порвать HTML-теги пополам.
    """
    messages: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            messages.append(current.rstrip())
        current = ""

    for block in blocks:
        if len(block) > limit:
            flush()
            buffer = ""
            for line in block.split("\n"):
                line = line[:limit]  # аварийный предохранитель для гигантской строки
                if len(buffer) + len(line) + 1 > limit:
                    messages.append(buffer.rstrip())
                    buffer = ""
                buffer += line + "\n"
            if buffer.strip():
                messages.append(buffer.rstrip())
            continue

        if len(current) + len(block) + 2 > limit:
            flush()
        current += block + "\n\n"

    flush()
    return messages


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout: int = 30,
        disable_web_page_preview: bool = True,
    ) -> None:
        if not bot_token or not chat_id:
            raise TelegramError("Не заданы bot_token или chat_id (см. TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.disable_web_page_preview = disable_web_page_preview

    def send_blocks(self, blocks: Sequence[str]) -> int:
        """Отправляет сводку. Возвращает число доставленных сообщений."""
        messages = chunk_blocks(blocks)
        sent = 0
        for message in messages:
            self._send(message)
            sent += 1
            time.sleep(0.5)  # Telegram ограничивает частоту в один чат
        return sent

    def _send(self, text: str, attempt: int = 0) -> None:
        url = API_URL.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": self.disable_web_page_preview,
        }
        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise TelegramError(f"Сеть недоступна: {exc}") from exc

        if response.status_code == 429 and attempt < 3:
            retry_after = 5
            try:
                retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
            except ValueError:
                pass
            log.warning("Telegram: лимит частоты, жду %s с", retry_after)
            time.sleep(retry_after + 1)
            return self._send(text, attempt + 1)

        if response.status_code >= 400:
            raise TelegramError(f"HTTP {response.status_code}: {response.text[:300]}")
