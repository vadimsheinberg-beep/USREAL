"""Отправка дневной сводки в Telegram.

Ограничения, которые здесь учтены:
  * одно сообщение — не длиннее 4096 символов;
  * при 429 Telegram сам говорит, сколько ждать (``retry_after``);
  * блоки не режутся посередине строки — иначе ломается HTML-разметка.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Sequence

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"
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

    # ------------------------------------------------------- диагностика ---

    def get_me(self) -> dict:
        """Проверяет токен и возвращает описание бота."""
        return self._call("getMe")

    def get_chat(self) -> dict:
        """Проверяет, что бот видит канал и знает его название."""
        return self._call("getChat", {"chat_id": self.chat_id})

    def discover_chats(self) -> list[dict]:
        """Список чатов из необработанных апдейтов — помогает найти ``chat_id``.

        Работает, только пока боту не назначен вебхук и апдейты не вычитаны.
        Для канала надёжнее добавить бота администратором и написать в канал.
        """
        updates = self._call("getUpdates", {"limit": 100})
        chats: dict[str, dict] = {}
        for update in updates if isinstance(updates, list) else []:
            for key in ("message", "channel_post", "edited_channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat")
                if chat and str(chat.get("id")) not in chats:
                    chats[str(chat["id"])] = {
                        "chat_id": chat.get("id"),
                        "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
                        "type": chat.get("type"),
                    }
        return list(chats.values())

    def _call(self, method: str, payload: dict | None = None) -> dict | list:
        url = API_BASE.format(token=self.bot_token, method=method)
        try:
            response = requests.post(url, json=payload or {}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise TelegramError(f"Сеть недоступна: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: ответ не JSON (HTTP {response.status_code})") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or response.text[:200]}")
        return data.get("result")

    # ----------------------------------------------------------- отправка --

    def send_document(self, path: Path, caption: str | None = None) -> None:
        """Отправляет файл (например, CSV с новыми лотами) в тот же чат."""
        url = API_BASE.format(token=self.bot_token, method="sendDocument")
        data = {"chat_id": self.chat_id}
        if caption:
            data["caption"] = caption[:1024]
        try:
            with path.open("rb") as fh:
                response = requests.post(
                    url, data=data, files={"document": (path.name, fh)}, timeout=self.timeout
                )
        except OSError as exc:
            raise TelegramError(f"Не удалось прочитать {path}: {exc}") from exc
        except requests.RequestException as exc:
            raise TelegramError(f"Сеть недоступна: {exc}") from exc
        if response.status_code >= 400:
            raise TelegramError(f"sendDocument HTTP {response.status_code}: {response.text[:300]}")

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
