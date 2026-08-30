"""Пересчёт операций в валюту отчёта.

Курсы берутся из конфига (``[fx].rates``) и считаются фиксированными:
для личной аналитики этого достаточно, а внешний источник курсов добавил
бы сетевой запрос и ещё один способ сломаться. Операции в валюте отчёта
пересчёта не требуют.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

from .models import Transaction

log = logging.getLogger(__name__)


def convert(
    transactions: Iterable[Transaction], currency: str, rates: dict[str, float]
) -> list[Transaction]:
    """Проставляет ``base_amount`` для операций в чужой валюте.

    Курс — сколько единиц валюты отчёта дают за одну единицу исходной.
    Если курса нет, сумма остаётся как есть, и об этом пишется в лог:
    молча смешивать шекели с долларами в одной сумме нельзя.
    """
    target = currency.upper()
    missing: set[str] = set()
    result: list[Transaction] = []

    for tx in transactions:
        if tx.currency == target:
            tx.base_amount = None
        else:
            rate = rates.get(tx.currency)
            if rate:
                tx.base_amount = tx.amount * rate
            else:
                tx.base_amount = None
                missing.add(tx.currency)
        result.append(tx)

    if missing:
        log.warning(
            "нет курса к %s для валют: %s — эти суммы попадут в отчёт без пересчёта. "
            "Добавьте их в [fx].rates",
            target,
            ", ".join(sorted(missing)),
        )
    return result


def currencies(transactions: Sequence[Transaction]) -> dict[str, int]:
    """Какие валюты встречаются в данных и сколько операций в каждой."""
    counts: dict[str, int] = {}
    for tx in transactions:
        counts[tx.currency] = counts.get(tx.currency, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
