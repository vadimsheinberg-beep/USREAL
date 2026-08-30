"""Пять показателей полезности лота."""

from datetime import date

import pytest

from landtender.invest import SIGNAL_CONFIRMED, SIGNAL_EARLY, SIGNAL_LIKELY, SIGNAL_NONE
from landtender.models import Lot
from landtender.scoring import WEIGHTS, Scorecard, score

TODAY = date(2026, 8, 28)


def lot(**kw) -> Lot:
    data = dict(source="rmi_michrazim", source_id="1")
    data.update(kw)
    return Lot(**data)


class TestPriceScore:
    def test_price_equal_to_estimate_is_the_middle(self):
        card = score(lot(price_nis=1_000_000.0, estimate_nis=1_000_000.0), TODAY)
        assert card.price == 50.0

    def test_half_the_estimate_is_full_marks(self):
        card = score(lot(price_nis=500_000.0, estimate_nis=1_000_000.0), TODAY)
        assert card.price == 100.0

    def test_far_above_the_estimate_is_zero(self):
        card = score(lot(price_nis=3_000_000.0, estimate_nis=1_000_000.0), TODAY)
        assert card.price == 0.0

    def test_scale_is_relative_not_absolute(self):
        """Скидка в миллион на участке за два и за сто — разные события."""
        cheap = score(lot(price_nis=1_000_000.0, estimate_nis=2_000_000.0), TODAY)
        dear = score(lot(price_nis=99_000_000.0, estimate_nis=100_000_000.0), TODAY)
        assert cheap.price > dear.price

    def test_without_estimate_there_is_no_score(self):
        assert score(lot(price_nis=1_000_000.0), TODAY).price is None


class TestRezoningScore:
    @pytest.mark.parametrize(
        "signal, expected",
        [
            (SIGNAL_CONFIRMED, 100.0),
            (SIGNAL_LIKELY, 70.0),
            (SIGNAL_EARLY, 40.0),
            (SIGNAL_NONE, 0.0),
        ],
    )
    def test_stage_becomes_score(self, signal, expected):
        assert score(lot(plan_signal=signal), TODAY).rezoning == expected

    def test_unknown_signal_is_empty(self):
        assert score(lot(), TODAY).rezoning is None


class TestDensityScore:
    def test_eight_units_per_dunam_is_full(self):
        assert score(lot(units=8, area_sqm=1000.0), TODAY).density == 100.0

    def test_half_density(self):
        assert score(lot(units=4, area_sqm=1000.0), TODAY).density == 50.0

    def test_above_the_ceiling_stays_at_hundred(self):
        assert score(lot(units=40, area_sqm=1000.0), TODAY).density == 100.0

    def test_needs_both_units_and_area(self):
        assert score(lot(units=8), TODAY).density is None
        assert score(lot(area_sqm=1000.0), TODAY).density is None


class TestMarketScore:
    def test_deep_sample_is_full(self):
        assert score(lot(estimate_n=30), TODAY).market == 100.0

    def test_thin_sample_scores_low(self):
        assert score(lot(estimate_n=9), TODAY).market == 30.0

    def test_no_sample_no_score(self):
        assert score(lot(), TODAY).market is None


class TestTimingScore:
    def test_plenty_of_time(self):
        assert score(lot(closing_date="2026-12-31"), TODAY).timing == 100.0

    def test_expired_scores_zero(self):
        assert score(lot(closing_date="2026-01-01"), TODAY).timing == 0.0

    def test_tight_window_is_penalised(self):
        """За неделю банковскую гарантию не оформить."""
        assert score(lot(closing_date="2026-09-04"), TODAY).timing < 50.0

    def test_no_date_no_score(self):
        assert score(lot(), TODAY).timing is None

    def test_malformed_date(self):
        assert score(lot(closing_date="не дата"), TODAY).timing is None


class TestTotal:
    def test_weights_are_applied(self):
        card = Scorecard(price=100.0, rezoning=0.0, density=0.0, market=0.0, timing=0.0)
        assert card.total == pytest.approx(100.0 * WEIGHTS["price"])

    def test_missing_indicators_do_not_count_as_zero(self):
        """Среднее по трём известным честнее среднего по пяти с домыслами."""
        partial = Scorecard(price=100.0, rezoning=100.0)
        assert partial.total == 100.0
        assert partial.coverage == 2

    def test_all_unknown_gives_nothing(self):
        assert Scorecard().total is None
        assert Scorecard().coverage == 0

    def test_full_card(self):
        card = score(
            lot(
                price_nis=6_000_000.0,
                estimate_nis=11_000_000.0,
                plan_signal=SIGNAL_LIKELY,
                units=30,
                area_sqm=5000.0,
                estimate_n=24,
                closing_date="2026-11-30",
            ),
            TODAY,
        )
        assert card.coverage == 5
        assert 0 < card.total <= 100


class TestDensityOnPlaceholderArea:
    """166 квартир на одном квадратном метре — брак портала, а не плотность.

    Тендер 405/2021 пришёл с площадью «1 м²» и 166 единицами. Деление давало
    двадцать тысяч квартир на дунам, полный балл за плотность и первое место
    в рейтинге — при том, что цены у лота вообще не было.
    """

    def test_placeholder_area_yields_no_density_score(self):
        from landtender.scoring import score
        card = score(Lot(source="rmi_michrazim", source_id="1", area_sqm=1.0, units=166))
        assert card.density is None

    def test_a_real_area_still_scores(self):
        from landtender.scoring import score
        card = score(Lot(source="rmi_michrazim", source_id="1", area_sqm=1000.0, units=8))
        assert card.density == 100.0
