"""Разбор ответов портала земельных тендеров рм"י."""

import pytest

from landtender.http import HttpError
from landtender.sources import RmiMichrazimSource, SourceContext
from landtender.sources.rmi_michrazim import _newest_first
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
        lot = lots["20250142:10769/42/1"]
        assert lot.price_nis == 18_500_000.0
        assert lot.price_kind == "min"
        assert lot.units == 60
        assert lot.area_sqm == 4200.0
        assert lot.development_costs_nis == 3_400_000.0

    def test_final_price_wins_when_tender_has_results(self):
        lots = {lot.source_id: lot for lot in make_source(ROUTES).fetch()}
        lot = lots["20250142:10769/43/2"]
        assert (lot.price_nis, lot.price_kind) == (3_610_000.0, "final")

    def test_inherits_tender_level_geography(self):
        lots = {lot.source_id: lot for lot in make_source(ROUTES).fetch()}
        lot = lots["20250142:10769/42/1"]
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
        lot = lots["20250143:5599/18/1"]
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


class TestRealPortalSchema:
    """Схема снята с живого ответа портала командой ``landtender inspect``.

    Имена полей у рм"י — транслитерация с иврита, и ни одно из них не
    совпало с тем, что предполагалось изначально.
    """

    ROUTES = {
        "SearchApi/Search": [{"MichrazID": 20260123, "MichrazName": "חי/123/2026", "StatusMichraz": 1}],
        "MichrazDetailsApi/Get": load_fixture("rmi_details_real_schema.json"),
    }

    def lots(self):
        return {lot.source_id: lot for lot in make_source(self.ROUTES).fetch()}

    def test_tik_array_expands_into_plots(self):
        assert len(self.lots()) == 2

    def test_mechir_saf_is_the_minimum_price(self):
        lot = self.lots()["20260123:10769/42/70113195א"]
        assert lot.price_nis == 18_500_000.0
        assert lot.price_kind == "min"

    def test_schum_zchiya_wins_when_tender_has_a_winner(self):
        lot = self.lots()["20260123:10769/43/70113196ב"]
        assert (lot.price_nis, lot.price_kind) == (3_610_000.0, "final")

    def test_mechir_shuma_is_the_appraisal(self):
        routes = dict(self.ROUTES)
        routes["MichrazDetailsApi/Get"] = {
            "MichrazID": 20260123,
            "Tik": [{"TikID": "1", "Shetach": 500, "mechirShuma": 4_000_000}],
        }
        lot = next(iter(make_source(routes).fetch()))
        assert (lot.price_nis, lot.price_kind) == (4_000_000.0, "appraisal")

    def test_hotzaot_pituach_is_development_cost(self):
        assert self.lots()["20260123:10769/42/70113195א"].development_costs_nis == 3_400_000.0

    def test_schum_arvut_is_the_bank_guarantee(self):
        assert self.lots()["20260123:10769/42/70113195א"].guarantee_nis == 1_850_000.0

    def test_kibolet_is_the_unit_count(self):
        lot = self.lots()["20260123:10769/42/70113195א"]
        assert lot.units == 60
        assert lot.units_basis == "reported"

    def test_shetach_is_the_area(self):
        assert self.lots()["20260123:10769/42/70113195א"].area_sqm == 4200.0

    def test_gush_helka_array_is_unpacked(self):
        lot = self.lots()["20260123:10769/42/70113195א"]
        assert (lot.gush, lot.chelka) == ("10769", "42")

    def test_documents_are_not_turned_into_plots(self):
        assert all("חוברת" not in (lot.tender_name or "") for lot in self.lots().values())

    def test_guarantee_is_roughly_ten_percent_of_the_price(self):
        """Проверка здравого смысла: рм"י требует не менее 10% от заявки."""
        lot = self.lots()["20260123:10769/42/70113195א"]
        assert lot.guarantee_nis == pytest.approx(lot.price_nis * 0.1, rel=0.01)


class TestRenewalLots:
    """Участки со старыми строениями должны попадать в сводку наравне с пустыми."""

    def source(self, details, name="חי/1/2026"):
        routes = {
            "SearchApi/Search": [{"MichrazID": 1, "MichrazName": name, "StatusMichraz": 1}],
            "MichrazDetailsApi/Get": details,
        }
        return make_source(routes)

    def test_built_area_marks_a_lot_as_having_a_structure(self):
        details = {
            "MichrazID": 1,
            "Tik": [{"TikID": "A", "Shetach": 1200, "ShetachBniya": 3400, "MechirSaf": 9_000_000}],
        }
        lot = next(iter(self.source(details).fetch()))
        assert lot.built_area_sqm == 3400.0
        assert lot.has_structure is True
        assert lot.renewal_kind == "existing_structure"

    def test_pinui_binui_in_the_tender_name_is_recognised(self):
        details = {"MichrazID": 1, "Tik": [{"TikID": "A", "Shetach": 900, "MechirSaf": 5_000_000}]}
        lot = next(iter(self.source(details, name="מכרז פינוי בינוי 5/2026").fetch()))
        assert lot.renewal_kind == "pinui_binui"
        assert lot.has_structure is True

    def test_demolition_note_on_the_plot_is_recognised(self):
        details = {
            "MichrazID": 1,
            "Tik": [
                {
                    "TikID": "A",
                    "Shetach": 900,
                    "MechirSaf": 5_000_000,
                    "Divur": "המגרש כולל מבנה ישן המיועד להריסה",
                }
            ],
        }
        lot = next(iter(self.source(details).fetch()))
        assert lot.renewal_kind == "demolition"

    def test_empty_plot_is_not_marked(self):
        details = {
            "MichrazID": 1,
            "Tik": [{"TikID": "A", "Shetach": 900, "ShetachBniya": 0, "MechirSaf": 5_000_000}],
        }
        lot = next(iter(self.source(details).fetch()))
        assert lot.renewal_kind is None
        assert lot.has_structure is None

    def test_renewal_lots_are_not_filtered_out(self):
        """Главное требование: такие лоты идут в сводку, а не отсеиваются."""
        details = {
            "MichrazID": 1,
            "Tik": [{"TikID": "A", "Shetach": 900, "ShetachBniya": 2000, "MechirSaf": 9_000_000}],
        }
        lots = list(self.source(details, name="פינוי בינוי").fetch())
        assert len(lots) == 1
        assert lots[0].price_nis == 9_000_000

    def test_tender_without_details_still_gets_classified(self):
        source = make_source({
            "SearchApi/Search": [{"MichrazID": 1, "MichrazName": 'מכרז תמ"א 38 בחיפה'}],
            "MichrazDetailsApi/Get": {},
        })
        lot = next(iter(source.fetch()))
        assert lot.renewal_kind == "tama_38"


class TestSettlementFilter:
    """Город портал отдаёт кодом ЦСБ, а текстом показывает только квартал."""

    SEARCH = [
        {"MichrazID": 1, "KodYeshuv": 3000, "Shchuna": "פסגת זאב"},   # Иерусалим
        {"MichrazID": 2, "KodYeshuv": 7400, "Shchuna": "קרית השרון"},  # Нетания
        {"MichrazID": 3, "KodYeshuv": 4000, "Shchuna": "נווה שאנן"},   # Хайфа
    ]

    def source(self, settlements):
        routes = {"SearchApi/Search": self.SEARCH, "MichrazDetailsApi/Get": {}}
        return make_source(routes, options={"settlements": settlements})

    def test_keeps_only_requested_cities(self):
        lots = list(self.source(["ירושלים", "נתניה"]).fetch())
        assert {lot.tender_id for lot in lots} == {"1", "2"}

    def test_city_name_is_filled_in_from_the_code(self):
        """Портал названия не даёт — подставляем то, по которому отобрали."""
        lots = {lot.tender_id: lot for lot in self.source(["ירושלים", "נתניה"]).fetch()}
        assert lots["1"].settlement == "ירושלים"
        assert lots["2"].settlement == "נתניה"

    def test_russian_spelling_works(self):
        lots = list(self.source(["Иерусалим"]).fetch())
        assert {lot.tender_id for lot in lots} == {"1"}

    def test_empty_list_disables_the_filter(self):
        assert len(list(self.source([]).fetch())) == 3

    def test_details_are_not_fetched_for_other_cities(self):
        source = self.source(["ירושלים"])
        list(source.fetch())
        detail_calls = [c for c in source.ctx.http.calls if "MichrazDetailsApi" in c[1]]
        assert len(detail_calls) == 1

    def test_tender_without_code_falls_back_to_text(self):
        routes = {
            "SearchApi/Search": [{"MichrazID": 9, "Shchuna": "מרכז ירושלים"}],
            "MichrazDetailsApi/Get": {},
        }
        source = make_source(routes, options={"settlements": ["ירושלים"]})
        assert [lot.tender_id for lot in source.fetch()] == ["9"]


class TestDeduplication:
    """Портал повторяет один и тот же участок на разных уровнях вложенности."""

    def test_same_plot_repeated_in_nested_nodes_yields_one_lot(self):
        plot = {"Gush": "555", "Chelka": "1", "Area": 900.0, "MinPrice": 1_000_000.0}
        details = {
            "MichrazID": 20250142,
            "Tik": {"Migrashim": [plot]},
            "Summary": {"Migrashim": [dict(plot)]},
            "Nested": {"Deeper": {"Plot": dict(plot)}},
        }
        routes = dict(ROUTES, **{"MichrazDetailsApi/Get": details})
        lots = [lot for lot in make_source(routes).fetch() if lot.tender_id == "20250142"]
        assert len(lots) == 1

    def test_identical_plots_without_gush_are_collapsed(self):
        plot = {"Area": 500.0, "YechidotDiur": 45, "MinPrice": 800_000.0}
        details = {"MichrazID": 20250142, "A": [dict(plot)], "B": [dict(plot)], "C": [dict(plot)]}
        routes = dict(ROUTES, **{"MichrazDetailsApi/Get": details})
        lots = [lot for lot in make_source(routes).fetch() if lot.tender_id == "20250142"]
        assert len(lots) == 1

    def test_genuinely_different_plots_are_kept_apart(self):
        details = {
            "MichrazID": 20250142,
            "Migrashim": [
                {"Area": 500.0, "YechidotDiur": 45, "MinPrice": 800_000.0},
                {"Area": 700.0, "YechidotDiur": 45, "MinPrice": 800_000.0},
                {"Area": 500.0, "YechidotDiur": 45, "MinPrice": 900_000.0},
            ],
        }
        routes = dict(ROUTES, **{"MichrazDetailsApi/Get": details})
        lots = [lot for lot in make_source(routes).fetch() if lot.tender_id == "20250142"]
        assert len(lots) == 3

    def test_same_gush_different_migrash_are_separate(self):
        details = {
            "MichrazID": 20250142,
            "Migrashim": [
                {"Gush": "555", "Chelka": "1", "MigrashNumber": "1", "MinPrice": 1_000_000.0},
                {"Gush": "555", "Chelka": "1", "MigrashNumber": "2", "MinPrice": 1_200_000.0},
            ],
        }
        routes = dict(ROUTES, **{"MichrazDetailsApi/Get": details})
        lots = [lot for lot in make_source(routes).fetch() if lot.tender_id == "20250142"]
        assert len(lots) == 2

    def test_real_tender_still_expands_to_all_its_plots(self):
        lots = [lot for lot in make_source(ROUTES).fetch() if lot.tender_id == "20250142"]
        assert len(lots) == 3

    def test_plots_come_from_tik_only_when_it_is_present(self):
        """Живой запуск удваивал каждый участок: те же данные лежат в дереве
        не только в ``Tik``, а обход брал все копии."""
        plot = {"TikID": "A", "Shetach": 900, "MechirSaf": 5_000_000}
        details = {
            "MichrazID": 20250142,
            "Tik": [plot],
            "Echo": {"Tik": [{"TikID": "B", "Shetach": 900, "MechirSaf": 5_000_000}]},
            "Mirror": {"Plot": {"TikID": "C", "Shetach": 900, "MechirSaf": 5_000_000}},
        }
        routes = dict(ROUTES, **{"MichrazDetailsApi/Get": details})
        lots = [lot for lot in make_source(routes).fetch() if lot.tender_id == "20250142"]
        assert len(lots) == 1

    def test_tree_walk_is_still_used_when_tik_is_absent(self):
        details = {
            "MichrazID": 20250142,
            "Migrashim": [{"TikID": "A", "Shetach": 900, "MechirSaf": 5_000_000}],
        }
        routes = dict(ROUTES, **{"MichrazDetailsApi/Get": details})
        lots = [lot for lot in make_source(routes).fetch() if lot.tender_id == "20250142"]
        assert len(lots) == 1
        assert lots[0].price_nis == 5_000_000


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


class TestLandUse:
    """Сельхозземля должна быть опознана и в поиске, и в деталях."""

    def test_purpose_code_for_agriculture_marks_the_lot(self):
        routes = dict(ROUTES, **{
            "SearchApi/Search": [{"MichrazID": 1, "KodYeudMichraz": 5}],
            "MichrazDetailsApi/Get": {
                "MichrazID": 1,
                "Tik": [{"TikID": "A", "Shetach": 145_000, "MechirSaf": 2_900_000}],
            },
        })
        lot = next(iter(make_source(routes).fetch()))
        assert lot.purpose == "חקלאות"
        assert lot.land_use == "agriculture"

    def test_plot_purpose_beats_tender_purpose(self):
        routes = dict(ROUTES, **{
            "SearchApi/Search": [{"MichrazID": 1, "KodYeudMichraz": 1}],
            "MichrazDetailsApi/Get": {
                "MichrazID": 1,
                "Tik": [
                    {"TikID": "A", "Yeud": "מטעים", "Shetach": 90_000, "MechirSaf": 1_200_000}
                ],
            },
        })
        assert next(iter(make_source(routes).fetch())).land_use == "agriculture"

    def test_tender_without_details_is_classified_too(self):
        routes = dict(ROUTES, **{
            "SearchApi/Search": [{"MichrazID": 1, "MichrazName": "מכרז לקרקע חקלאית"}],
            "MichrazDetailsApi/Get": {},
        })
        assert next(iter(make_source(routes).fetch())).land_use == "agriculture"

    def test_residential_lot_is_not_marked_as_farmland(self):
        lots = list(make_source(ROUTES).fetch())
        assert all(lot.land_use != "agriculture" for lot in lots)


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


class TestOpeningDate:
    """PtichaDate — когда открывается приём заявок; до неё цены у портала нет."""

    ROUTES_UNOPENED = {
        "SearchApi/Search": [{"MichrazID": 1, "PtichaDate": "2026-10-26T00:00:00+02:00",
                              "SgiraDate": "2026-12-28T12:00:00+02:00"}],
        "MichrazDetailsApi/Get": {},
    }

    def test_read_from_the_tender(self):
        lot = next(iter(make_source(self.ROUTES_UNOPENED).fetch()))
        assert lot.opening_date == "2026-10-26"
        assert lot.closing_date == "2026-12-28"

    def test_plots_inherit_it(self):
        routes = dict(self.ROUTES_UNOPENED, **{
            "MichrazDetailsApi/Get": {
                "MichrazID": 1,
                "Tik": [{"TikID": "A", "Shetach": 900, "MechirSaf": 5_000_000}],
            },
        })
        lot = next(iter(make_source(routes).fetch()))
        assert lot.opening_date == "2026-10-26"

    def test_missing_field_stays_empty(self):
        lot = next(iter(make_source(ROUTES).fetch()))
        assert lot.opening_date is None


class TestArchiveOrder:
    """Лимит деталей меньше архива, поэтому важно, с какого конца его читать.

    Портал отдаёт архив со своего края — самого старого. Три захода харвеста
    подряд выбрали 2000-2005 годы и ни одного года новее: сделок с ценой в
    базе накопилось 3340, и все они старше двадцати лет, то есть для оценки
    бесполезны. Детали добираются от свежих торгов к старым.
    """

    def test_newest_tenders_come_first(self):
        rows = [
            {"MichrazID": 1, "SgiraDate": "2004-05-01T00:00:00"},
            {"MichrazID": 2, "SgiraDate": "2026-02-01T00:00:00"},
            {"MichrazID": 3, "SgiraDate": "2015-09-01T00:00:00"},
        ]
        assert [t["MichrazID"] for t in _newest_first(rows)] == [2, 3, 1]

    def test_tenders_without_a_date_go_last(self):
        rows = [{"MichrazID": 1}, {"MichrazID": 2, "SgiraDate": "2020-01-01T00:00:00"}]
        assert [t["MichrazID"] for t in _newest_first(rows)] == [2, 1]

    def test_nothing_is_lost_in_the_reordering(self):
        rows = [{"MichrazID": i} for i in range(5)]
        assert len(_newest_first(rows)) == 5

    def test_a_tight_budget_is_spent_on_the_freshest_tender(self):
        source = make_source(ROUTES, options={"details_budget": 1})
        list(source.fetch())
        detail_calls = [c for c in source.ctx.http.calls if "MichrazDetailsApi" in c[1]]
        assert len(detail_calls) == 1
        requested = detail_calls[0][2]["params"]["michrazID"]

        search = load_fixture("rmi_search.json")
        newest = _newest_first([t for t in search if isinstance(t, dict)])[0]
        assert str(requested) == str(newest["MichrazID"])
