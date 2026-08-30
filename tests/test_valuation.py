"""Оценка лота по сделкам с соседними участками."""

import math
from datetime import date

import pytest

from landtender.landuse import AGRICULTURE
from landtender.macro import IndexPoint
from landtender.models import Lot
from landtender.regression import fit, median
from landtender.valuation import (
    MAX_AGE_YEARS,
    MAX_SPREAD_RATIO,
    MIN_CREDIBLE_AREA_SQM,
    MIN_COMPARABLES,
    Comparable,
    age_histogram,
    collect_comparables,
    explain_estimates,
    estimate,
    explain_rejections,
    nearby,
)


def deal(source_id="1", area=1000.0, price=1_000_000.0, city="חיפה", when="2025-06-01", **kw):
    data = dict(
        source="rmi_michrazim",
        source_id=source_id,
        settlement=city,
        area_sqm=area,
        price_nis=price,
        price_kind="final",
        closing_date=when,
    )
    data.update(kw)
    return Lot(**data)


class TestRegressionSolver:
    def test_recovers_a_known_line(self):
        model = fit([[x] for x in range(10)], [2 + 3 * x for x in range(10)])
        assert model.coefficients == pytest.approx([2.0, 3.0])
        assert model.r_squared == pytest.approx(1.0)

    def test_two_predictors(self):
        rows = [[x, x * x] for x in range(12)]
        targets = [1 + 2 * x - 0.1 * x * x for x in range(12)]
        model = fit(rows, targets)
        assert model.coefficients == pytest.approx([1.0, 2.0, -0.1])

    def test_constant_predictor_is_degenerate(self):
        """Столбец без разброса делает систему неразрешимой."""
        assert fit([[1], [1], [1], [1], [1], [1]], [1, 2, 3, 4, 5, 6]) is None

    def test_too_few_rows(self):
        assert fit([[1], [2]], [1, 2]) is None

    def test_mismatched_lengths(self):
        assert fit([[1], [2]], [1]) is None

    def test_usable_demands_headroom(self):
        """Подгонка по трём точкам с тремя признаками идеальна и бессмысленна."""
        rows = [[x, x + 1, x * 2] for x in range(5)]
        model = fit(rows, [x * 3.0 for x in range(5)])
        assert model is None or not model.usable

    def test_median(self):
        assert median([3, 1, 2]) == 2
        assert median([4, 1, 3, 2]) == 2.5
        assert median([]) is None


class TestCollectComparables:
    def test_only_closed_deals_count(self):
        """Минимальная цена — это запрос ведомства, а не то, что заплатил рынок."""
        rows = [deal(), deal("2", price_kind="min"), deal("3", price_kind="appraisal")]
        assert len(collect_comparables(rows)) == 1

    def test_price_and_area_are_required(self):
        rows = [deal("1", price=None), deal("2", area=None), deal("3")]
        assert len(collect_comparables(rows)) == 1

    def test_price_per_sqm(self):
        comp = collect_comparables([deal(area=500.0, price=2_000_000.0)])[0]
        assert comp.price_per_sqm == 4000.0

    def test_old_deals_are_dropped(self):
        assert collect_comparables([deal(when="2005-01-01")]) == []

    def test_index_adjusts_old_prices(self):
        points = [
            IndexPoint(code="40010", name=None, year=2026, month=1, value=120.0),
            IndexPoint(code="40010", name=None, year=2022, month=1, value=100.0),
        ]
        comp = collect_comparables([deal(when="2022-01-15", price=1_000_000.0)], points)[0]
        assert comp.price_nis == pytest.approx(1_200_000.0)
        assert comp.index_factor == pytest.approx(1.2)

    def test_without_index_the_price_is_left_alone(self):
        """Без ряда индекса цена номинальная, и множитель пуст, а не равен единице."""
        comp = collect_comparables([deal(price=1_000_000.0)])[0]
        assert comp.price_nis == 1_000_000.0
        assert comp.index_factor is None

    def test_deal_outside_the_index_is_dropped(self):
        """Ряд подключён, но месяца в нём нет — сделка уходит, а не идёт по номиналу.

        Множитель 1.0 здесь означал бы «цены с 2011 года не менялись»: на
        двадцатилетнем окне такая сделка занижала бы оценку вдвое.
        """
        points = [
            IndexPoint(code="40010", name=None, year=2026, month=1, value=120.0),
            IndexPoint(code="40010", name=None, year=2022, month=1, value=100.0),
        ]
        assert collect_comparables([deal(when="2011-05-01")], points) == []

    def test_age_is_recorded(self):
        comp = collect_comparables([deal(when="2019-06-01")], today=date(2026, 8, 28))[0]
        assert comp.years_ago == 7.0

    def test_window_is_configurable(self):
        rows = [deal(when="2012-01-01")]
        assert collect_comparables(rows, today=date(2026, 8, 28)) != []
        assert collect_comparables(rows, today=date(2026, 8, 28), max_age_years=10) == []


def pool(n=20, city="חיפה", **kw):
    """Выборка с реалистичной зависимостью: цена за метр падает с площадью."""
    rows = []
    for i in range(n):
        area = 400.0 + i * 300
        per_sqm = 9000 * area ** -0.25
        rows.append(deal(str(i), area=area, price=per_sqm * area, city=city, **kw))
    return collect_comparables(rows)


class TestNearby:
    def test_same_city_is_preferred(self):
        comps = pool(12, city="חיפה") + pool(12, city="נתניה")
        target = deal(city="נתניה")
        assert all(c.settlement == "נתניה" for c in nearby(comps, target))

    def test_other_cities_never_leak_in(self):
        """Мало сделок по городу — не повод сравнивать с другим городом.

        Прежде выборка расширялась до всех сделок того же назначения по
        стране. На настоящей базе это и дало бессмыслицу: участок сравнивался
        с участком в другом конце страны, модель объясняла разброс на треть,
        интервал оценки выходил в полсотни раз, и относительно такого числа
        любая цена выглядела выгодной. Отказ от оценки честнее.
        """
        comps = pool(3, city="עכו") + pool(12, city="חיפה")
        chosen = nearby(comps, deal("не из выборки", city="עכו"))
        assert len(chosen) == 3
        assert all(c.settlement == "עכו" for c in chosen)

    def test_a_lot_without_a_city_gets_nothing(self):
        """Без города сравнивать не с чем — вся страна сравнимой не является."""
        assert nearby(pool(20), deal(city=None)) == []


class TestEstimate:
    def test_regression_on_a_clean_sample(self):
        value = estimate(deal(area=1000.0), pool(30))
        assert value.method == "regression"
        assert value.r_squared > 0.9
        assert value.low_nis < value.price_nis < value.high_nis

    def test_price_scales_with_area(self):
        comps = pool(30)
        small = estimate(deal(area=500.0), comps)
        large = estimate(deal(area=5000.0), comps)
        assert large.price_nis > small.price_nis
        # …а цена за метр наоборот падает
        assert large.price_per_sqm < small.price_per_sqm

    def test_too_few_comparables_gives_nothing(self):
        """Отказ вернуть число честнее уверенной цифры на четырёх сделках."""
        assert estimate(deal(), pool(4)) is None

    def test_lot_without_area_cannot_be_valued(self):
        assert estimate(deal(area=None), pool(30)) is None

    def test_noise_falls_back_to_median(self):
        import random

        random.seed(3)
        rows = [
            deal(str(i), area=1000.0, price=random.uniform(200_000, 5_000_000))
            for i in range(20)
        ]
        value = estimate(deal(area=1000.0), collect_comparables(rows))
        assert value.method == "median"
        assert value.r_squared is None

    def test_agricultural_flag_does_not_break_a_uniform_sample(self):
        """Признак, одинаковый у всех, раньше ронял регрессию целиком."""
        comps = pool(30, land_use=AGRICULTURE)
        value = estimate(deal(area=1000.0, land_use=AGRICULTURE), comps)
        assert value is not None and value.method == "regression"

    def test_interval_is_reported_with_the_estimate(self):
        # Идентификатор вне выборки: свои сделки из неё исключаются.
        value = estimate(deal("новый", area=1000.0), pool(30))
        assert value.spread_ratio > 1.0
        assert value.n == 30


class TestSelfExclusion:
    """Сделка не может объяснять собственную цену."""

    def test_own_deal_is_dropped_from_the_pool(self):
        comps = pool(20)
        target = deal(comps[0].source_id, area=comps[0].area_sqm)
        assert all(c.source_id != target.source_id for c in nearby(comps, target))

    def test_pool_shrinks_by_exactly_one(self):
        comps = pool(20)
        target = deal(comps[3].source_id)
        assert len(nearby(comps, target)) == len(comps) - 1

    def test_other_lots_are_untouched(self):
        comps = pool(20)
        assert len(nearby(comps, deal("не из выборки"))) == len(comps)


class TestExplainRejections:
    """Счётчики отсева: почему из сотен сделок годными оказываются единицы."""

    def test_counts_add_up(self):
        rows = [
            deal("годная"),
            deal("минимум", price_kind="min"),
            deal("без площади", area=None),
            deal("старая", when="2005-01-01"),
        ]
        counts = explain_rejections(rows)
        assert counts["всего"] == 4
        assert counts["годных"] == 1
        assert counts["нет цены сделки"] == 1
        assert counts["нет площади"] == 1
        assert counts[f"старше {MAX_AGE_YEARS} лет"] == 1

    def test_each_row_is_counted_once(self):
        rows = [deal(str(i)) for i in range(5)]
        counts = explain_rejections(rows)
        reasons = sum(v for k, v in counts.items() if k != "всего")
        assert reasons == counts["всего"]

    def test_agrees_with_collect(self):
        rows = [deal("1"), deal("2", price_kind="min"), deal("3", area=None)]
        assert explain_rejections(rows)["годных"] == len(collect_comparables(rows))


class TestAgeHistogram:
    """Гистограмма по годам: за какие годы архив вообще есть.

    Разбор причин отвечает, сколько сделок потеряно по возрасту; гистограмма
    показывает, куда двигать окно, чтобы их вернуть.
    """

    def test_counts_by_year_newest_first(self):
        rows = [
            deal("1", when="2025-06-01"),
            deal("2", when="2025-11-01"),
            deal("3", when="2008-01-01"),
        ]
        assert age_histogram(rows) == {2025: 2, 2008: 1}

    def test_ignores_rows_without_a_deal_price(self):
        rows = [deal("1", price_kind="min"), deal("2", area=None), deal("3")]
        assert age_histogram(rows) == {2025: 1}


class TestPlaceholderArea:
    """Портал вместо площади иногда отдаёт «1 м²» — это не участок.

    У поля в 14 гектаров (тендер 21/2020) площадь пришла как 1 м². Цена за
    метр по такой записи получается фантастической, и в обе стороны: как
    сравнимая сделка она отравляет выборку, как объект оценки — получает
    оценку в сотни миллионов.
    """

    def test_such_a_deal_is_not_comparable(self):
        assert collect_comparables([deal(area=1.0)]) == []

    def test_the_boundary_is_the_threshold(self):
        assert collect_comparables([deal(area=MIN_CREDIBLE_AREA_SQM)]) != []
        assert collect_comparables([deal(area=MIN_CREDIBLE_AREA_SQM - 0.1)]) == []

    def test_such_a_lot_is_not_valued(self):
        assert estimate(deal("цель", area=1.0), pool(30)) is None

    def test_the_rejection_is_counted_separately(self):
        counts = explain_rejections([deal(area=1.0)])
        assert counts["площадь-заглушка или нулевая цена"] == 1
        assert counts["годных"] == 0


class TestSpreadGate:
    """Интервал в полсотни раз — это не осторожная оценка, а её отсутствие.

    Первый прогон топа выдал «оценка 175 млн ₪ (23 млн — 1.3 млрд)». Хуже
    самого числа то, что относительно него любая цена оказывается «на 88%
    ниже оценки», и рейтинг заполняется мнимыми находками.
    """

    def noisy(self, n=40):
        """Выборка без всякой зависимости цены от площади: разброс огромен."""
        rows = []
        for i in range(n):
            area = 400.0 + i * 300
            # Цена за метр скачет на два порядка и площадью не объясняется.
            per_sqm = 100.0 * (1000 ** ((i % 7) / 6.0))
            rows.append(deal(str(i), area=area, price=per_sqm * area))
        return collect_comparables(rows)

    def test_a_wide_regression_is_not_shown_as_one(self):
        value = estimate(deal("цель", area=5000.0), self.noisy())
        assert value is None or value.method == "median" or not _too_wide(value)

    def test_a_tight_regression_survives(self):
        value = estimate(deal("цель", area=1000.0), pool(30))
        assert value.method == "regression"
        assert value.spread_ratio <= MAX_SPREAD_RATIO

    def test_the_ratio_itself_is_reported(self):
        value = estimate(deal("цель", area=1000.0), pool(30))
        assert value.spread_ratio == pytest.approx(value.high_nis / value.low_nis)


def _too_wide(value):
    return value.spread_ratio is not None and value.spread_ratio > MAX_SPREAD_RATIO


class TestExplainEstimates:
    """Почему оценки нет: счётчики вместо догадок.

    После ужесточения правил оценку не получил ни один лот, и топ выродился
    в сортировку по сроку подачи. Гипотез было три — нет города, мало сделок
    по городу, слишком широкий интервал, — и разбор отвечает, какая верна.
    """

    def test_placeholder_area_is_named(self):
        counts = explain_estimates([deal("цель", area=1.0)], pool(30))
        assert counts["площадь-заглушка"] == 1

    def test_missing_city_is_named(self):
        counts = explain_estimates([deal("цель", area=1000.0, city=None)], pool(30))
        assert counts["город не указан"] == 1

    def test_thin_city_is_named(self):
        """Сделки есть, но не в этом городе."""
        counts = explain_estimates(
            [deal("цель", area=1000.0, city="דימונה")], pool(30, city="חיפה")
        )
        assert counts["мало сделок по городу"] == 1

    def test_a_valued_lot_is_counted_as_such(self):
        counts = explain_estimates([deal("цель", area=1000.0)], pool(30))
        assert counts["оценено"] == 1

    def test_each_lot_is_counted_once(self):
        lots = [deal(str(i), area=1000.0) for i in range(4)]
        counts = explain_estimates(lots, pool(30))
        assert sum(v for k, v in counts.items() if k != "всего") == counts["всего"]


class TestPlaceByCode:
    """Сравнение по коду населённого пункта, а не по названию.

    Разбор на настоящей базе дал однозначный ответ: из 817 действующих лотов
    название города не было известно ни у одного, и требование «тот же город»
    отсекало поголовно всех. Портал при этом присылает код ЦСБ почти всегда.
    Коду безразличны написания, которых у израильских городов по десятку.
    """

    def coded(self, source_id, code, **kw):
        return deal(source_id, city=None, settlement_code=code, **kw)

    def test_same_code_is_the_same_place(self):
        comps = collect_comparables([self.coded(str(i), 4000) for i in range(5)])
        chosen = nearby(comps, self.coded("цель", 4000))
        assert len(chosen) == 5

    def test_different_codes_never_mix(self):
        comps = collect_comparables(
            [self.coded(str(i), 4000) for i in range(5)]
            + [self.coded(f"д{i}", 9000) for i in range(5)]
        )
        chosen = nearby(comps, self.coded("цель", 9000))
        assert len(chosen) == 5
        assert all(c.settlement_code == 9000 for c in chosen)

    def test_the_code_wins_over_the_name(self):
        """У одного города бывает десяток написаний; код — один."""
        comps = collect_comparables([
            deal("1", city="חיפה", settlement_code=4000),
            deal("2", city="Хайфа", settlement_code=4000),
        ])
        chosen = nearby(comps, deal("цель", city="Haifa", settlement_code=4000))
        assert len(chosen) == 2

    def test_the_name_still_works_without_a_code(self):
        comps = collect_comparables([deal(str(i), city="עכו") for i in range(4)])
        assert len(nearby(comps, deal("цель", city="עכו"))) == 4

    def test_neither_code_nor_name_means_no_comparables(self):
        comps = collect_comparables([self.coded(str(i), 4000) for i in range(5)])
        assert nearby(comps, deal("цель", city=None)) == []
