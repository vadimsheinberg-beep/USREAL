"""Реестр планов iplan: стадии, смена назначения, разбор полей."""

import pytest

from landtender.http import HttpError
from landtender.plans import (
    STAGE_APPROVED,
    STAGE_DEPOSITED,
    STAGE_REJECTED,
    STAGE_SUBMITTED,
    STAGE_UNKNOWN,
    IplanRegistry,
    Plan,
    classify_stage,
)
from tests.conftest import FakeHttp

#: Настоящая запись из слоя планов (снята разведкой).
JERUSALEM_PLAN = {
    "pl_number": "101-0057273",
    "pl_name": "תוספת קומה והרחבת יח\"ד, ברח' שמואל הנביא 107, ירושלים",
    "pl_url": "https://mavat.iplan.gov.il/SV4/1/1000216487/310",
    "mp_id": 1000216487.0,
    "pl_landuse_string": "מגורים ג'",
    "pl_objectives": "א.\tשינוי במערך יעודי הקרקע כמפורט להלן : מאזור מגורים מיוחד למגורים ג'.",
    "internet_short_status": "פרסום הפקדה",
    "station_desc": "בבדיקה תכנונית",
    "entity_subtype_desc": "תכנית מתאר מקומית",
    "plan_county_name": "ירושלים",
    "district_name": "ירושלים",
    "pl_area_dunam": 2.201,
    "quantity_delta_120": 0.0,
    "pq_authorised_quantity_120": 60.0,
    "quantity_delta_125": 1129.9,
    "pl_last_deposit_date": 1436227200000,
    "pl_date_advertise": 1431043200000,
    "receiving_date": 1430784000000,
}


def registry(rows, layer_errors=None):
    payload = {"features": [{"attributes": row} for row in rows]}
    return IplanRegistry(FakeHttp({"Xplan/MapServer": layer_errors or payload}))


class TestStage:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("פרסום אישור", STAGE_APPROVED),
            ("אישור", STAGE_APPROVED),
            ("פרסום הפקדה", STAGE_DEPOSITED),
            ("תכנית מופקדת", STAGE_DEPOSITED),
            ("בבדיקה תכנונית", STAGE_SUBMITTED),
            ("עמידה בתנאי סף", STAGE_SUBMITTED),
            ("התכנית נדחתה", STAGE_REJECTED),
            ("בוטלה", STAGE_REJECTED),
        ],
    )
    def test_hebrew_statuses(self, text, expected):
        assert classify_stage(text) == expected

    def test_rejection_wins_over_approval(self):
        """«נדחתה» рядом со словами об утверждении — план всё-таки отклонён."""
        assert classify_stage("אישור בוטל, התכנית נדחתה") == STAGE_REJECTED

    def test_unknown_stays_unknown(self):
        assert classify_stage(None) == STAGE_UNKNOWN
        assert classify_stage("") == STAGE_UNKNOWN
        assert classify_stage("משהו אחר") == STAGE_UNKNOWN

    def test_several_fields_are_read_together(self):
        assert classify_stage(None, "פרסום אישור") == STAGE_APPROVED


class TestParsePlan:
    def plan(self, **overrides):
        return registry([dict(JERUSALEM_PLAN, **overrides)]).plans_where("1=1")[0]

    def test_reads_identity(self):
        plan = self.plan()
        assert plan.number == "101-0057273"
        assert plan.settlement == "ירושלים"
        assert plan.url.endswith("/1000216487/310")

    def test_mp_id_loses_its_fraction(self):
        """ArcGIS отдаёт mp_id дробным; в ссылке нужен целый."""
        assert self.plan().mp_id == "1000216487"

    def test_stage_from_status(self):
        assert self.plan().stage == STAGE_DEPOSITED

    def test_dates_come_from_epoch_millis(self):
        plan = self.plan()
        assert plan.deposited_date == "2015-07-07"
        assert plan.advertised_date == "2015-05-08"

    def test_quantities(self):
        plan = self.plan()
        assert plan.units_authorised == 60
        assert plan.housing_sqm_delta == 1129.9
        assert plan.area_dunam == 2.201

    def test_empty_dates_stay_none(self):
        assert self.plan(pl_last_deposit_date=None, pl_date_advertise=0).deposited_date is None


class TestRezoning:
    """Смена назначения — единственное, что делает дешёвую землю дорогой."""

    def test_objectives_mentioning_rezoning(self):
        assert Plan(objectives="שינוי במערך יעודי הקרקע").rezones is True

    def test_short_form_is_recognised(self):
        assert Plan(objectives="שינוי ייעוד מחקלאי למגורים").rezones is True

    def test_line_shift_is_not_rezoning(self):
        plan = Plan(objectives="שינוי קו בנין צידי לשם הכשרת חריגה מקומית")
        assert plan.rezones is False

    def test_from_agriculture_is_detected(self):
        assert Plan(objectives="שינוי ייעוד מקרקע חקלאית למגורים").rezones_from_agriculture is True
        assert Plan(objectives="מאזור חקלאי למגורים").rezones_from_agriculture is True

    def test_rezoning_between_urban_uses_is_not_agricultural(self):
        plan = Plan(objectives="שינוי במערך יעודי הקרקע: מאזור מגורים מיוחד למגורים ג'")
        assert plan.rezones is True
        assert plan.rezones_from_agriculture is False

    def test_empty_objectives(self):
        assert Plan().rezones is False
        assert Plan().rezones_from_agriculture is False


class TestQueries:
    def test_point_query_sends_geometry(self):
        http = FakeHttp({"Xplan/MapServer": {"features": []}})
        IplanRegistry(http).plans_at(3875000.0, 3760000.0)
        params = http.calls[0][2]["params"]
        assert params["geometry"] == "3875000.0,3760000.0"
        assert params["geometryType"] == "esriGeometryPoint"
        assert params["inSR"] == 3857

    def test_legacy_tls_is_enabled_for_the_host(self):
        """Иначе сервер рвёт рукопожатие и слой недоступен целиком."""
        http = FakeHttp({"Xplan": {"features": []}})
        IplanRegistry(http)
        assert http.legacy_tls_hosts == ["https://ags.iplan.gov.il"]

    def test_land_use_layer(self):
        rows = [{"mavat_name": "קרקע חקלאית", "legal_area": 12.5, "mavat_code": 51}]
        uses = registry(rows).land_use_at(1.0, 2.0)
        assert uses[0].mavat_name == "קרקע חקלאית"
        assert uses[0].legal_area_dunam == 12.5

    def test_service_error_is_not_fatal(self):
        http = FakeHttp({"Xplan": {"error": {"code": 400, "message": "Invalid"}}})
        assert IplanRegistry(http).plans_where("1=1") == []

    def test_network_failure_is_not_fatal(self):
        http = FakeHttp({"Xplan": HttpError("HTTP 500")})
        assert IplanRegistry(http).plans_at(1.0, 2.0) == []
