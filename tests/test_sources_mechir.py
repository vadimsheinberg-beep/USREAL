"""Программа «דירה בהנחה»: субсидированные квартиры с ценой за метр."""

import pytest

from landtender.sources import SourceContext
from landtender.sources.mechir_lamishtaken import MechirLamishtakenSource
from tests.conftest import FakeHttp

#: Запись снята разведкой с живого набора, а не придумана.
REAL_ROW = {
    "_id": 1,
    "WinnersHasryDiur": 79,
    "Winners": 79,
    "Subscribers": 6594,
    "LotteryHousingUnits": 79,
    "PriceForMeter": "9,242.00",
    "ProjectStatus": "בתהליכי הגרלה",
    "ProviderName": "י. ד. ברזאני נכסים ובניין (1983) בע",
    "ProjectName": "מגרשים 16A,16B,16P,17A,17B,17P,18-19",
    "ProjectId": 73418,
    "Neighborhood": "שכונת השקדיות",
    "LamasName": "מגדל העמק",
    "LamasCode": 874,
    "LotteryExecutionDate": "2025-01-27 10:38:07",
    "LotteryEndSignupDate": "2025-01-21 00:00:00",
    "MarketingMethodDesc": "מחיר למשתכן",
    "LotteryId": 2564,
}


def make_source(records, options=None):
    routes = {
        "datastore_search": {"result": {"records": records}},
    }
    return MechirLamishtakenSource(
        SourceContext(http=FakeHttp(routes), options=options or {})
    )


class TestRealRecord:
    def lot(self, **overrides):
        row = dict(REAL_ROW)
        row.update(overrides)
        return next(iter(make_source([row]).fetch()), None)

    def test_price_per_metre_survives_the_thousands_separator(self):
        """Портал шлёт «9,242.00» строкой — без разбора это ноль."""
        assert self.lot().price_per_sqm_nis == 9242.0

    def test_full_price_is_not_invented(self):
        """Площади квартиры набор не даёт; полная цена из неё не выводится."""
        lot = self.lot()
        assert lot.price_nis is None
        assert lot.area_sqm is None

    def test_city_comes_with_its_cbs_code(self):
        """Код — тот же, по которому сравниваются участки: место сходится."""
        lot = self.lot()
        assert lot.settlement == "מגדל העמק"
        assert lot.settlement_code == 874

    def test_units_and_neighbourhood(self):
        lot = self.lot()
        assert lot.units == 79
        assert lot.neighborhood == "שכונת השקדיות"

    def test_dates_are_normalised(self):
        lot = self.lot()
        assert lot.closing_date == "2025-01-27"
        assert lot.published_date == "2025-01-21"

    def test_purpose_is_residential(self):
        assert self.lot().purpose == "מגורים"

    def test_the_row_links_to_the_programme_page(self):
        assert "gov.il" in self.lot().url


class TestRejections:
    def test_a_row_without_a_price_is_skipped(self):
        """Запись без цены метра не сообщает ничего, ради чего набор взят."""
        row = dict(REAL_ROW, PriceForMeter=None)
        assert list(make_source([row]).fetch()) == []

    def test_a_zero_price_is_skipped(self):
        row = dict(REAL_ROW, PriceForMeter="0")
        assert list(make_source([row]).fetch()) == []

    def test_a_row_without_a_lottery_id_is_skipped(self):
        row = dict(REAL_ROW, LotteryId=None)
        assert list(make_source([row]).fetch()) == []


class TestOpenOnly:
    def test_finished_lotteries_can_be_filtered_out(self):
        """Проект с опубликованными результатами — история, а не предложение."""
        rows = [
            dict(REAL_ROW, LotteryId=1, ProjectStatus="בתהליכי הגרלה"),
            dict(REAL_ROW, LotteryId=2, ProjectStatus="הסתיים"),
        ]
        lots = list(make_source(rows, {"only_open": True}).fetch())
        assert [lot.source_id for lot in lots] == ["lottery:1"]

    def test_by_default_the_whole_history_is_kept(self):
        """История цен — тоже данные: по ней видно, куда движется рынок."""
        rows = [
            dict(REAL_ROW, LotteryId=1, ProjectStatus="בתהליכי הגרלה"),
            dict(REAL_ROW, LotteryId=2, ProjectStatus="הסתיים"),
        ]
        assert len(list(make_source(rows).fetch())) == 2
