"""Разбор ответов портала земельных тендеров рм"י."""

import pytest

from landtender.http import HttpError
from landtender.sources import RmiMichrazimSource, SourceContext
from tests.conftest import FakeHttp, load_fixture


def make_source(routes: dict, options: dict | None = None, cache=None, full_refresh=False):
    ctx = SourceContext(
        http=FakeHttp(routes),
        options=options or {},
        cache=cache,
        full_refresh=full_refresh,
    )
    return RmiMichrazimSource(ctx)


def details_route(params, _body):
    tender_id = params.get("michrazID")
    try:
        return load_fixture(f"rmi_details_{tender_id}.json")
    except FileNotFoundError:
        return {}


ROUTES = {
    "SearchApi/Search": load_fixture("rmi_search.json"),
    "MichrazDetailsApi/Get": details_route,
}


class TestFetch:
    def test_expands_tender_into_per_plot_lots(self):
        lots = list(make_source(ROUTES).fetch())
        haifa = [lot for lot in lots if lot.tender_id == "20250142"]
        assert len(haifa) == 3
        assert {lot.chelka for lot in haifa} == {"42", "43", "7"}

    def test_reads_price_units_and_area(self):
        lots = {lot.source_id: lot for lot in make_source(ROUTES).fetch()}
        lot = lots["20250142:10769/42"]
        assert lot.price_nis == 18_500_000.0
        assert lot.price_kind == "min"
        assert lot.units == 60
        assert lot.area_sqm == 4200.0
        assert lot.development_costs_nis == 3_400_000.0

    def test_final_price_wins_when_tender_has_results(self):
        lots = {lot.source_id: lot for lot in make_source(ROUTES).fetch()}
        lot = lots["20250142:10769/43"]
        assert (lot.price_nis, lot.price_kind) == (3_610_000.0, "final")

    def test_inherits_tender_level_geography(self):
        lots = {lot.source_id: lot for lot in make_source(ROUTES).fetch()}
        lot = lots["20250142:10769/42"]
        assert lot.settlement == "חיפה"
        assert lot.neighborhood == "נווה שאנן"
        assert lot.closing_date == "2026-09-15"

    def test_decodes_purpose_and_status_codes(self):
        lots = list(make_source(ROUTES).fetch())
        haifa = next(lot for lot in lots if lot.tender_id == "20250142")
        assert haifa.purpose == "מגורים"
        assert haifa.status == "פתוח"

    def test_infers_single_unit_for_private_house_plot(self):
        lots = {lot.source_id: lot for lot in make_source(ROUTES).fetch()}
        lot = lots["20250143:5599/18"]
        assert lot.units == 1
        assert lot.units_basis == "inferred"

    def test_tender_without_details_becomes_single_lot(self):
        lots = [lot for lot in make_source(ROUTES).fetch() if lot.tender_id == "20250144"]
        assert len(lots) == 1
        assert lots[0].price_nis is None
        assert lots[0].source_id == "20250144"

    def test_builds_portal_url(self):
        lot = next(iter(make_source(ROUTES).fetch()))
        assert lot.url == "https://apps.land.gov.il/MichrazimSite/#/michraz/20250142"

    def test_documents_are_not_mistaken_for_lots(self):
        lots = list(make_source(ROUTES).fetch())
        assert all("חוברת" not in (lot.tender_name or "") for lot in lots)


class TestResilience:
    def test_details_failure_degrades_to_tender_level_lot(self):
        routes = dict(ROUTES, **{"MichrazDetailsApi/Get": HttpError("500")})
        lots = list(make_source(routes).fetch())
        assert len(lots) == 3  # по одному лоту на тендер, без цен
        assert all(lot.price_nis is None for lot in lots)

    def test_search_failure_propagates(self):
        routes = {"SearchApi/Search": HttpError("нет сети")}
        with pytest.raises(HttpError):
            list(make_source(routes).fetch())

    def test_details_budget_limits_requests(self):
        source = make_source(ROUTES, options={"details_budget": 1})
        list(source.fetch())
        detail_calls = [c for c in source.ctx.http.calls if "MichrazDetailsApi" in c[1]]
        assert len(detail_calls) == 1


class FakeCache:
    def __init__(self, known: dict[str, str] | None = None):
        self.known = known or {}

    def tender_changed(self, source, tender_id, fingerprint):
        return self.known.get(tender_id) != fingerprint

    def remember_tender(self, source, tender_id, fingerprint):
        self.known[tender_id] = fingerprint


class TestCache:
    def test_unchanged_tender_skips_details_request(self):
        cache = FakeCache()
        first = make_source(ROUTES, cache=cache)
        list(first.fetch())

        second = make_source(ROUTES, cache=cache)
        list(second.fetch())
        detail_calls = [c for c in second.ctx.http.calls if "MichrazDetailsApi" in c[1]]
        assert detail_calls == []

    def test_full_refresh_ignores_cache(self):
        cache = FakeCache()
        list(make_source(ROUTES, cache=cache).fetch())

        source = make_source(ROUTES, cache=cache, full_refresh=True)
        list(source.fetch())
        detail_calls = [c for c in source.ctx.http.calls if "MichrazDetailsApi" in c[1]]
        assert len(detail_calls) == 3


class TestCodeTables:
    def test_unknown_code_leaves_field_empty_instead_of_guessing(self):
        routes = dict(ROUTES, **{"SearchApi/Search": [{"MichrazID": 1, "KodYeudMichraz": 99}]})
        lot = next(iter(make_source(routes).fetch()))
        assert lot.purpose is None

    def test_config_can_override_code_table(self):
        routes = dict(ROUTES, **{"SearchApi/Search": [{"MichrazID": 1, "KodYeudMichraz": 99}]})
        source = make_source(routes, options={"purpose_codes": {99: "תיירות"}})
        assert next(iter(source.fetch())).purpose == "תיירות"

    def test_textual_purpose_is_used_as_is(self):
        routes = dict(ROUTES, **{"SearchApi/Search": [{"MichrazID": 1, "Yeud": "מגורים ומסחר"}]})
        assert next(iter(make_source(routes).fetch())).purpose == "מגורים ומסחר"

    def test_numeric_region_code_is_not_shown_as_region(self):
        lot = next(iter(make_source(ROUTES).fetch()))
        assert lot.region is None


def test_active_only_option_reaches_payload():
    source = make_source(ROUTES, options={"active_only": True})
    list(source.fetch())
    _, _, kwargs = source.ctx.http.calls[0]
    assert kwargs["json"]["ActiveMichraz"] is True
