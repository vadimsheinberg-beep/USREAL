"""Демонстрационные данные — чтобы посмотреть отчёт до подключения API.

Набор синтетический, но по структуре похож на реальную выписку:
регулярные подписки и аренда, частые мелкие покупки, редкие крупные
траты и ежемесячная зарплата.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from .models import DIRECTION_INCOME, Transaction

#: (описание, минимум, максимум, сколько раз в месяц)
_FREQUENT = [
    ("SHUFERSAL DEAL 4821", 90, 420, 6),
    ("RAMI LEVY TLV", 120, 500, 3),
    ("AROMA ESPRESSO BAR", 18, 65, 8),
    ("WOLT ORDER", 55, 180, 4),
    ("PAZ YELLOW 118", 180, 380, 2),
    ("RAV KAV RECHARGE", 50, 150, 2),
    ("SUPER PHARM DIZENGOFF", 35, 240, 2),
]

#: Ежемесячные списания с почти неизменной суммой.
_RECURRING = [
    ("SHKAR DIRA - שכר דירה", 6200.0),
    ("CELLCOM MOBILE", 89.0),
    ("HOT INTERNET", 129.0),
    ("NETFLIX.COM", 54.9),
    ("SPOTIFY AB", 21.9),
    ("OPENAI CHATGPT SUBSCR", 74.0),
    ("MACCABI HEALTH", 210.0),
    ("HAREL INSURANCE", 340.0),
    ("CHEVRAT HASHMAL - חברת חשמל", 380.0),
]

#: Редкие крупные траты — попадают в топ отчёта.
_OCCASIONAL = [
    ("IKEA NETANYA", 400, 2600),
    ("AMAZON.COM", 120, 900),
    ("EL AL ISRAEL AIRLINES", 900, 3800),
    ("BOOKING.COM", 400, 2200),
    ("ZARA DIZENGOFF CENTER", 150, 700),
    ("YES PLANET RISHON", 60, 220),
    ("Перевод другу", 200, 1500),
]


def generate(months: int = 6, seed: int = 42, end: date | None = None) -> list[Transaction]:
    """Собирает выписку за ``months`` месяцев. При одном ``seed`` — одна и та же."""
    rng = random.Random(seed)
    end = end or date.today()
    transactions: list[Transaction] = []

    #: Отсчитываем месяцы арифметикой по номеру месяца: вычитание дней
    #: промахивается мимо коротких месяцев и сдвигает период на единицу.
    year, month = end.year, end.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1

    for _ in range(months):
        days_in_month = 28
        month_start = date(year, month, 1)

        for description, low, high, times in _FREQUENT:
            for _ in range(rng.randint(max(1, times - 2), times + 2)):
                transactions.append(
                    Transaction(
                        date=month_start + timedelta(days=rng.randint(0, days_in_month - 1)),
                        amount=round(rng.uniform(low, high), 2),
                        description=description,
                        source="demo",
                    )
                )

        for description, amount in _RECURRING:
            #: Небольшой разброс — так же, как в жизни: индексация, округления.
            jitter = amount * rng.uniform(-0.03, 0.03)
            transactions.append(
                Transaction(
                    date=month_start + timedelta(days=rng.randint(1, 5)),
                    amount=round(amount + jitter, 2),
                    description=description,
                    source="demo",
                )
            )

        for description, low, high in _OCCASIONAL:
            if rng.random() < 0.45:
                transactions.append(
                    Transaction(
                        date=month_start + timedelta(days=rng.randint(0, days_in_month - 1)),
                        amount=round(rng.uniform(low, high), 2),
                        description=description,
                        source="demo",
                    )
                )

        transactions.append(
            Transaction(
                date=month_start + timedelta(days=9),
                amount=round(rng.uniform(21000, 23000), 2),
                description="משכורת / SALARY",
                direction=DIRECTION_INCOME,
                source="demo",
            )
        )

        year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    #: Текущий месяц ещё не кончился — будущих операций в выписке не бывает.
    return sorted((tx for tx in transactions if tx.date <= end), key=lambda t: t.date)
