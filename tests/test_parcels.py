"""Кадастровые участки govmap: площадь и город по гуш/хелка."""

from landtender.parcels import GovmapParcels, Parcel
from tests.conftest import FakeHttp

#: Настоящий ответ сервиса по участку 10223/100 (снят разведкой).
PARCEL_10223_100 = {
    "features": [
        {
            "properties": {
                "OBJECTID": 688000,
                "GUSH_NUM": 10223,
                "PARCEL": 100,
                "LEGAL_AREA": 1000,
                "SHAPE_AREA": 999.57837476,
                "STATUS_TEX": "מוסדר",
                "LOCALITY_N": "בנימינה-גבעת עדה",
                "COUNTY_NAM": "חדרה",
                "REGION_NAM": "חיפה",
            }
        }
    ]
}

EMPTY = {"features": []}


def source(response):
    return GovmapParcels(FakeHttp({"geoserver/opendata/wfs": response}))


class TestFind:
    def test_reads_area_and_place(self):
        parcel = source(PARCEL_10223_100).find(10223, 100)
        assert parcel is not None
        assert parcel.legal_area_sqm == 1000.0
        assert parcel.settlement == "בנימינה-גבעת עדה"
        assert parcel.region == "חיפה"
        assert parcel.status == "מוסדר"

    def test_registered_area_wins_over_computed(self):
        parcel = source(PARCEL_10223_100).find(10223, 100)
        assert parcel.area_sqm == 1000.0

    def test_falls_back_to_computed_area(self):
        response = {"features": [{"properties": {"SHAPE_AREA": 4200.0}}]}
        assert source(response).find(1, 2).area_sqm == 4200.0

    def test_dunam_conversion(self):
        assert source(PARCEL_10223_100).find(10223, 100).dunam == 1.0

    def test_missing_parcel_is_none(self):
        assert source(EMPTY).find(99999, 1) is None

    def test_string_numbers_are_accepted(self):
        assert source(PARCEL_10223_100).find("10223", "100") is not None

    def test_non_numeric_input_does_not_reach_the_service(self):
        """Текст в CQL-фильтре уронил бы запрос — до сети дело не доходит."""
        http = FakeHttp({})
        assert GovmapParcels(http).find("גוש", "חלקה") is None
        assert http.calls == []

    def test_filter_uses_both_numbers(self):
        http = FakeHttp({"geoserver": PARCEL_10223_100})
        GovmapParcels(http).find(10223, 100)
        params = http.calls[0][2]["params"]
        assert params["cql_filter"] == "GUSH_NUM=10223 AND PARCEL=100"
        assert params["typeNames"] == "opendata:PARCEL_ALL"

    def test_network_failure_is_not_fatal(self):
        from landtender.http import HttpError

        assert source(HttpError("HTTP 500")).find(10223, 100) is None

    def test_zero_area_is_treated_as_unknown(self):
        response = {"features": [{"properties": {"LEGAL_AREA": 0, "SHAPE_AREA": 0}}]}
        parcel = source(response).find(1, 2)
        assert parcel.area_sqm is None
        assert parcel.dunam is None


def test_parcel_without_area_reports_nothing():
    assert Parcel(gush="1", chelka="2").area_sqm is None
