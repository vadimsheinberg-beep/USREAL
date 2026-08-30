"""Помесячная аналитика: сводки, тренды по категориям, регулярные списания."""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from .models import Transaction


@dataclass
class MonthStats:
    """Итоги одного месяца."""

    month: str
    expense: float = 0.0
    income: float = 0.0
    count: int = 0
    #: Категория → сумма расходов за месяц.
    by_category: dict[str, float] = field(default_factory=dict)
    #: Мерчант → сумма расходов за месяц.
    by_merchant: dict[str, float] = field(default_factory=dict)

    @property
    def net(self) -> float:
        """Сколько осталось: поступления минус расходы."""
        return self.income - self.expense

    @property
    def avg_expense(self) -> float:
        return self.expense / self.count if self.count else 0.0

    def top_categories(self, limit: int = 10) -> list[tuple[str, float]]:
        return sorted(self.by_category.items(), key=lambda kv: -kv[1])[:limit]

    def top_merchants(self, limit: int = 10) -> list[tuple[str, float]]:
        return sorted(self.by_merchant.items(), key=lambda kv: -kv[1])[:limit]


@dataclass
class RecurringCharge:
    """Похоже на подписку или регулярный платёж."""

    merchant: str
    category: str
    #: Медианная сумма списания — устойчивее среднего к разовым скачкам.
    typical_amount: float
    months: list[str]
    total: float

    @property
    def months_count(self) -> int:
        return len(self.months)

    @property
    def yearly_estimate(self) -> float:
        """Во что обойдётся за год, если платить столько же каждый месяц."""
        return self.typical_amount * 12


@dataclass
class CategoryTrend:
    """Как менялась категория от месяца к месяцу."""

    category: str
    #: Месяц → сумма. Месяцы без трат в периоде присутствуют с нулём.
    by_month: dict[str, float]
    total: float
    average: float

    @property
    def last_delta(self) -> float | None:
        """Разница между последним и предпоследним месяцем периода."""
        values = list(self.by_month.values())
        if len(values) < 2:
            return None
        return values[-1] - values[-2]


def month_range(start: date, end: date) -> list[str]:
    """Все месяцы между двумя датами включительно, без пропусков."""
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def filter_period(
    transactions: Iterable[Transaction],
    *,
    since: date | None = None,
    until: date | None = None,
    months: int | None = None,
) -> list[Transaction]:
    """Отбирает операции за период.

    ``months`` — «последние N месяцев», считая от самой свежей операции в
    данных, а не от сегодняшней даты: выписку часто выгружают задним числом.
    """
    items = sorted(transactions, key=lambda t: t.date)
    if not items:
        return []
    if months and not since:
        newest = items[-1].date
        year, month = newest.year, newest.month
        back = months - 1
        month -= back % 12
        year -= back // 12
        if month <= 0:
            month += 12
            year -= 1
        since = date(year, month, 1)
    if since:
        items = [t for t in items if t.date >= since]
    if until:
        items = [t for t in items if t.date <= until]
    return items


def monthly_summary(transactions: Iterable[Transaction]) -> list[MonthStats]:
    """Считает итоги по каждому месяцу, от старого к новому."""
    buckets: dict[str, MonthStats] = {}
    for tx in transactions:
        stats = buckets.setdefault(tx.month, MonthStats(month=tx.month))
        amount = tx.report_amount
        if tx.is_expense:
            stats.expense += amount
            stats.count += 1
            stats.by_category[tx.category] = stats.by_category.get(tx.category, 0.0) + amount
            merchant = tx.merchant or tx.description or "—"
            stats.by_merchant[merchant] = stats.by_merchant.get(merchant, 0.0) + amount
        else:
            stats.income += amount
    return [buckets[m] for m in sorted(buckets)]


def category_trends(summary: Sequence[MonthStats]) -> list[CategoryTrend]:
    """Разворачивает помесячные итоги в тренды по категориям.

    Категория, которой в каком-то месяце не было, получает там ноль —
    иначе график и дельты врут.
    """
    months = [s.month for s in summary]
    totals: dict[str, dict[str, float]] = defaultdict(dict)
    for stats in summary:
        for category, amount in stats.by_category.items():
            totals[category][stats.month] = amount

    trends: list[CategoryTrend] = []
    for category, by_month in totals.items():
        filled = {m: by_month.get(m, 0.0) for m in months}
        total = sum(filled.values())
        trends.append(
            CategoryTrend(
                category=category,
                by_month=filled,
                total=total,
                average=total / len(months) if months else 0.0,
            )
        )
    return sorted(trends, key=lambda t: -t.total)


def find_recurring(
    transactions: Iterable[Transaction],
    *,
    min_months: int = 3,
    tolerance: float = 0.25,
) -> list[RecurringCharge]:
    """Ищет списания, которые повторяются из месяца в месяц.

    Критерий простой и намеренно консервативный: один и тот же мерчант в
    ``min_months`` разных месяцах, и разброс месячных сумм вокруг медианы
    не больше ``tolerance``. Считаем именно месячные суммы, а не отдельные
    операции: у мерчанта может быть два списания в месяц, и «в месяц»
    должно означать месяц. Продукты в такой фильтр не попадают (суммы
    скачут), а подписки, аренда и страховки — попадают.
    """
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for tx in transactions:
        if not tx.is_expense:
            continue
        key = tx.merchant or tx.description
        if key:
            groups[key].append(tx)

    found: list[RecurringCharge] = []
    for merchant, items in groups.items():
        per_month: dict[str, float] = defaultdict(float)
        for tx in items:
            per_month[tx.month] += tx.report_amount
        months = sorted(per_month)
        if len(months) < min_months:
            continue
        monthly = [per_month[m] for m in months]
        median = statistics.median(monthly)
        if median <= 0:
            continue
        spread = max(abs(a - median) / median for a in monthly)
        if spread > tolerance:
            continue
        found.append(
            RecurringCharge(
                merchant=merchant,
                category=items[-1].category,
                typical_amount=median,
                months=months,
                total=sum(monthly),
            )
        )
    return sorted(found, key=lambda r: -r.typical_amount)


def top_expenses(transactions: Iterable[Transaction], limit: int = 10) -> list[Transaction]:
    """Самые крупные разовые траты периода."""
    expenses = [tx for tx in transactions if tx.is_expense]
    return sorted(expenses, key=lambda t: -t.report_amount)[:limit]


def compare_months(previous: MonthStats, current: MonthStats) -> list[tuple[str, float, float]]:
    """Сравнивает два месяца по категориям.

    Возвращает ``(категория, было, стало)``, отсортированное по модулю
    изменения — сверху то, что сильнее всего повлияло на итог.
    """
    categories = set(previous.by_category) | set(current.by_category)
    rows = [
        (c, previous.by_category.get(c, 0.0), current.by_category.get(c, 0.0))
        for c in categories
    ]
    return sorted(rows, key=lambda row: -abs(row[2] - row[1]))


def overall_stats(summary: Sequence[MonthStats]) -> dict[str, float]:
    """Средние по всему периоду — для шапки отчёта."""
    if not summary:
        return {"months": 0, "expense_total": 0.0, "expense_avg": 0.0, "income_total": 0.0}
    expense_total = sum(s.expense for s in summary)
    income_total = sum(s.income for s in summary)
    return {
        "months": len(summary),
        "expense_total": expense_total,
        "expense_avg": expense_total / len(summary),
        "income_total": income_total,
        "income_avg": income_total / len(summary),
        "net_total": income_total - expense_total,
    }
