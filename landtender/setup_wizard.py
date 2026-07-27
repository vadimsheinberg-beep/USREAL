"""Мастер первичной настройки: токен → канал → файл ``.env`` → проверка.

Отдельный модуль, потому что логика диалоговая: ввод пользователя вынесен в
параметр ``ask``, поэтому мастер полностью тестируется без терминала.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

from .notify import TelegramError, TelegramNotifier

Ask = Callable[[str], str]
Say = Callable[[str], None]

ENV_TEMPLATE = """# Создано `landtender setup`. Файл содержит секреты и в git не попадает.
TELEGRAM_BOT_TOKEN={token}
TELEGRAM_CHAT_ID={chat_id}
"""


def write_env_file(path: Path, token: str, chat_id: str) -> None:
    """Пишет ``.env`` и закрывает права: секрет не должен читаться всеми."""
    path.write_text(ENV_TEMPLATE.format(token=token, chat_id=chat_id), encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass  # на файловых системах без POSIX-прав это не критично


def _ask_token(ask: Ask, say: Say) -> str | None:
    """Спрашивает токен, пока getMe его не подтвердит."""
    existing = os.environ.get("TELEGRAM_BOT_TOKEN")
    prompt = "Токен бота от @BotFather"
    if existing:
        prompt += " [Enter — оставить найденный в окружении]"

    for attempt in range(3):
        answer = ask(f"{prompt}: ").strip() or (existing if attempt == 0 else "")
        if not answer:
            say("Токен не введён.")
            continue
        try:
            bot = TelegramNotifier(bot_token=answer, chat_id="0").get_me()
        except TelegramError as exc:
            say(f"  ✗ Токен не принят: {exc}")
            continue
        say(f"  ✓ Бот @{bot.get('username')} ({bot.get('first_name')})")
        return answer
    return None


def _ask_chat(ask: Ask, say: Say, token: str) -> str | None:
    """Спрашивает канал: публичное имя со знаком @ либо числовой id."""
    say(
        "\nКуда слать сводку. Бот должен быть администратором канала.\n"
        "  • публичный канал — введите @имя_канала\n"
        "  • приватный канал — введите ? , чтобы найти id по входящим сообщениям"
    )
    existing = os.environ.get("TELEGRAM_CHAT_ID")

    for _ in range(3):
        answer = (ask("Канал: ").strip() or existing or "")
        if answer == "?":
            answer = _discover(ask, say, token) or ""
        if not answer:
            say("  Значение не введено.")
            continue
        try:
            chat = TelegramNotifier(bot_token=token, chat_id=answer).get_chat()
        except TelegramError as exc:
            say(f"  ✗ Канал недоступен: {exc}")
            say("    Проверьте, что бот добавлен в канал администратором.")
            continue
        say(f"  ✓ Канал: {chat.get('title') or chat.get('username')} (id {chat.get('id')})")
        return answer
    return None


def _discover(ask: Ask, say: Say, token: str) -> str | None:
    say("\n  Напишите в канал любое сообщение и нажмите Enter.")
    ask("  Готово? ")
    try:
        chats = TelegramNotifier(bot_token=token, chat_id="0").discover_chats()
    except TelegramError as exc:
        say(f"  ✗ Не удалось получить обновления: {exc}")
        return None
    if not chats:
        say("  Ничего не найдено. Убедитесь, что бот — администратор канала.")
        return None
    say("  Найдено:")
    for chat in chats:
        say(f"    {chat['chat_id']:<16} {chat['type']:<10} {chat['title'] or ''}")
    return ask("  Введите нужный chat_id: ").strip() or None


def run_wizard(
    env_path: Path,
    ask: Ask | None = None,
    say: Say | None = None,
    send_demo: bool = True,
    threshold_usd: float = 1_000_000.0,
) -> int:
    """Проводит настройку от начала до сообщения в канале. Возвращает код возврата."""
    # Разрешаем ввод/вывод в момент вызова, а не импорта: так их можно подменить.
    ask = ask or input
    say = say or print

    try:
        return _wizard_steps(env_path, ask, say, send_demo, threshold_usd)
    except (EOFError, KeyboardInterrupt):
        # Ctrl+C или конец ввода при запуске из скрипта — выходим без трейсбека.
        say("\nНастройка прервана, ничего не записано.")
        return 1


def _wizard_steps(
    env_path: Path, ask: Ask, say: Say, send_demo: bool, threshold_usd: float
) -> int:
    say("Настройка Telegram-канала для landtender.\n")

    if env_path.exists():
        answer = ask(f"Файл {env_path} уже есть. Перезаписать? [y/N] ").strip().lower()
        if answer not in {"y", "yes", "д", "да"}:
            say("Отменено, файл не тронут.")
            return 1

    token = _ask_token(ask, say)
    if not token:
        say("\nНастройка прервана: токен не подтверждён.")
        return 1

    chat_id = _ask_chat(ask, say, token)
    if not chat_id:
        say("\nНастройка прервана: канал не подтверждён.")
        return 1

    write_env_file(env_path, token, chat_id)
    say(f"\n✓ Настройки записаны в {env_path} (права 600)")

    if send_demo:
        from .demo import demo_blocks

        try:
            notifier = TelegramNotifier(bot_token=token, chat_id=chat_id)
            notifier.send_blocks(demo_blocks(threshold_usd))
            say("✓ В канал отправлена демонстрационная сводка — проверьте, как она выглядит")
        except TelegramError as exc:
            say(f"✗ Демонстрацию отправить не удалось: {exc}")
            return 1

    say("\nГотово. Дальше:\n  landtender run --no-notify   # наполнить базу молча\n  landtender run               # ежедневный запуск")
    return 0
