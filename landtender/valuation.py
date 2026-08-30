"""Оценка лота по сделкам с соседними участками.

Откуда берутся сделки. Портал рм"и хранит не только действующие торги, но и
их результаты: у закрытого тендера есть ``SchumZchiya`` — сумма, которую
заплатил победитель. Это настоящие сделки с землёй, с гуш/хелка, площадью и
датой. Собранные в базу командой ``landtender harvest``, они и служат
сравнимыми: земля сравнивается с землёй, а не с квартирами.

Как считается. Регрессия по логарифму цены за квадратный метр:

    log(цена/м²) = b0 + b1·log(площадь) + b2·плотность + b3·сельхоз
                 + b4·возраст сделки

Логарифм цены — потому что цена за метр падает с ростом участка не линейно, а
примерно степенно: гектар поля стоит за метр много меньше, чем сотка под дом.

Цены старых сделок приводятся к сегодняшним деньгам индексом рынка жилья
(``macro.py``). Без этого сделка 2019 года тянула бы оценку вниз просто
потому, что она старая. Индекс отвечает за квартиры, а не за землю, поэтому
возраст сделки входит в регрессию отдельным признаком: чего индекс не
объяснил, объяснит коэффициент при нём.

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

#: Сделки старше этого срока в расчёт не идут. Порог был десять лет, и на
#: реальном архиве рм"и это оказалось почти запретом: из 2383 состоявшихся
#: сделок с ценой и площадью в окно попали десять, остальные 2373 отсеялись
#: по возрасту. Архив закрытых торгов по природе своей старый, и выбрасывать
#: его — значит остаться без базы сравнения вовсе. Двадцать лет держатся на
#: двух подпорках: цена приводится к сегодняшним деньгам индексом рынка жилья,
#: а возраст сделки входит в регрессию отдельным признаком, так что остаток
#: временного сноса модель забирает себе, а не выдаёт за свойство участка.
MAX_AGE_YEARS = 20

#: Насколько модель должна объяснять разброс, чтобы её показывать.
MIN_R_SQUARED = 0.2

#: Во сколько раз верхняя граница может превышать нижнюю. Первый прогон топа
#: на настоящей базе выдал «оценка 175 млн ₪ (23 млн — 1.3 млрд)»: интервал в
#: пятьдесят раз — это не оценка, а сообщение «где-то между сараем и заводом».
#: Хуже того, относительно такого числа любая цена оказывается «на 88% ниже
#: оценки», и весь рейтинг заполняется мнимыми находками. Оценка шире этого
#: порога не показывается вовсе.
MAX_SPREAD_RATIO = 4.0

#: Площадь меньше этой портал сообщает вместо настоящей: у поля в 14 гектаров
#: он отдаёт «1 м²». Такой участок нельзя ни оценить, ни взять в сравнимые —
#: цена за метр получается фантастической в обе стороны.
MIN_CREDIBLE_AREA_SQM = 10.0


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
    #: Множитель индексации; ``None`` — ряд индекса не подключён и цена
    #: осталась номинальной. Единица здесь означала бы «инфляции не было».
    index_factor: float | None = None
    #: Сколько лет назад состоялась сделка. Признак регрессии, а не справка.
    years_ago: float = 0.0


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
    max_age_years: int = MAX_AGE_YEARS,
) -> list[Comparable]:
    """Отбирает состоявшиеся сделки, годные для сравнения.

    Годной считается запись с ценой сделки (не минимальной ценой — это
    разные вещи), площадью и датой. Минимальная цена показывает, чего хотело
    ведомство, а не сколько рынок заплатил.

    Если ряд индекса подключён, но нужного месяца в нём нет, сделка
    отбрасывается. Раньше в этом случае подставлялся множитель 1.0, и сделка
    2007 года входила в выборку по номиналу — то есть заведомо заниженной.
    Пока окно было десятилетним, ошибка стоила процентов; на двадцати годах
    она удваивала бы цену.
    """
    today = today or date.today()
    out: list[Comparable] = []
    indexed = bool(housing_index)

    for lot in rows:
        if lot.price_kind != "final" or not lot.price_nis or not lot.area_sqm:
            continue
        if lot.area_sqm < MIN_CREDIBLE_AREA_SQM or lot.price_nis <= 0:
            continue
        when = lot.closing_date or lot.published_date
        age = _age_years(when, today)
        if age is None or not 0 <= age <= max_age_years:
            continue

        factor = index_factor(housing_index or [], when) if indexed else None
        if indexed and factor is None:
            continue

        adjusted = lot.price_nis * (factor or 1.0)
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
                years_ago=float(age),
            )
        )
    return out


def explain_rejections(
    rows: list[Lot],
    today: date | None = None,
    max_age_years: int = MAX_AGE_YEARS,
) -> dict[str, int]:
    """Куда девались сделки: по одной причине отсева на запись.

    Из тысяч закрытых тендеров годными оказываются немногие, и важно знать
    почему: нет цены сделки, нет площади или запись слишком старая. Гадать
    об этом бессмысленно — счётчики отвечают точно, и по ним видно, что
    чинить: догружать детали, добирать площадь из кадастра или расширять окно.
    Именно этот разбор и показал, что окно в десять лет отсекало 2373 сделки
    из 2383.
    """
    today = today or date.today()
    counts = {
        "всего": 0,
        "нет цены сделки": 0,
        "нет площади": 0,
        "площадь-заглушка или нулевая цена": 0,
        "нет даты": 0,
        f"старше {max_age_years} лет": 0,
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
        if lot.area_sqm < MIN_CREDIBLE_AREA_SQM or lot.price_nis <= 0:
            counts["площадь-заглушка или нулевая цена"] += 1
            continue
        age = _age_years(lot.closing_date or lot.published_date, today)
        if age is None:
            counts["нет даты"] += 1
            continue
        if not 0 <= age <= max_age_years:
            counts[f"старше {max_age_years} лет"] += 1
            continue
        counts["годных"] += 1

    return counts


def age_histogram(rows: list[Lot], today: date | None = None) -> dict[int, int]:
    """Сколько состоявшихся сделок какого года — по годам, свежие первыми.

    Разбор причин отвечает «сколько потеряно по возрасту», гистограмма — «за
    какие годы вообще есть архив». Без неё выбор окна остаётся гаданием.
    """
    today = today or date.today()
    counts: dict[int, int] = {}
    for lot in rows:
        if lot.price_kind != "final" or not lot.price_nis or not lot.area_sqm:
            continue
        if lot.area_sqm <= 0 or lot.price_nis <= 0:
            continue
        year = _year_of(lot.closing_date or lot.published_date)
        if year is None:
            continue
        counts[year] = counts.get(year, 0) + 1
    return dict(sorted(counts.items(), reverse=True))


def explain_estimates(
    lots: list[Lot], comparables: list[Comparable]
) -> dict[str, int]:
    """Почему лот остался без оценки — по одной причине на лот.

    Оценка отвечает за главный показатель рейтинга: цену против рынка. Когда
    её нет ни у кого, топ вырождается в сортировку по сроку подачи. Причин
    ровно четыре, и гадать между ними бессмысленно — счётчики отвечают точно.
    """
    counts = {
        "всего": 0,
        "площадь-заглушка": 0,
        "город не указан": 0,
        "мало сделок по городу": 0,
        "интервал шире порога": 0,
        "оценено": 0,
    }

    for lot in lots:
        counts["всего"] += 1
        if not lot.area_sqm or lot.area_sqm < MIN_CREDIBLE_AREA_SQM:
            counts["площадь-заглушка"] += 1
            continue
        if not lot.settlement:
            counts["город не указан"] += 1
            continue
        pool = nearby(comparables, lot)
        if len(pool) < MIN_COMPARABLES:
            counts["мало сделок по городу"] += 1
            continue
        value = estimate(lot, comparables)
        if value is None or _too_wide(value):
            counts["интервал шире порога"] += 1
            continue
        counts["оценено"] += 1

    return counts


def nearby(comparables: list[Comparable], lot: Lot) -> list[Comparable]:
    """Сделки, сравнимые с этим лотом: тот же населённый пункт, и только он.

    Раньше при нехватке сделок по городу выборка расширялась до всех сделок
    того же назначения по стране. На настоящей базе это и оказалось главным
    источником бессмыслицы: участок сравнивался с участком в другом конце
    страны, модель объясняла разброс на треть, а интервал оценки выходил в
    полсотни раз. Цена земли меняется на границе муниципалитета скачком, и
    «широкая выборка» здесь не компромисс, а подмена предмета.

    Лучше отказаться от оценки, чем выдать оценку по чужому городу.
    """
    if not lot.settlement:
        return []
    # Сделка не может быть сравнимой сама себе. Закрытый тендер попадает и в
    # выборку, и на оценку: без этого он объяснял бы собственную цену.
    return [
        c for c in comparables
        if c.settlement == lot.settlement and c.source_id != lot.source_id
    ]


def estimate(lot: Lot, comparables: list[Comparable]) -> Valuation | None:
    """Оценка стоимости лота. ``None``, если считать не на чем.

    Отказ вернуть число — тоже результат: пустая оценка честнее уверенной
    цифры, построенной на четырёх старых сделках.
    """
    if not lot.area_sqm or lot.area_sqm < MIN_CREDIBLE_AREA_SQM:
        return None

    pool = nearby(comparables, lot)
    if len(pool) < MIN_COMPARABLES:
        return None

    rows = [_features(c.area_sqm, c.units, c.land_use, c.years_ago) for c in pool]
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
        value = _from_regression(lot, model, len(pool), keep)
        # Слишком широкий интервал — не осторожная оценка, а её отсутствие:
        # относительно такого числа любая цена выглядит выгодной.
        if value is not None and _too_wide(value):
            return _median_valuation(lot, pool)
        return value

    return _median_valuation(lot, pool)


def _too_wide(value: Valuation) -> bool:
    ratio = value.spread_ratio
    return ratio is not None and ratio > MAX_SPREAD_RATIO


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
    # Оценка делается на сегодня, поэтому возраст сделки для прогноза — ноль:
    # цены сравнимых уже приведены к сегодняшним деньгам, а остаток временного
    # сноса сидит в коэффициенте при этом признаке.
    full = _features(lot.area_sqm, lot.units, lot.land_use, 0.0)
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


def _features(
    area_sqm: float | None,
    units: int | None,
    land_use: str | None,
    years_ago: float = 0.0,
) -> list[float]:
    """Признаки одной строки. Порядок обязан совпадать между обучением и прогнозом."""
    area = max(float(area_sqm or 1.0), 1.0)
    # Плотность на дунам, а не абсолютное число единиц: участок вдвое больше
    # с вдвое большим числом квартир — это тот же продукт, а не другой.
    density = (units or 0) / (area / 1000)
    return [
        math.log(area),
        density,
        1.0 if land_use == AGRICULTURE else 0.0,
        # Возраст сделки. Индекс рынка жилья приводит цену к сегодняшним
        # деньгам, но земля дорожала не так же, как квартиры; этот признак
        # забирает разницу себе, вместо того чтобы приписать её участку.
        float(years_ago),
    ]


def _year_of(when: str | None) -> int | None:
    if not when:
        return None
    try:
        return int(str(when)[:4])
    except (ValueError, TypeError):
        return None


def _age_years(when: str | None, today: date) -> int | None:
    """Возраст сделки в годах; ``None`` — даты нет или она не читается."""
    year = _year_of(when)
    return None if year is None else today.year - year
