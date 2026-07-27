"""Стоимость земли в долларах и отсечка по порогу в 1 000 000 $."""

import pytest

from landtender.models import TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN, Lot
from landtender.money import FxProvider, choose_price, enrich_lot
from tests.conftest import FakeHttp, load_fixture

DEFAULT_PREFERENCE = ["final", "min", "appraisal", "asking"]


class TestChoosePrice:
    def test_prefers_final_price_over_minimum(self):
        price, kind = choose_price({"final": 3_610_000, "min": 2_900_000}, DEFAULT_PREFERENCE)
        assert (price, kind) == (3_610_000, "final")

    def test_falls_back_to_minimum(self):
        price, kind = choose_price({"final": None, "min": 2_900_000}, DEFAULT_PREFERENCE)
        assert (price, kind) == (2_900_000, "min")

    def test_falls_back_to_appraisal(self):
        price, kind = choose_price({"appraisal": 1_000}, DEFAULT_PREFERENCE)
        assert kind == "appraisal"

    def test_zero_is_not_a_price(self):
        assert choose_price({"final": 0, "min": 0}, DEFAULT_PREFERENCE) == (None, None)

    def test_custom_preference_is_respected(self):
        price, kind = choose_price({"final": 5, "min": 7}, ["min", "final"])
        assert (price, kind) == (7, "min")


class TestEnrichLot:
    def test_expensive_lot_goes_to_premium(self, fx_rate):
        # 18.5 млн ₪ / 3.6412 ≈ 5.08 млн $
        lot = enrich_lot(Lot("t", "1", price_nis=18_500_000, units=60), fx_rate, 1_000_000)
        assert lot.tier == TIER_PREMIUM
        assert lot.price_usd == pytest.approx(5_080_742.6, rel=1e-4)

    def test_cheap_lot_goes_to_standard(self, fx_rate):
        lot = enrich_lot(Lot("t", "2", price_nis=2_900_000), fx_rate, 1_000_000)
        assert lot.tier == TIER_STANDARD

    def test_lot_exactly_at_threshold_is_premium(self, fx_rate):
        at_threshold = 1_000_000 * fx_rate.rate
        lot = enrich_lot(Lot("t", "3", price_nis=at_threshold), fx_rate, 1_000_000)
        assert lot.tier == TIER_PREMIUM

    def test_lot_without_price_is_unknown(self, fx_rate):
        lot = enrich_lot(Lot("t", "4", units=10), fx_rate, 1_000_000)
        assert lot.tier == TIER_UNKNOWN
        assert lot.price_usd is None
        assert lot.price_per_unit_usd is None

    def test_per_unit_and_per_sqm_are_computed(self, fx_rate):
        lot = enrich_lot(
            Lot("t", "5", price_nis=3_641_200, units=4, area_sqm=1000), fx_rate, 1_000_000
        )
        assert lot.price_per_unit_usd == pytest.approx(250_000, rel=1e-3)
        assert lot.price_per_sqm_usd == pytest.approx(1_000, rel=1e-3)

    def test_development_costs_can_push_lot_over_threshold(self, fx_rate):
        # 3.5 млн ₪ ≈ 0.96 млн $ — ниже порога; с развитием 1.2 млн ₪ — выше
        base = Lot("t", "6", price_nis=3_500_000, development_costs_nis=1_200_000)
        assert enrich_lot(base, fx_rate, 1_000_000).tier == TIER_STANDARD

        with_dev = Lot("t", "7", price_nis=3_500_000, development_costs_nis=1_200_000)
        enrich_lot(with_dev, fx_rate, 1_000_000, include_development_costs=True)
        assert with_dev.tier == TIER_PREMIUM

    def test_custom_threshold(self, fx_rate):
        lot = enrich_lot(Lot("t", "8", price_nis=18_500_000), fx_rate, 10_000_000)
        assert lot.tier == TIER_STANDARD


class TestFxProvider:
    def test_reads_boi_response(self, tmp_path):
        http = FakeHttp({"GetExchangeRate": load_fixture("boi_usd.json")})
        rate = FxProvider(http, cache_path=tmp_path / "fx.json").get()
        assert rate.rate == 3.6412
        assert rate.source == "boi"
        assert rate.as_of == "2026-07-27"

    def test_divides_by_unit_for_currencies_quoted_per_100(self, tmp_path):
        http = FakeHttp({"GetExchangeRate": {"key": "USD", "currentExchangeRate": 364.12, "unit": 100}})
        rate = FxProvider(http, cache_path=tmp_path / "fx.json").get()
        assert rate.rate == pytest.approx(3.6412)

    def test_falls_back_to_static_rate_when_api_is_down(self, tmp_path):
        from landtender.http import HttpError

        http = FakeHttp({"GetExchangeRate": HttpError("boi недоступен")})
        rate = FxProvider(http, static_rate=3.8, cache_path=tmp_path / "fx.json").get()
        assert rate.rate == 3.8
        assert rate.source == "static-fallback"

    def test_second_call_uses_cache(self, tmp_path):
        http = FakeHttp({"GetExchangeRate": load_fixture("boi_usd.json")})
        cache = tmp_path / "fx.json"
        FxProvider(http, cache_path=cache).get()
        calls_after_first = len(http.calls)

        rate = FxProvider(http, cache_path=cache).get()
        assert len(http.calls) == calls_after_first
        assert rate.rate == 3.6412
        assert rate.source.endswith("-cached")

    def test_static_provider_skips_network(self, tmp_path):
        http = FakeHttp({})
        rate = FxProvider(http, provider="static", static_rate=3.5).get()
        assert (rate.rate, rate.source) == (3.5, "static")
        assert http.calls == []

    def test_rejects_implausible_rate(self, tmp_path):
        http = FakeHttp({"GetExchangeRate": {"key": "USD", "currentExchangeRate": 100000}})
        rate = FxProvider(http, static_rate=3.9, cache_path=tmp_path / "fx.json").get()
        assert rate.source == "static-fallback"
