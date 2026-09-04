"""Запас прочности: предельная ставка, отрыв от минимума и доходность."""

import pytest

from landtender.bidding import (
    DEFAULT_OVERHEAD,
    DEFAULT_PURCHASE_TAX,
    DEFAULT_TARGET_ROI,
    advise,
    outlay,
)
from landtender.models import Lot


def lot(**kw) -> Lot:
    data = dict(
        source="rmi_michrazim",
        source_id="1",
        price_nis=6_000_000.0,
        price_kind="min",
        estimate_nis=11_000_000.0,
    )
    data.update(kw)
    return Lot(**data)


class TestOutlay:
    def test_taxes_and_overhead_ride_on_the_bid(self):
        value = outlay(1_000_000.0, lot(development_costs_nis=None), 0.06, 0.03)
        assert value == pytest.approx(1_090_000.0)

    def test_development_costs_are_added_whole(self):
        value = outlay(1_000_000.0, lot(development_costs_nis=500_000.0), 0.06, 0.03)
        assert value == pytest.approx(1_590_000.0)


class TestAdvise:
    def test_max_bid_delivers_exactly_the_target_roi(self):
        """Главная проверка: на предельной ставке доходность равна целевой."""
        target = lot(development_costs_nis=1_200_000.0)
        advice = advise(target)
        spent = outlay(advice.max_bid_nis, target, DEFAULT_PURCHASE_TAX, DEFAULT_OVERHEAD)
        roi = (advice.exit_value_nis - spent) / spent
        assert roi == pytest.approx(DEFAULT_TARGET_ROI)

    def test_headroom_against_the_minimum(self):
        advice = advise(lot(price_nis=3_000_000.0, estimate_nis=6_000_000.0))
        assert advice.headroom_pct > 0
        assert advice.headroom_ratio == pytest.approx(advice.max_bid_nis / 3_000_000.0)

    def test_roi_at_the_minimum_price(self):
        advice = advise(lot(price_nis=5_000_000.0, estimate_nis=10_000_000.0))
        spent = outlay(5_000_000.0, lot(), DEFAULT_PURCHASE_TAX, DEFAULT_OVERHEAD)
        assert advice.roi_at_min == pytest.approx((10_000_000.0 - spent) / spent)

    def test_higher_target_lowers_the_ceiling(self):
        modest = advise(lot(), target_roi=0.10).max_bid_nis
        greedy = advise(lot(), target_roi=0.50).max_bid_nis
        assert greedy < modest

    def test_development_costs_lower_the_ceiling(self):
        bare = advise(lot()).max_bid_nis
        loaded = advise(lot(development_costs_nis=3_000_000.0)).max_bid_nis
        assert loaded < bare

    def test_expensive_minimum_is_flagged_as_not_viable(self):
        advice = advise(lot(price_nis=10_000_000.0, estimate_nis=9_000_000.0))
        assert advice.viable is False
        assert advice.roi_at_min < 0

    def test_costs_can_eat_the_whole_deal(self):
        advice = advise(lot(estimate_nis=1_000_000.0, development_costs_nis=5_000_000.0))
        assert advice.max_bid_nis == 0.0
        assert advice.viable is False

    def test_without_an_estimate_there_is_no_advice(self):
        """Без оценки выхода доходности не существует, и выдумывать её нельзя."""
        assert advise(lot(estimate_nis=None)) is None

    def test_final_price_is_not_a_minimum(self):
        """У состоявшейся сделки минимальной цены уже нет — торговаться не с чем."""
        advice = advise(lot(price_kind="final"))
        assert advice.min_price_nis is None
        assert advice.headroom_pct is None
        assert advice.roi_at_min is None

    def test_tax_rates_come_from_the_caller(self):
        low = advise(lot(), purchase_tax=0.0, overhead=0.0).max_bid_nis
        high = advise(lot(), purchase_tax=0.10, overhead=0.05).max_bid_nis
        assert high < low
