"""Отправка месячного отчёта в Telegram.

Сообщением уходит короткая сводка, а подробности — файлом: помесячная
таблица не влезает в 4096 символов и в мессенджере всё равно нечитаема.
Клиент здесь свой, а не из соседнего пакета: ``expenses`` должен
оставаться самостоятельным, а нужно от Telegram всего два метода.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Sequence

import requests

from .analyze import category_trends, find_recurring, monthly_summary, overall_stats
from .models import Transaction
from .report import money

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
#: Лимит Telegram — 4096 символов; берём с запасом.
SAFE_LEN = 3900

_MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


class TelegramError(RuntimeError):
    """Не удалось доставить отчёт."""


def month_name(month: str) -> str:
    """``2026-08`` → ``август 2026``."""
    year, _, number = month.partition("-")
    try:
        return f"{_MONTHS[int(number) - 1]} {year}"
    except (ValueError, IndexError):
        return month


def build_summary(
    transactions: Sequence[Transaction], currency: str = "ILS", top: int = 6
) -> str:
    """Короткая сводка для сообщения. HTML-разметка Telegram, без таблиц."""
    if not transactions:
        return "За период не нашлось ни одной операции."

    summary = monthly_summary(transactions)
    totals = overall_stats(summary)
    trends = category_trends(summary)
    last = summary[-1]

    lines = [
        f"<b>Расходы за {month_name(last.month)}</b>",
        f"Потрачено: <b>{money(last.expense, currency)}</b> за {last.count} операций",
    ]

    if len(summary) >= 2:
        previous = summary[-2]
        delta = last.expense - previous.expense
        if previous.expense:
            percent = delta / previous.expense * 100
            arrow = "▲" if delta > 0 else "▼"
            #: Названия месяцев подставляются как есть, поэтому фраза
            #: построена без падежей: «К июль 2026» звучало бы криво.
            lines.append(
                f"{month_name(previous.month)} → {month_name(last.month)}: "
                f"{arrow} {money(abs(delta), currency)} ({percent:+.0f}%)"
            )
    lines.append(f"Средний месяц за период: {money(totals['expense_avg'], currency)}")
    lines.append("")

    lines.append("<b>Куда ушло</b>")
    month_total = last.expense or 1.0
    for category, amount in last.top_categories(top):
        share = amount / month_total * 100
        lines.append(f"· {category} — {money(amount, currency)} ({share:.0f}%)")

    if len(last.by_category) > top:
        rest = sum(a for _, a in last.top_categories(1000)[top:])
        lines.append(f"· остальное — {money(rest, currency)}")

    #: Что выросло сильнее всего — обычно единственное, ради чего отчёт и читают.
    if len(summary) >= 2:
        previous = summary[-2]
        movers = [
            (c, last.by_category.get(c, 0.0) - previous.by_category.get(c, 0.0))
            for c in set(last.by_category) | set(previous.by_category)
        ]
        #: Порог в 2% месячного расхода: иначе в сводку лезет округление
        #: страховки на пару шекелей и вытесняет то, ради чего её читают.
        floor = month_total * 0.02
        grown = [(c, d) for c, d in sorted(movers, key=lambda kv: -kv[1])[:3] if d > floor]
        if grown:
            lines.append("")
            lines.append("<b>Выросло к прошлому месяцу</b>")
            for category, delta in grown:
                lines.append(f"· {category} +{money(delta, currency)}")

    recurring = find_recurring(transactions)
    if recurring:
        yearly = sum(item.yearly_estimate for item in recurring)
        lines.append("")
        lines.append(
            f"Регулярных списаний: {len(recurring)} — {money(yearly, currency)} в год"
        )

    unknown = sum(
        tx.report_amount for tx in transactions if tx.category_rule is None and tx.is_expense
    )
    if unknown:
        lines.append(f"Без категории: {money(unknown, currency)}")

    text = "\n".join(lines)
    if len(text) > SAFE_LEN:
        text = text[: SAFE_LEN - 1] + "…"
    return text


class TelegramNotifier:
    """Ровно то, что нужно для месячного отчёта: сообщение и вложение."""

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 30) -> None:
        if not bot_token or not chat_id:
            raise TelegramError(
                "не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID — положите их в .env"
            )
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.timeout = timeout

    def _url(self, method: str) -> str:
        return API_URL.format(token=self.bot_token, method=method)

    def _post(self, method: str, *, data: dict, files: dict | None = None, attempt: int = 0) -> None:
        try:
            response = requests.post(
                self._url(method), data=data, files=files, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise TelegramError(f"запрос к Telegram не прошёл: {exc}") from exc

        if response.status_code == 429 and attempt < 3:
            #: Telegram сам сообщает, сколько ждать.
            wait = int(response.json().get("parameters", {}).get("retry_after", 5))
            log.warning("Telegram просит подождать %d с", wait)
            time.sleep(wait + 1)
            #: Файл уже прочитан до конца — перечитывать его умеет только вызывающий.
            if files:
                raise TelegramError("Telegram ограничил частоту при отправке файла, повторите позже")
            return self._post(method, data=data, attempt=attempt + 1)

        if response.status_code >= 400:
            raise TelegramError(
                f"Telegram ответил {response.status_code}: {response.text[:200]}"
            )

    def send_message(self, text: str) -> None:
        self._post(
            "sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def send_document(self, path: Path, caption: str | None = None) -> None:
        path = Path(path)
        if not path.exists():
            raise TelegramError(f"файл не найден: {path}")
        with path.open("rb") as fh:
            self._post(
                "sendDocument",
                data={"chat_id": self.chat_id, "caption": caption or ""},
                files={"document": (path.name, fh)},
            )

    def check(self) -> str:
        """Проверка доступа: кто бот и виден ли чат."""
        try:
            me = requests.get(self._url("getMe"), timeout=self.timeout).json()
            chat = requests.get(
                self._url("getChat"), params={"chat_id": self.chat_id}, timeout=self.timeout
            ).json()
        except requests.RequestException as exc:
            raise TelegramError(f"запрос к Telegram не прошёл: {exc}") from exc

        if not me.get("ok"):
            raise TelegramError(f"токен не принят: {me.get('description')}")
        if not chat.get("ok"):
            raise TelegramError(
                f"чат {self.chat_id} недоступен: {chat.get('description')}. "
                "Бот должен быть добавлен в канал администратором."
            )
        bot = me["result"].get("username")
        title = chat["result"].get("title") or chat["result"].get("username")
        return f"бот @{bot}, чат «{title}»"
