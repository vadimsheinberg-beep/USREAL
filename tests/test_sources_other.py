"""Yad2, data.gov.il и mr.gov.il."""

import pytest

from landtender.sources import DataGovIlSource, GovMrSource, SourceContext, Yad2Source
from tests.conftest import FakeHttp, load_fixture


def make(source_cls, routes, options=None):
    return source_cls(SourceContext(http=FakeHttp(routes), options=options or {}))


# ------------------------------------------------------------------ Yad2 ----

YAD2_ROUTES = {"feed-search-legacy": load_fixture("yad2_feed.json")}


class TestYad2:
    def test_reads_listings_from_nested_feed(self):
        lots = list(make(Yad2Source, YAD2_ROUTES, {"max_pages": 1}).fetch())
        assert {lot.source_id for lot in lots} == {"abc123", "def456"}

    def test_parses_price_and_area(self):
        lots = {lot.source_id: lot for lot in make(Yad2Source, YAD2_ROUTES, {"max_pages": 1}).fetch()}
        lot = lots["abc123"]
        assert lot.price_nis == 6_200_000.0
        assert lot.price_kind == "asking"
        assert lot.area_sqm == 780.0
        assert lot.settlement == "כפר סבא"

    def test_extracts_units_from_listing_title(self):
        lots = {lot.source_id: lot for lot in make(Yad2Source, YAD2_ROUTES, {"max_pages": 1}).fetch()}
        assert lots["def456"].units == 4
        assert lots["def456"].units_basis == "inferred"

    def test_skips_banners(self):
        lots = list(make(Yad2Source, YAD2_ROUTES, {"max_pages": 1}).fetch())
        assert all(lot.tender_name != "פרסומת" for lot in lots)

    def test_builds_item_url(self):
        lots = {lot.source_id: lot for lot in make(Yad2Source, YAD2_ROUTES, {"max_pages": 1}).fetch()}
        assert lots["abc123"].url == "https://www.yad2.co.il/item/abc123"

    def test_marked_as_private_source(self):
        assert Yad2Source.kind == "private"

    def test_total_blockage_is_reported_as_source_failure(self):
        routes = {"feed-search-legacy": RuntimeError("заблокировано защитой от ботов")}
        with pytest.raises(RuntimeError, match="заблокировано"):
            list(make(Yad2Source, routes, {"max_pages": 1}).fetch())

    def test_failure_on_later_page_keeps_what_was_collected(self):
        state = {"page": 0}

        def flaky(params, _body):
            state["page"] += 1
            if state["page"] > 1:
                raise RuntimeError("429 Too Many Requests")
            return load_fixture("yad2_feed.json")

        lots = list(make(Yad2Source, {"feed-search-legacy": flaky}, {"max_pages": 3}).fetch())
        assert len(lots) == 2


# ------------------------------------------------------------ data.gov.il --


def ckan_routes():
    package = {
        "result": {
            "results": [
                {
                    "name": "rami-tenders",
                    "resources": [
                        {"id": "res-001", "datastore_active": True},
                        {"id": "res-002", "datastore_active": False},
                    ],
                }
            ]
        }
    }
    return {"package_search": package, "datastore_search": load_fixture("ckan_datastore.json")}


class TestDataGovIl:
    def test_reads_records_with_hebrew_columns(self):
        lots = list(make(DataGovIlSource, ckan_routes(), {"queries": ["מכרזים"]}).fetch())
        assert len(lots) == 2
        jerusalem = lots[0]
        assert jerusalem.settlement == "ירושלים"
        assert jerusalem.units == 140
        assert jerusalem.price_nis == 52_000_000
        assert jerusalem.gush == "30521"

    def test_skips_rows_that_are_not_lots(self):
        lots = list(make(DataGovIlSource, ckan_routes(), {"queries": ["מכרזים"]}).fetch())
        assert all("הערה" not in (lot.raw or {}) for lot in lots)

    def test_ignores_resources_without_datastore(self):
        source = make(DataGovIlSource, ckan_routes(), {"queries": ["מכרזים"]})
        list(source.fetch())
        datastore_calls = [c for c in source.ctx.http.calls if "datastore_search" in c[1]]
        assert {c[2]["params"]["resource_id"] for c in datastore_calls} == {"res-001"}

    def test_max_rows_caps_output(self):
        lots = list(make(DataGovIlSource, ckan_routes(), {"queries": ["מכרזים"], "max_rows": 1}).fetch())
        assert len(lots) == 1

    def test_parses_israeli_date_format(self):
        lots = list(make(DataGovIlSource, ckan_routes(), {"queries": ["מכרזים"]}).fetch())
        assert lots[0].published_date == "2026-06-01"


# --------------------------------------------------------------- mr.gov.il --


class FakeResponse:
    def __init__(self, text: str):
        self.text = text


MR_HTML = """
<html><body>
  <div class="results">
    <a href="/ilgstorefront/he/p/TENDER-555">מכרז 12/2026 להקצאת קרקע למגורים, מחיר מינימום 4,500,000 ₪, פורסם 01/07/2026</a>
    <a href="/ilgstorefront/he/p/TENDER-556">מכרז לאספקת מכשירי כתיבה למשרדי הממשלה</a>
    <a href="/ilgstorefront/he/p/TENDER-557">מכרז מקרקעין: מגרש בעכו, 12 יח"ד</a>
  </div>
</body></html>
"""


class TestGovMr:
    def test_keeps_only_land_related_tenders(self):
        source = make(GovMrSource, {"search": FakeResponse(MR_HTML)}, {"max_pages": 1, "keywords": ["מקרקעין"]})
        lots = list(source.fetch())
        assert {lot.source_id for lot in lots} == {"TENDER-555", "TENDER-557"}

    def test_parses_price_from_card_text(self):
        source = make(GovMrSource, {"search": FakeResponse(MR_HTML)}, {"max_pages": 1, "keywords": ["מקרקעין"]})
        lots = {lot.source_id: lot for lot in source.fetch()}
        assert lots["TENDER-555"].price_nis == 4_500_000.0
        assert lots["TENDER-555"].published_date == "2026-07-01"

    def test_extracts_units_from_card_text(self):
        source = make(GovMrSource, {"search": FakeResponse(MR_HTML)}, {"max_pages": 1, "keywords": ["מקרקעין"]})
        lots = {lot.source_id: lot for lot in source.fetch()}
        assert lots["TENDER-557"].units == 12

    def test_builds_absolute_url(self):
        source = make(GovMrSource, {"search": FakeResponse(MR_HTML)}, {"max_pages": 1, "keywords": ["מקרקעין"]})
        lots = {lot.source_id: lot for lot in source.fetch()}
        assert lots["TENDER-555"].url == "https://mr.gov.il/ilgstorefront/he/p/TENDER-555"

    def test_empty_page_stops_pagination(self):
        source = make(GovMrSource, {"search": FakeResponse("<html></html>")}, {"max_pages": 5, "keywords": ["מקרקעין"]})
        assert list(source.fetch()) == []
        assert len(source.ctx.http.calls) == 1
