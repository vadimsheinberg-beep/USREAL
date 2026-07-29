"""Реестр городского обновления: комплексы со старыми домами."""

import pytest

from landtender.sources import SourceContext, UrbanRenewalSource
from tests.conftest import FakeHttp


def package(name="urban-renewal-complexes", title="מתחמי התחדשות עירונית", resources=None):
    return {
        "name": name,
        "title": title,
        "notes": "רשימת מתחמי פינוי בינוי מוכרזים",
        "resources": resources or [{"id": "res-ur-1", "datastore_active": True}],
    }


def routes(records, packages=None):
    return {
        "package_search": {"result": {"results": packages or [package()]}},
        "datastore_search": {"result": {"records": records}},
    }


def make(records, options=None, packages=None):
    ctx = SourceContext(http=FakeHttp(routes(records, packages)), options=options or {"queries": ["התחדשות עירונית"]})
    return UrbanRenewalSource(ctx)


COMPLEX = {
    "_id": 1,
    "שם המתחם": "מתחם רחוב הרצל",
    "יישוב": "נתניה",
    "שכונה": "קרית השרון",
    "מסלול": "פינוי בינוי",
    "יחידות דיור קיימות": 120,
    "יחידות דיור מתוכננות": 480,
    "סטטוס": "מוכרז",
    "תאריך פרסום": "01/03/2026",
}


class TestFetch:
    def test_reads_a_renewal_complex(self):
        lots = list(make([COMPLEX]).fetch())
        assert len(lots) == 1
        lot = lots[0]
        assert lot.settlement == "נתניה"
        assert lot.tender_name == "מתחם רחוב הרצל"

    def test_always_marked_as_having_a_structure(self):
        """Весь этот реестр — застроенная земля, пустых участков в нём нет."""
        lot = next(iter(make([COMPLEX]).fetch()))
        assert lot.has_structure is True
        assert lot.renewal_kind == "pinui_binui"

    def test_planned_units_are_preferred_over_existing(self):
        lot = next(iter(make([COMPLEX]).fetch()))
        assert lot.units == 480

    def test_existing_units_used_when_planned_are_missing(self):
        row = dict(COMPLEX)
        row.pop("יחידות דיור מתוכננות")
        lot = next(iter(make([row]).fetch()))
        assert lot.units == 120

    def test_track_becomes_the_purpose(self):
        assert next(iter(make([COMPLEX]).fetch())).purpose == "פינוי בינוי"

    def test_tama_38_track_is_recognised(self):
        row = dict(COMPLEX, **{"מסלול": 'תמ"א 38'})
        assert next(iter(make([row]).fetch())).renewal_kind == "tama_38"

    def test_date_is_parsed(self):
        assert next(iter(make([COMPLEX]).fetch())).published_date == "2026-03-01"

    def test_record_without_price_is_still_kept(self):
        """Цены у реестра нет, но комплекс всё равно интересен."""
        lot = next(iter(make([COMPLEX]).fetch()))
        assert lot.price_nis is None
        assert lot.units == 480

    def test_row_without_place_or_name_is_skipped(self):
        assert list(make([{"_id": 9, "הערה": "שורת סיכום"}]).fetch()) == []

    def test_unrelated_datasets_are_ignored(self):
        """Поиск CKAN нечёткий — набор про мусоровозы сюда попасть не должен."""
        unrelated = package(name="garbage-trucks", title="משאיות אשפה")
        unrelated["notes"] = "רשימת רכבים"
        source = make([COMPLEX], packages=[unrelated])
        assert list(source.fetch()) == []

    def test_max_rows_caps_output(self):
        rows = [dict(COMPLEX, _id=i) for i in range(10)]
        lots = list(make(rows, options={"queries": ["התחדשות"], "max_rows": 3}).fetch())
        assert len(lots) == 3


class TestResilience:
    def test_search_failure_does_not_raise(self):
        from landtender.http import HttpError

        ctx = SourceContext(http=FakeHttp({"package_search": HttpError("503")}), options={})
        assert list(UrbanRenewalSource(ctx).fetch()) == []

    def test_dataset_failure_skips_that_dataset(self):
        from landtender.http import HttpError

        ctx = SourceContext(
            http=FakeHttp({
                "package_search": {"result": {"results": [package()]}},
                "datastore_search": HttpError("500"),
            }),
            options={},
        )
        assert list(UrbanRenewalSource(ctx).fetch()) == []


def test_source_is_registered_as_government():
    assert UrbanRenewalSource.kind == "government"
    assert UrbanRenewalSource.name == "urban_renewal"
