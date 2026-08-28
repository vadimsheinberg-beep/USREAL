"""Связка кадастра и реестра планов: инвестиционный признак участка."""

import pytest

from landtender.invest import (
    MIN_CREDIBLE_AREA_SQM,
    SIGNAL_CONFIRMED,
    SIGNAL_EARLY,
    SIGNAL_LIKELY,
    SIGNAL_NONE,
    Enricher,
    Insight,
    apply,
    rank_signal,
)
from landtender.landuse import AGRICULTURE
from landtender.models import Lot
from landtender.parcels import GovmapParcels, Parcel
from landtender.plans import (
    STAGE_APPROVED,
    STAGE_DEPOSITED,
    STAGE_SUBMITTED,
    IplanRegistry,
    LandUse,
    Plan,
)
from tests.conftest import FakeHttp


def plan(objectives="שינוי במערך יעודי הקרקע", stage=STAGE_DEPOSITED, **kw):
    return Plan(objectives=objectives, stage=stage, **kw)


class TestRankSignal:
    def test_no_plans_no_signal(self):
        assert rank_signal([], agricultural=True) == (SIGNAL_NONE, None)

    def test_building_line_shift_is_not_a_signal(self):
        """План, двигающий линию застройки на два метра, цену земли не меняет."""
        line = plan(objectives="שינוי קו בנין צידי", stage=STAGE_APPROVED)
        assert rank_signal([line], agricultural=True)[0] == SIGNAL_NONE

    @pytest.mark.parametrize(
        "stage, expected",
        [
            (STAGE_SUBMITTED, SIGNAL_EARLY),
            (STAGE_DEPOSITED, SIGNAL_LIKELY),
            (STAGE_APPROVED, SIGNAL_CONFIRMED),
        ],
    )
    def test_stage_becomes_signal(self, stage, expected):
        assert rank_signal([plan(stage=stage)], agricultural=True)[0] == expected

    def test_strongest_stage_wins(self):
        weak, strong = plan(stage=STAGE_SUBMITTED), plan(stage=STAGE_APPROVED)
        signal, leader = rank_signal([weak, strong], agricultural=True)
        assert signal == SIGNAL_CONFIRMED
        assert leader is strong

    def test_agricultural_rezoning_wins_at_equal_stage(self):
        urban = plan(objectives="שינוי ייעוד ממסחר למגורים", number="A")
        farm = plan(objectives="שינוי ייעוד מקרקע חקלאית למגורים", number="B")
        _, leader = rank_signal([urban, farm], agricultural=True)
        assert leader.number == "B"

    def test_more_units_breaks_the_tie(self):
        small = plan(number="A", units_delta=2)
        big = plan(number="B", units_delta=400)
        _, leader = rank_signal([small, big], agricultural=True)
        assert leader.number == "B"

    def test_rejected_plan_gives_no_signal(self):
        from landtender.plans import STAGE_REJECTED

        assert rank_signal([plan(stage=STAGE_REJECTED)], agricultural=True)[0] == SIGNAL_NONE


class TestApply:
    def lot(self, **kw):
        return Lot(source="rmi_michrazim", source_id="1", **kw)

    def parcel(self, **kw):
        data = dict(gush="10223", chelka="100", legal_area_sqm=1000.0)
        data.update(kw)
        return Parcel(**data)

    def test_nonsense_area_is_replaced(self):
        """У тендера 21/2020 портал отдал «1 м²» сельхозполя."""
        lot = self.lot(area_sqm=1.0)
        apply(lot, Insight(parcel=self.parcel()))
        assert lot.area_sqm == 1000.0

    def test_missing_area_is_filled(self):
        lot = self.lot()
        apply(lot, Insight(parcel=self.parcel()))
        assert lot.area_sqm == 1000.0

    def test_credible_area_from_the_tender_is_kept(self):
        """Источник остаётся хозяином своих данных."""
        lot = self.lot(area_sqm=4200.0)
        apply(lot, Insight(parcel=self.parcel()))
        assert lot.area_sqm == 4200.0

    def test_threshold_is_where_it_says(self):
        lot = self.lot(area_sqm=MIN_CREDIBLE_AREA_SQM + 1)
        apply(lot, Insight(parcel=self.parcel()))
        assert lot.area_sqm == MIN_CREDIBLE_AREA_SQM + 1

    def test_settlement_is_filled_but_not_overwritten(self):
        lot = self.lot(settlement="חיפה")
        apply(lot, Insight(parcel=self.parcel(settlement="בנימינה")))
        assert lot.settlement == "חיפה"

        empty = self.lot()
        apply(empty, Insight(parcel=self.parcel(settlement="בנימינה")))
        assert empty.settlement == "בנימינה"

    def test_without_parcel_nothing_changes(self):
        lot = self.lot(area_sqm=1.0)
        apply(lot, Insight())
        assert lot.area_sqm == 1.0


class TestInsight:
    def test_current_use_takes_the_first_named(self):
        insight = Insight(land_uses=[LandUse(), LandUse(mavat_name="קרקע חקלאית")])
        assert insight.current_use == "קרקע חקלאית"

    def test_units_ahead_sums_plans(self):
        insight = Insight(plans=[plan(units_delta=100), plan(units_delta=50), plan()])
        assert insight.units_ahead == 150

    def test_units_ahead_is_none_without_data(self):
        assert Insight(plans=[plan()]).units_ahead is None


PARCEL_RESPONSE = {
    "features": [
        {
            "properties": {"LEGAL_AREA": 145000, "LOCALITY_N": "בנימינה-גבעת עדה"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 20], [0, 20]]]},
        }
    ]
}

PLAN_RESPONSE = {
    "features": [
        {
            "attributes": {
                "pl_number": "353-0061416",
                "pl_objectives": "שינוי ייעוד מקרקע חקלאית למגורים",
                "internet_short_status": "פרסום הפקדה",
                "quantity_delta_120": 400.0,
            }
        }
    ]
}


class TestEnricher:
    def build(self, routes=None, **kw):
        http = FakeHttp(
            routes
            or {
                "geoserver/opendata/wfs": PARCEL_RESPONSE,
                "Xplan/MapServer": PLAN_RESPONSE,
            }
        )
        return Enricher(GovmapParcels(http), IplanRegistry(http), **kw), http

    def lot(self, **kw):
        data = dict(source="rmi_michrazim", source_id="1", gush="10223", chelka="100")
        data.update(kw)
        return Lot(**data)

    def test_end_to_end(self):
        enricher, _ = self.build()
        insight = enricher.enrich(self.lot(land_use=AGRICULTURE))
        assert insight.parcel.legal_area_sqm == 145000.0
        assert insight.signal == SIGNAL_LIKELY
        assert insight.leading_plan.number == "353-0061416"

    def test_lot_without_cadastral_number_is_skipped(self):
        enricher, http = self.build()
        assert enricher.enrich(self.lot(gush=None, chelka=None)) is None
        assert http.calls == []

    def test_budget_stops_the_work(self):
        enricher, _ = self.build(budget=1)
        assert enricher.enrich(self.lot()) is not None
        assert enricher.enrich(self.lot()) is None

    def test_only_agricultural_filter(self):
        enricher, _ = self.build(only_agricultural=True)
        assert enricher.enrich(self.lot(land_use="residential")) is None
        assert enricher.enrich(self.lot(land_use=AGRICULTURE)) is not None

    def test_budget_is_not_spent_on_skipped_lots(self):
        enricher, _ = self.build(budget=1, only_agricultural=True)
        enricher.enrich(self.lot(land_use="residential"))
        assert enricher.enrich(self.lot(land_use=AGRICULTURE)) is not None

    def test_unknown_parcel_gives_nothing(self):
        enricher, _ = self.build({"geoserver": {"features": []}, "Xplan": PLAN_RESPONSE})
        assert enricher.enrich(self.lot()) is None

    def test_parcel_without_geometry_skips_the_plan_query(self):
        routes = {
            "geoserver": {"features": [{"properties": {"LEGAL_AREA": 500}}]},
            "Xplan": PLAN_RESPONSE,
        }
        enricher, http = self.build(routes)
        insight = enricher.enrich(self.lot())
        assert insight.plans == []
        assert not any("Xplan" in url for _, url, _ in http.calls)
