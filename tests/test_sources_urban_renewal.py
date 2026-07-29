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


class TestRealSchema:
    """Схема снята с живого набора `urban_renewal` командой inspect --ckan.

    Заголовки — транслитерация с иврита, а значения дополнены пробелами
    до фиксированной ширины.
    """

    ROW = {
        "_id": 1,
        "MisparMitham": 4001,
        "Yeshuv": "ירושלים" + " " * 200,
        "SemelYeshuv": 3000,
        "ShemMitcham": "ערבי נחל" + " " * 200,
        "YachadKayam": "126",
        "YachadTosafti": "108",
        "YachadMutza": 530,
        "TaarichHachraza": "20/08/2006",
        "MisparTochnit": "גב/490" + " " * 100,
        "KishurLaMapa": "https://www.govmap.gov.il/map.html?lay=X" + " " * 80,
        "Maslul": "פינוי בינוי" + " " * 50,
        "Status": "מוכרז",
    }

    def lot(self):
        return next(iter(make([self.ROW]).fetch()))

    def test_complex_name_is_read(self):
        assert self.lot().tender_name == "ערבי נחל"

    def test_trailing_padding_is_stripped(self):
        assert self.lot().settlement == "ירושלים"

    def test_final_unit_count_is_used(self):
        """YachadMutza — итог после стройки, YachadTosafti лишь прибавка."""
        assert self.lot().units == 530

    def test_existing_units_fall_back_when_final_is_missing(self):
        row = dict(self.ROW)
        del row["YachadMutza"]
        del row["YachadTosafti"]
        assert next(iter(make([row]).fetch())).units == 126

    def test_track_is_recognised(self):
        lot = self.lot()
        assert lot.renewal_kind == "pinui_binui"
        assert "פינוי בינוי" in lot.purpose

    def test_plan_number_joins_the_purpose(self):
        assert "גב/490" in self.lot().purpose

    def test_map_link_beats_the_dataset_page(self):
        assert self.lot().url == "https://www.govmap.gov.il/map.html?lay=X"

    def test_declaration_date_is_parsed(self):
        assert self.lot().published_date == "2006-08-20"

    def test_complex_number_becomes_the_tender_id(self):
        assert self.lot().tender_id == "4001"

    def test_still_marked_as_built_up(self):
        assert self.lot().has_structure is True


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
