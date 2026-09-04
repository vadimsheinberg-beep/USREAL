"""Реестр планов (מבא"ת / iplan): что собираются построить на участке.

Стоимость израильской сельхозземли определяется одним обстоятельством —
сменой назначения. Поле стоит дёшево ровно до тех пор, пока план не переводит
его под застройку; после утверждения цена другая. Поэтому земельный лот без
ответа на вопрос «какие планы его накрывают и на какой они стадии» оценить
нельзя.

Схема снята разведкой (``landtender inspect --service iplan``). Сервис —
обычный ArcGIS REST, но требует старого набора шифров: на современный
ClientHello отвечает ``SSLV3_ALERT_HANDSHAKE_FAILURE``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .http import HttpClient, HttpError
from .mapping import (
    IPLAN_LANDUSE_FIELDS,
    IPLAN_LANDUSE_LAYER,
    IPLAN_PLAN_FIELDS,
    IPLAN_PLANS_LAYER,
    IPLAN_XPLAN,
)

log = logging.getLogger(__name__)

#: Стадии плана от ранней к поздней. Чем позже стадия, тем ближе смена
#: назначения к реальности — и тем меньше остаётся дисконт на риск.
STAGE_UNKNOWN = "unknown"
STAGE_SUBMITTED = "submitted"  # תכנית התקבלה, идёт проверка
STAGE_DEPOSITED = "deposited"  # פרסום הפקדה — опубликована для возражений
STAGE_APPROVED = "approved"  # פרסום אישור — утверждена
STAGE_REJECTED = "rejected"  # נדחתה

STAGE_TITLES = {
    STAGE_SUBMITTED: "подана",
    STAGE_DEPOSITED: "депонирована",
    STAGE_APPROVED: "утверждена",
    STAGE_REJECTED: "отклонена",
    STAGE_UNKNOWN: "стадия неизвестна",
}

#: Порядок для сравнения «какая стадия дальше продвинулась».
STAGE_ORDER = {
    STAGE_REJECTED: -1,
    STAGE_UNKNOWN: 0,
    STAGE_SUBMITTED: 1,
    STAGE_DEPOSITED: 2,
    STAGE_APPROVED: 3,
}

_APPROVED = re.compile(r"אישור|מאושרת|תוקף")
_DEPOSITED = re.compile(r"הפקדה|מופקדת")
_REJECTED = re.compile(r"נדחת|דחייה|בוטל")
_SUBMITTED = re.compile(r"בבדיקה|נקלט|הוגש|תנאי\s+סף")

#: «Изменение назначения земли» — формулировка, которой планы описывают
#: перевод участка из одной категории в другую. Ради неё всё и читается.
_REZONING = re.compile(r"שינוי\s+(?:ב)?מערך\s+יעודי\s+הקרקע|שינוי\s+ייעוד|שינוי\s+יעוד")

#: Из чего переводят. Сельхоз в этом списке — то, что нам интересно.
_FROM_AGRICULTURE = re.compile(r"מחקלא|מקרקע\s+חקלאית|משטח\s+חקלאי|מאזור\s+חקלאי")


@dataclass(frozen=True)
class Plan:
    """План застройки в том виде, в каком его отдаёт реестр."""

    number: str | None = None
    name: str | None = None
    url: str | None = None
    mp_id: str | None = None

    land_use: str | None = None  # סוג ייעוד קרקע
    objectives: str | None = None  # מטרות — полный текст
    stage: str = STAGE_UNKNOWN
    status_text: str | None = None
    plan_type: str | None = None  # תת-סוג תכנית
    settlement: str | None = None
    district: str | None = None

    area_dunam: float | None = None
    #: Насколько план меняет число квартир. У перевода поля под застройку
    #: это число большое; у уточнения линии застройки — ноль.
    units_delta: int | None = None
    units_authorised: int | None = None
    #: Изменение жилой площади в м².
    housing_sqm_delta: float | None = None

    deposited_date: str | None = None
    advertised_date: str | None = None
    received_date: str | None = None

    @property
    def rezones(self) -> bool:
        """Меняет ли план назначение земли (а не только линию застройки)."""
        return bool(self.objectives and _REZONING.search(self.objectives))

    @property
    def rezones_from_agriculture(self) -> bool:
        """Переводит ли план землю именно из сельхозназначения."""
        return bool(self.objectives and _FROM_AGRICULTURE.search(self.objectives))

    @property
    def label(self) -> str:
        bits = [b for b in (self.number, self.name) if b]
        return " · ".join(bits) if bits else (self.mp_id or "план")


@dataclass(frozen=True)
class LandUse:
    """Ячейка назначения земли из слоя ``יעודי קרקע``."""

    mavat_code: int | None = None
    mavat_name: str | None = None  # «קרקע חקלאית», «מגורים א'»…
    legal_area_dunam: float | None = None
    plan_number: str | None = None
    plan_name: str | None = None
    status_text: str | None = None


class IplanRegistry:
    """Запросы к реестру планов по координате или кадастровому номеру."""

    def __init__(self, http: HttpClient, base_url: str = IPLAN_XPLAN) -> None:
        self.http = http
        self.base_url = base_url
        # Сервер отвечает только на старый набор шифров; включаем точечно.
        http.use_legacy_tls("https://ags.iplan.gov.il")

    # ------------------------------------------------------------- планы ----

    def plans_at(self, x: float, y: float, limit: int = 20) -> list[Plan]:
        """Планы, накрывающие точку. Координаты — в веб-меркаторе (3857)."""
        rows = self._query(
            IPLAN_PLANS_LAYER,
            IPLAN_PLAN_FIELDS,
            geometry=f"{x},{y}",
            geometry_type="esriGeometryPoint",
            limit=limit,
        )
        return [_to_plan(row) for row in rows]

    def plans_where(self, where: str, limit: int = 20) -> list[Plan]:
        """Планы по произвольному условию ArcGIS — для поиска по городу."""
        rows = self._query(IPLAN_PLANS_LAYER, IPLAN_PLAN_FIELDS, where=where, limit=limit)
        return [_to_plan(row) for row in rows]

    # -------------------------------------------------------- назначения ----

    def land_use_at(self, x: float, y: float, limit: int = 10) -> list[LandUse]:
        """Назначения земли в точке: что здесь разрешено сейчас."""
        rows = self._query(
            IPLAN_LANDUSE_LAYER,
            IPLAN_LANDUSE_FIELDS,
            geometry=f"{x},{y}",
            geometry_type="esriGeometryPoint",
            limit=limit,
        )
        return [
            LandUse(
                mavat_code=_as_int(row.get("mavat_code")),
                mavat_name=_clean(row.get("mavat_name")),
                legal_area_dunam=_as_float(row.get("legal_area")),
                plan_number=_clean(row.get("pl_number")),
                plan_name=_clean(row.get("pl_name")),
                status_text=_clean(row.get("station_desc")),
            )
            for row in rows
        ]

    # ---------------------------------------------------------------- API ---

    def _query(
        self,
        layer: int,
        fields: Iterable[str],
        where: str | None = None,
        geometry: str | None = None,
        geometry_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "f": "json",
            "outFields": ",".join(fields),
            "returnGeometry": "false",
            "resultRecordCount": limit,
            "where": where or "1=1",
        }
        if geometry:
            params.update(
                {
                    "geometry": geometry,
                    "geometryType": geometry_type or "esriGeometryPoint",
                    "inSR": 3857,
                    "spatialRel": "esriSpatialRelIntersects",
                }
            )
        try:
            data = self.http.get_json(f"{self.base_url}/{layer}/query", params=params)
        except HttpError as exc:
            log.warning("iplan: слой %s недоступен: %s", layer, exc)
            return []

        if isinstance(data, dict) and data.get("error"):
            log.warning("iplan: слой %s вернул ошибку: %s", layer, data["error"])
            return []
        return [f.get("attributes") or {} for f in (data or {}).get("features") or []]

    def probe(self) -> str:
        rows = self._query(IPLAN_PLANS_LAYER, ("pl_number",), limit=1)
        return f"реестр планов отвечает, записей в пробе: {len(rows)}"


# ------------------------------------------------------------- разбор ------


def _to_plan(row: dict[str, Any]) -> Plan:
    return Plan(
        number=_clean(row.get("pl_number")),
        name=_clean(row.get("pl_name")),
        url=_clean(row.get("pl_url")),
        mp_id=_id(row.get("mp_id")),
        land_use=_clean(row.get("pl_landuse_string")),
        objectives=_clean(row.get("pl_objectives")),
        stage=classify_stage(row.get("internet_short_status"), row.get("station_desc")),
        status_text=_clean(row.get("internet_short_status")) or _clean(row.get("station_desc")),
        plan_type=_clean(row.get("entity_subtype_desc")),
        settlement=_clean(row.get("plan_county_name")),
        district=_clean(row.get("district_name")),
        area_dunam=_as_float(row.get("pl_area_dunam")),
        units_delta=_as_int(row.get("quantity_delta_120")),
        units_authorised=_as_int(row.get("pq_authorised_quantity_120")),
        housing_sqm_delta=_as_float(row.get("quantity_delta_125")),
        deposited_date=_epoch_date(row.get("pl_last_deposit_date") or row.get("depositing_date")),
        advertised_date=_epoch_date(row.get("pl_date_advertise") or row.get("pl_date_8")),
        received_date=_epoch_date(row.get("receiving_date")),
    )


def classify_stage(*texts: Any) -> str:
    """Стадия плана по ивритским подписям статуса.

    Отклонение проверяется первым: «נדחתה» встречается вместе со словами об
    утверждении, и спутать эти два состояния хуже всего.
    """
    haystack = " ".join(str(t) for t in texts if t)
    if not haystack.strip():
        return STAGE_UNKNOWN
    if _REJECTED.search(haystack):
        return STAGE_REJECTED
    if _APPROVED.search(haystack):
        return STAGE_APPROVED
    if _DEPOSITED.search(haystack):
        return STAGE_DEPOSITED
    if _SUBMITTED.search(haystack):
        return STAGE_SUBMITTED
    return STAGE_UNKNOWN


def _epoch_date(value: Any) -> str | None:
    """ArcGIS отдаёт даты миллисекундами эпохи."""
    try:
        millis = float(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _id(value: Any) -> str | None:
    """``mp_id`` приходит дробным (1000216487.0) — в ссылке нужен целый."""
    number = _as_int(value)
    return str(number) if number else None


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number else None
