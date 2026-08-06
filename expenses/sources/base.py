"""Приведение произвольной записи из источника к :class:`Transaction`.

Каждый банк и каждое приложение называет поля по-своему. Вместо того чтобы
писать отдельный парсер под каждый формат, описываем соответствие полей
(:class:`FieldMap`) — обычно это несколько строк в конфиге, и новый
источник подключается без правки кода.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from ..models import DIRECTION_EXPENSE, DIRECTION_INCOME, Transaction

log = logging.getLogger(__name__)

#: Форматы дат, которые встречаются в выписках чаще всего.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%d/%m/%y",
    "%d.%m.%y",
)

_NUMBER_JUNK = re.compile(r"[^\d,.\-+]")


class SourceError(RuntimeError):
    """Источник отдал данные, которые не удалось разобрать."""


def parse_date(value: Any) -> date:
    """Разбирает дату в любом из ходовых представлений.

    Понимает ISO, ``dd/mm/yyyy`` и родню, а также unix-время в секундах и
    миллисекундах — последнее любят отдавать мобильные приложения.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        seconds = float(value)
        #: 10^11 секунд — это 5138 год, значит пришли миллисекунды.
        if seconds > 1e11:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date()

    text = str(value or "").strip()
    if not text:
        raise SourceError("пустая дата")
    if text.isdigit() and len(text) >= 10:
        return parse_date(int(text))

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    raise SourceError(f"не разобрал дату: {value!r}")


def parse_amount(value: Any) -> float:
    """Разбирает сумму: ``1,234.56``, ``1 234,56``, ``₪-45.00``, ``(45.00)``."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        raise SourceError("пустая сумма")
    negative = text.startswith("(") and text.endswith(")")
    text = _NUMBER_JUNK.sub("", text)
    if not text or text in {"-", "+"}:
        raise SourceError(f"не разобрал сумму: {value!r}")
    #: Если есть и запятая, и точка — запятая это разделитель тысяч.
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        amount = float(text)
    except ValueError as exc:
        raise SourceError(f"не разобрал сумму: {value!r}") from exc
    return -amount if negative else amount


def pick(record: dict[str, Any], path: str | Sequence[str] | None) -> Any:
    """Достаёт значение по имени поля, точечному пути или списку кандидатов.

    ``"amount"``, ``"payment.sum"`` и ``["sum", "amount"]`` — всё валидно.
    Список пробуется по порядку до первого непустого значения.
    """
    if not path:
        return None
    if isinstance(path, (list, tuple)):
        for candidate in path:
            value = pick(record, candidate)
            if value not in (None, ""):
                return value
        return None

    current: Any = record
    for part in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


@dataclass
class FieldMap:
    """Как поля источника ложатся на :class:`Transaction`.

    Значение каждого поля — имя, точечный путь или список кандидатов.
    Незаданные поля просто не заполняются.
    """

    date: str | Sequence[str] = "date"
    amount: str | Sequence[str] = "amount"
    description: str | Sequence[str] = "description"

    currency: str | Sequence[str] | None = "currency"
    source_id: str | Sequence[str] | None = "id"
    merchant: str | Sequence[str] | None = None
    category: str | Sequence[str] | None = None
    account: str | Sequence[str] | None = None

    #: Поле с направлением операции, если источник его отдаёт отдельно.
    direction: str | Sequence[str] | None = None
    #: Значения этого поля, означающие расход.
    expense_values: Sequence[str] = ("debit", "expense", "out", "withdrawal", "charge", "-")
    #: Значения, означающие поступление.
    income_values: Sequence[str] = ("credit", "income", "in", "deposit", "refund", "+")

    #: Если направления нет: минус в сумме = расход. Иногда наоборот.
    negative_is_expense: bool = True
    #: Валюта, когда источник её не передаёт.
    default_currency: str = "ILS"
    #: Куда смотреть за списком операций внутри ответа API.
    records_path: str | Sequence[str] | None = None

    #: Класть ли исходную запись в ``Transaction.extra``.
    keep_raw: bool = False

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "FieldMap":
        if not raw:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            log.warning("неизвестные ключи в маппинге полей: %s", ", ".join(sorted(unknown)))
        return cls(**{k: v for k, v in raw.items() if k in known})


def _direction_from(record: dict[str, Any], mapping: FieldMap, amount: float) -> str:
    raw = pick(record, mapping.direction)
    if raw is not None:
        token = str(raw).strip().lower()
        if token in {str(v).lower() for v in mapping.expense_values}:
            return DIRECTION_EXPENSE
        if token in {str(v).lower() for v in mapping.income_values}:
            return DIRECTION_INCOME
        log.debug("непонятное направление операции: %r, определяю по знаку", raw)
    if amount == 0:
        return DIRECTION_EXPENSE
    negative = amount < 0
    return DIRECTION_EXPENSE if negative == mapping.negative_is_expense else DIRECTION_INCOME


def normalize_record(
    record: dict[str, Any], mapping: FieldMap, source: str
) -> Transaction:
    """Превращает одну запись источника в транзакцию."""
    amount = parse_amount(pick(record, mapping.amount))
    description = pick(record, mapping.description)
    currency = pick(record, mapping.currency) or mapping.default_currency
    source_id = pick(record, mapping.source_id)

    return Transaction(
        date=parse_date(pick(record, mapping.date)),
        amount=amount,
        description=str(description or "").strip() or "без описания",
        currency=str(currency),
        direction=_direction_from(record, mapping, amount),
        source=source,
        source_id=str(source_id) if source_id not in (None, "") else None,
        merchant=_opt_str(pick(record, mapping.merchant)),
        source_category=_opt_str(pick(record, mapping.category)),
        account=_opt_str(pick(record, mapping.account)),
        extra=dict(record) if mapping.keep_raw else {},
    )


def _opt_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def normalize_records(
    records: Iterable[dict[str, Any]],
    mapping: FieldMap,
    source: str,
    *,
    strict: bool = False,
) -> list[Transaction]:
    """Нормализует пачку записей.

    По умолчанию битая строка не роняет импорт: она пропускается с
    предупреждением. ``strict=True`` — для отладки нового маппинга.
    """
    result: list[Transaction] = []
    skipped = 0
    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue
        try:
            result.append(normalize_record(record, mapping, source))
        except (SourceError, TypeError, ValueError) as exc:
            if strict:
                raise
            skipped += 1
            log.warning("пропущена запись (%s): %s", exc, _preview(record))
    if skipped:
        log.warning("пропущено записей: %d из %d", skipped, skipped + len(result))
    return result


def _preview(record: dict[str, Any], limit: int = 120) -> str:
    text = str(record)
    return text if len(text) <= limit else text[:limit] + "…"


def extract_records(payload: Any, path: str | Sequence[str] | None) -> list[dict[str, Any]]:
    """Достаёт список операций из ответа API.

    Если путь не задан, ищет первый список словарей: у большинства API он
    лежит под ``data``, ``items``, ``result`` или прямо в корне.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        raise SourceError(f"ожидал список или объект, получил {type(payload).__name__}")

    if path:
        found = pick(payload, path)
        if isinstance(found, list):
            return [r for r in found if isinstance(r, dict)]
        raise SourceError(f"по пути {path!r} списка операций нет")

    for key in ("data", "items", "transactions", "result", "results", "records", "list"):
        found = payload.get(key)
        if isinstance(found, list) and found and isinstance(found[0], dict):
            return found
        #: Часто список лежит на уровень глубже: ``{"data": {"items": [...]}}``.
        if isinstance(found, dict):
            for nested_key in ("items", "transactions", "list", "records"):
                nested = found.get(nested_key)
                if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                    return nested
    raise SourceError(
        "не нашёл список операций в ответе — укажите records_path в настройках источника"
    )
