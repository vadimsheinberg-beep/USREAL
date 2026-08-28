"""Оценка лота по сделкам с соседними участками.

Откуда берутся сделки. Портал рм"и хранит не только действующие торги, но и
их результаты: у закрытого тендера есть ``SchumZchiya`` — сумма, которую
заплатил победитель. Это настоящие сделки с землёй, с гуш/хелка, площадью и
датой. Собранные в базу командой ``landtender harvest``, они и служат
сравнимыми: земля сравнивается с землёй, а не с квартирами.

Как считается. Регрессия по логарифму цены за квадратный метр:

    log(цена/м²) = b0 + b1·log(площадь) + b2·плотность + b3·сельхоз

Логарифм цены — потому что цена за метр падает с ростом участка не линейно, а
примерно степенно: гектар поля стоит за метр много меньше, чем сотка под дом.

Цены старых сделок приводятся к сегодняшним деньгам индексом рынка жилья
(``macro.py``). Без этого сделка 2019 года тянула бы оценку вниз просто
потому, что она старая.

Чего эта оценка не делает. Она не заменяет шамая. Выборка мала, признаков
мало, разброс цен на землю огромен. Поэтому вместе с числом всегда
показывается, на скольких сделках оно построено и насколько модель вообще
объясняет разброс, — а при плохих цифрах оценка не показывается совсем.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date

from .landuse import AGRICULTURE
from .macro import IndexPoint, index_factor
from .models import Lot
from .regression import Fit, fit, median

log = logging.getLogger(__name__)

#: Меньше этого числа сделок регрессию строить не на чем.
MIN_COMPARABLES = 8

#: Сделки старше этого срока в расчёт не идут даже с поправкой на индекс:
#: за десять лет меняется не только цена, но и сам рынок.
MAX_AGE_YEARS = 10

#: Насколько модель должна объяснять разброс, чтобы её показывать.
MIN_R_SQUARED = 0.2


@dataclass(frozen=True)
class Comparable:
    """Состоявшаяся сделка, приведённая к сегодняшним деньгам."""

    source_id: str
    settlement: str | None
    area_sqm: float
    price_nis: float
    #: Цена за м² после поправки на индекс.
    price_per_sqm: float
    units: int | None
    land_use: str | None
    when: str | None
    #: Множитель индексации; 1.0 означает «поправку применить не удалось».
    index_factor: float = 1.0


@dataclass(frozen=True)
class Valuation:
    """Оценка лота с признанием её точности."""

    price_nis: float
    price_per_sqm: float
    #: Сколько сделок участвовало.
    n: int
    #: Доля объяснённой дисперсии; ``None`` — оценка по медиане.
    r_squared: float | None
    #: Нижняя и верхняя границы: одно стандартное отклонение остатков.
    low_nis: float | None = None
    high_nis: float | None = None
    #: ``regression`` или ``median`` — чем именно посчитано.
    method: str = "regression"

    @property
    def spread_ratio(self) -> float | None:
        """Во сколько раз верхняя граница выше нижней."""
        if not (self.low_nis and self.high_nis):
            return None
        return self.high_nis / self.low_nis


def collect_comparables(
    rows: list[Lot],
    housing_index: list[IndexPoint] | None = None,
    today: date | None = None,
) -> list[Comparable]:
    """Отбирает состоявшиеся сделки, годные для сравнения.

    Годной считается запись с ценой сделки (не минимальной ценой — это
    разные вещи), площадью и датой. Минимальная цена показывает, чего хотело
    ведомство, а не сколько рынок заплатил.
    """
    today = today or date.today()
    out: list[Comparable] = []

    for lot in rows:
        if lot.price_kind != "final" or not lot.price_nis or not lot.area_sqm:
            continue
        if lot.area_sqm <= 0 or lot.price_nis <= 0:
            continue
        when = lot.closing_date or lot.published_date
        if not _within_age(when, today):
            continue

        factor = index_factor(housing_index or [], when) or 1.0
        adjusted = lot.price_nis * factor
        out.append(
            Comparable(
                source_id=lot.source_id,
                settlement=lot.settlement,
                area_sqm=lot.area_sqm,
                price_nis=adjusted,
                price_per_sqm=adjusted / lot.area_sqm,
                units=lot.units,
                land_use=lot.land_use,
                when=when,
                index_factor=factor,
            )
        )
    return out


def explain_rejections(rows: list[Lot], today: date | None = None) -> dict[str, int]:
    """Куда девались сделки: по одной причине отсева на запись.

    Из сотен закрытых тендеров годными оказываются единицы, и важно знать
    почему: нет цены сделки, нет площади или запись слишком старая. Гадать
    об этом бессмысленно — счётчики отвечают точно, и по ним видно, что
    чинить: догружать детали, добирать площадь из кадастра или ничего.
    """
    today = today or date.today()
    counts = {
        "всего": 0,
        "нет цены сделки": 0,
        "нет площади": 0,
        "цена или площадь нулевые": 0,
        "старше десяти лет": 0,
        "годных": 0,
    }

    for lot in rows:
        counts["всего"] += 1
        if lot.price_kind != "final" or not lot.price_nis:
            counts["нет цены сделки"] += 1
            continue
        if not lot.area_sqm:
            counts["нет площади"] += 1
            continue
        if lot.area_sqm <= 0 or lot.price_nis <= 0:
            counts["цена или площадь нулевые"] += 1
            continue
        if not _within_age(lot.closing_date or lot.published_date, today):
            counts["старше десяти лет"] += 1
            continue
        counts["годных"] += 1

    return counts


def nearby(comparables: list[Comparable], lot: Lot) -> list[Comparable]:
    """Сделки, сравнимые с этим лотом.

    Близость определяется населённым пунктом: расстояние в метрах здесь
    обманчиво, потому что цена земли меняется на границе муниципалитета
    скачком, а не плавно. Если сделок по городу мало, берём тот же вид
    назначения по всей выборке — лучше широкая база, чем оценка по трём
    точкам.
    """
    # Сделка не может быть сравнимой сама себе. Закрытый тендер попадает и в
    # выборку, и на оценку: без этого он объяснял бы собственную цену.
    pool = [c for c in comparables if c.source_id != lot.source_id]

    if lot.settlement:
        same_city = [c for c in pool if c.settlement == lot.settlement]
        if len(same_city) >= MIN_COMPARABLES:
            return same_city

    same_use = [c for c in pool if c.land_use == lot.land_use]
    return same_use if len(same_use) >= MIN_COMPARABLES else pool


def estimate(lot: Lot, comparables: list[Comparable]) -> Valuation | None:
    """Оценка стоимости лота. ``None``, если считать не на чем.

    Отказ вернуть число — тоже результат: пустая оценка честнее уверенной
    цифры, построенной на четырёх старых сделках.
    """
    if not lot.area_sqm or lot.area_sqm <= 0:
        return None

    pool = nearby(comparables, lot)
    if len(pool) < MIN_COMPARABLES:
        return None

    rows = [_features(c.area_sqm, c.units, c.land_use) for c in pool]
    # Признак, одинаковый у всех сделок, ничего не объясняет и делает систему
    # вырожденной: если сельхоза в выборке нет вовсе, столбец из одних нулей
    # уронил бы регрессию целиком, а не просто оказался бесполезным.
    keep = _informative(rows)
    if not keep:
        return _median_valuation(lot, pool)

    model = fit(
        [[row[j] for j in keep] for row in rows],
        [math.log(c.price_per_sqm) for c in pool],
    )

    if model is not None and model.usable and model.r_squared >= MIN_R_SQUARED:
        return _from_regression(lot, model, len(pool), keep)

    return _median_valuation(lot, pool)


def _median_valuation(lot: Lot, pool: list[Comparable]) -> Valuation | None:
    """Медиана по тем же сделкам: грубее, но не притворяется моделью."""
    per_sqm = median([c.price_per_sqm for c in pool])
    if per_sqm is None or not lot.area_sqm:
        return None
    return Valuation(
        price_nis=per_sqm * lot.area_sqm,
        price_per_sqm=per_sqm,
        n=len(pool),
        r_squared=None,
        method="median",
    )


def _informative(rows: list[list[float]]) -> list[int]:
    """Номера признаков, которые в выборке действительно меняются."""
    if not rows:
        return []
    keep = []
    for column in range(len(rows[0])):
        values = [row[column] for row in rows]
        if max(values) - min(values) > 1e-9:
            keep.append(column)
    return keep


def _from_regression(lot: Lot, model: Fit, n: int, keep: list[int]) -> Valuation | None:
    full = _features(lot.area_sqm, lot.units, lot.land_use)
    predicted = model.predict([full[j] for j in keep])
    if predicted is None:
        return None

    per_sqm = math.exp(predicted)
    # Границы считаем в логарифмах и возвращаем в шекели: в логарифмической
    # модели интервал симметричен там, а не в деньгах.
    low = math.exp(predicted - model.residual_std) * lot.area_sqm
    high = math.exp(predicted + model.residual_std) * lot.area_sqm

    return Valuation(
        price_nis=per_sqm * lot.area_sqm,
        price_per_sqm=per_sqm,
        n=n,
        r_squared=model.r_squared,
        low_nis=low,
        high_nis=high,
        method="regression",
    )


def _features(area_sqm: float | None, units: int | None, land_use: str | None) -> list[float]:
    """Признаки одной строки. Порядок обязан совпадать между обучением и прогнозом."""
    area = max(float(area_sqm or 1.0), 1.0)
    # Плотность на дунам, а не абсолютное число единиц: участок вдвое больше
    # с вдвое большим числом квартир — это тот же продукт, а не другой.
    density = (units or 0) / (area / 1000)
    return [math.log(area), density, 1.0 if land_use == AGRICULTURE else 0.0]


def _within_age(when: str | None, today: date) -> bool:
    if not when:
        return False
    try:
        year = int(when[:4])
    except (ValueError, TypeError):
        return False
    return 0 <= today.year - year <= MAX_AGE_YEARS
