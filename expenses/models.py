"""Нормализованная модель транзакции.

Любой источник (Babit, выгрузка банка в CSV, JSON из приложения) приводит
свои записи к одному виду — :class:`Transaction`. Дальше вся логика
(категоризация, помесячная сводка, отчёт) работает только с этой моделью
и ничего не знает про формат исходных данных.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

#: Транзакция, где деньги ушли со счёта.
DIRECTION_EXPENSE = "expense"
#: Поступление: зарплата, возврат, входящий перевод.
DIRECTION_INCOME = "income"

#: Категория для всего, что не подошло ни под одно правило.
CATEGORY_UNKNOWN = "Прочее"


@dataclass
class Transaction:
    """Одна операция по счёту.

    ``amount`` всегда положительный — это модуль суммы. Знак операции
    живёт в ``direction``: так не приходится гадать, какую полярность
    выбрал очередной источник (одни отдают расход минусом, другие плюсом).
    """

    #: Дата операции. Для отчёта важна именно она, не дата проводки.
    date: date
    #: Модуль суммы в валюте ``currency``.
    amount: float
    #: Что видно в выписке: мерчант, назначение платежа.
    description: str

    currency: str = "ILS"
    direction: str = DIRECTION_EXPENSE

    #: Машинное имя источника: ``babit``, ``csv``, ``json``.
    source: str = ""
    #: Идентификатор операции в источнике, если он есть.
    source_id: str | None = None

    #: Нормализованное имя мерчанта — по нему ищем повторяющиеся списания.
    merchant: str | None = None
    #: Категория, присвоенная источником (если он её отдаёт).
    source_category: str | None = None
    #: Счёт/карта, откуда списано.
    account: str | None = None

    #: Категория после работы :mod:`expenses.categories`.
    category: str = CATEGORY_UNKNOWN
    #: Имя сработавшего правила — чтобы можно было объяснить решение.
    category_rule: str | None = None

    #: Сумма в базовой валюте отчёта, если исходная валюта другая.
    base_amount: float | None = None

    #: Всё, что источник отдал сверх схемы, — на случай отладки.
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.date, datetime):
            self.date = self.date.date()
        if isinstance(self.date, str):
            self.date = date.fromisoformat(self.date)
        self.amount = abs(float(self.amount))
        self.description = (self.description or "").strip()
        if self.currency:
            self.currency = self.currency.strip().upper()

    @property
    def is_expense(self) -> bool:
        return self.direction == DIRECTION_EXPENSE

    @property
    def month(self) -> str:
        """Месяц операции в виде ``2026-08`` — ключ группировки в отчётах."""
        return f"{self.date.year:04d}-{self.date.month:02d}"

    @property
    def report_amount(self) -> float:
        """Сумма в валюте отчёта: пересчитанная, если пересчёт был."""
        return self.base_amount if self.base_amount is not None else self.amount

    @property
    def key(self) -> str:
        """Ключ дедупликации.

        Если источник дал свой id — доверяем ему. Иначе склеиваем дату,
        сумму и описание: повторный импорт той же выписки не должен
        удваивать расходы.
        """
        if self.source_id:
            raw = f"{self.source}|{self.source_id}"
        else:
            raw = f"{self.source}|{self.date.isoformat()}|{self.amount:.2f}|{self.description.lower()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["date"] = self.date.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in data.items() if k in known}
        payload["date"] = date.fromisoformat(str(payload["date"]))
        return cls(**payload)
