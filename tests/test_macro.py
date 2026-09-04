"""Индексы ЦСБ и ставка Банка Израиля."""

from landtender.macro import (
    BUILDING_INPUTS,
    CPI,
    HOUSING,
    CbsIndices,
    IndexPoint,
    collect,
    fetch_boi_rate,
    index_factor,
    parse_index,
)
from tests.conftest import FakeHttp

#: Настоящий ответ API (снят разведкой): значение лежит в currBase.value.
BUILDING_INPUTS_RESPONSE = {
    "month": [
        {
            "code": 200010,
            "name": 'מדד מחירי תשומה בבנייה למגורים - כללי',
            "date": [
                {"year": 2026, "month": 7, "percent": -0.1, "percentYear": 3.5,
                 "currBase": {"baseDesc": "2025 יולי", "value": 103.5}, "prevBase": None},
                {"year": 2026, "month": 6, "percent": 0.2, "percentYear": 3.7,
                 "currBase": {"value": 103.6}, "prevBase": None},
                {"year": 2025, "month": 7, "percent": 0.6,
                 "currBase": {"value": 100.0}, "prevBase": None},
            ],
        }
    ],
    "quarter": None,
    "paging": {"total_items": 3},
}


class TestParse:
    def test_reads_values_and_periods(self):
        points = parse_index(BUILDING_INPUTS_RESPONSE, BUILDING_INPUTS)
        assert [(p.period, p.value) for p in points] == [
            ("2026-07", 103.5), ("2026-06", 103.6), ("2025-07", 100.0)
        ]

    def test_keeps_the_official_changes(self):
        latest = parse_index(BUILDING_INPUTS_RESPONSE, BUILDING_INPUTS)[0]
        assert latest.change_month == -0.1
        assert latest.change_year == 3.5

    def test_newest_first_regardless_of_input_order(self):
        shuffled = {"month": [{"code": 1, "name": "x", "date": [
            {"year": 2024, "month": 3, "currBase": {"value": 90.0}},
            {"year": 2026, "month": 1, "currBase": {"value": 101.0}},
        ]}]}
        assert parse_index(shuffled, "1")[0].period == "2026-01"

    def test_quarterly_series_lands_on_the_last_month(self):
        payload = {"quarter": [{"code": 40010, "name": "דירות", "date": [
            {"year": 2026, "quarter": 2, "currBase": {"value": 120.0}},
        ]}]}
        assert parse_index(payload, HOUSING)[0].period == "2026-06"

    def test_entry_without_value_is_dropped(self):
        payload = {"month": [{"code": 1, "date": [
            {"year": 2026, "month": 1, "currBase": {}},
            {"year": 2026, "month": 2, "currBase": {"value": 5.0}},
        ]}]}
        assert len(parse_index(payload, "1")) == 1

    def test_garbage_is_not_fatal(self):
        assert parse_index(None, "1") == []
        assert parse_index({"month": "nonsense"}, "1") == []


class TestIndexFactor:
    def points(self):
        return parse_index(BUILDING_INPUTS_RESPONSE, BUILDING_INPUTS)

    def test_growth_since_a_past_month(self):
        assert index_factor(self.points(), "2025-07-15") == 1.035

    def test_latest_month_gives_one(self):
        assert index_factor(self.points(), "2026-07-01") == 1.0

    def test_date_before_the_series_is_unknown(self):
        """Множитель 1.0 по умолчанию тихо соврал бы, что инфляции не было."""
        assert index_factor(self.points(), "2019-01-01") is None

    def test_empty_series_or_date(self):
        assert index_factor([], "2025-01-01") is None
        assert index_factor(self.points(), None) is None

    def test_malformed_date(self):
        assert index_factor(self.points(), "не дата") is None


class TestCbsIndices:
    def test_latest_value(self):
        http = FakeHttp({"index/data/price": BUILDING_INPUTS_RESPONSE})
        latest = CbsIndices(http).latest(BUILDING_INPUTS)
        assert latest.value == 103.5

    def test_request_carries_the_code(self):
        http = FakeHttp({"index/data/price": BUILDING_INPUTS_RESPONSE})
        CbsIndices(http).series(CPI, last=5)
        params = http.calls[0][2]["params"]
        assert params["id"] == CPI
        assert params["last"] == 5

    def test_failure_gives_empty_series(self):
        from landtender.http import HttpError

        http = FakeHttp({"index/data/price": HttpError("HTTP 500")})
        assert CbsIndices(http).series(BUILDING_INPUTS) == []

    def test_cache_survives_a_broken_api(self, tmp_path):
        from landtender.http import HttpError

        good = FakeHttp({"index/data/price": BUILDING_INPUTS_RESPONSE})
        CbsIndices(good, cache_path=tmp_path).series(BUILDING_INPUTS)

        broken = FakeHttp({"index/data/price": HttpError("HTTP 500")})
        points = CbsIndices(broken, cache_path=tmp_path).series(BUILDING_INPUTS)
        assert points and points[0].value == 103.5

    def test_fresh_cache_avoids_the_network(self, tmp_path):
        http = FakeHttp({"index/data/price": BUILDING_INPUTS_RESPONSE})
        CbsIndices(http, cache_path=tmp_path).series(BUILDING_INPUTS, last=3)
        before = len(http.calls)
        CbsIndices(http, cache_path=tmp_path).series(BUILDING_INPUTS, last=3)
        assert len(http.calls) == before


class TestBoiRate:
    def test_reads_the_rate(self):
        http = FakeHttp({"GetInterest": {"interestRate": 4.5, "date": "2026-08-01T00:00:00"}})
        assert fetch_boi_rate(http) == (4.5, "2026-08-01")

    def test_nested_answer_is_still_found(self):
        http = FakeHttp({"GetInterest": {"data": [{"rate": 4.25}]}})
        assert fetch_boi_rate(http)[0] == 4.25

    def test_failure_invents_nothing(self):
        """Подставленная ставка испортила бы оценку молча."""
        from landtender.http import HttpError

        http = FakeHttp({"GetInterest": HttpError("HTTP 404")})
        assert fetch_boi_rate(http) == (None, None)


def test_collect_gathers_everything():
    http = FakeHttp({
        "index/data/price": BUILDING_INPUTS_RESPONSE,
        "GetInterest": {"interestRate": 4.5},
    })
    macro = collect(http)
    assert macro.building_inputs.value == 103.5
    assert macro.boi_rate == 4.5
    assert macro.empty is False
