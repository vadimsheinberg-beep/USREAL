"""Оценка лота по сделкам с соседними участками."""

import math

import pytest

from landtender.landuse import AGRICULTURE
from landtender.macro import IndexPoint
from landtender.models import Lot
from landtender.regression import fit, median
from landtender.valuation import (
    MIN_COMPARABLES,
    Comparable,
    collect_comparables,
    estimate,
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
        comp = collect_comparables([deal(price=1_000_000.0)])[0]
        assert comp.price_nis == 1_000_000.0
        assert comp.index_factor == 1.0


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

    def test_falls_back_to_the_same_land_use(self):
        """Лучше широкая база, чем оценка по трём точкам."""
        comps = pool(3, city="עכו") + pool(12, city="חיפה")
        chosen = nearby(comps, deal(city="עכו"))
        assert len(chosen) >= MIN_COMPARABLES


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
