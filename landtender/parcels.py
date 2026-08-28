"""Участки кадастра: гуш/хелка → площадь, город, полигон (govmap).

Зачем: тендер рм"י сплошь и рядом не сообщает площадь участка. У тендера
21/2020 портал отдал ``Shetach: 1`` — один квадратный метр сельхозполя, что
очевидно неправда, а у 406/2025 площадь пустая совсем. При этом гуш и хелка
в тендере есть, а по ним площадь берётся из кадастра.

Схема получена разведкой (``landtender inspect --service govmap``), поля не
угаданы: ``GUSH_NUM``, ``PARCEL``, ``LEGAL_AREA``, ``LOCALITY_N``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

#: Открытый WFS govmap. Авторизация не нужна, отвечает GeoJSON.
WFS_URL = "https://open.govmap.gov.il/geoserver/opendata/wfs"

#: Слой кадастровых участков. Соседние типы того же сервиса:
#: ``SUB_GUSH_ALL`` (гуши), ``nechalim1`` (нахалот), ``muni_il`` (муниципалитеты).
PARCEL_LAYER = "opendata:PARCEL_ALL"

#: Сервис отвечает в EPSG:3857 — это веб-меркатор, для ссылки на карту
#: пересчёт не нужен, а площадь берём из атрибута, а не из геометрии.
DEFAULT_CRS = "EPSG:3857"


@dataclass(frozen=True)
class Parcel:
    """Кадастровый участок в том виде, в каком его знает govmap."""

    gush: str
    chelka: str
    #: Зарегистрированная площадь в м². Сам портал предупреждает, что это
    #: справка, а не юридическая выписка, — поэтому и мы её так подаём.
    legal_area_sqm: float | None = None
    #: Вычисленная по контуру площадь: расходится с записанной на единицы м².
    shape_area_sqm: float | None = None
    settlement: str | None = None
    county: str | None = None
    region: str | None = None
    status: str | None = None

    @property
    def area_sqm(self) -> float | None:
        """Площадь, которой стоит верить: записанная, иначе — по контуру."""
        return self.legal_area_sqm or self.shape_area_sqm

    @property
    def dunam(self) -> float | None:
        area = self.area_sqm
        return area / 1000 if area else None


class GovmapParcels:
    """Поиск участков по кадастровым номерам."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def find(self, gush: str | int, chelka: str | int) -> Parcel | None:
        """Участок по гуш/хелка, либо ``None``, если такого нет.

        Оба номера должны быть числами: CQL-фильтр сравнивает их с числовыми
        колонками, и подстановка текста уронила бы запрос целиком.
        """
        gush_num = _as_int(gush)
        chelka_num = _as_int(chelka)
        if gush_num is None or chelka_num is None:
            return None

        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": PARCEL_LAYER,
            "outputFormat": "application/json",
            "count": "1",
            "cql_filter": f"GUSH_NUM={gush_num} AND PARCEL={chelka_num}",
        }
        try:
            data = self.http.get_json(WFS_URL, params=params)
        except HttpError as exc:
            log.warning("govmap: участок %s/%s недоступен: %s", gush_num, chelka_num, exc)
            return None

        features = (data or {}).get("features") or []
        if not features:
            return None
        return _to_parcel(features[0].get("properties") or {}, gush_num, chelka_num)


def _to_parcel(props: dict[str, Any], gush: int, chelka: int) -> Parcel:
    return Parcel(
        gush=str(gush),
        chelka=str(chelka),
        legal_area_sqm=_as_float(props.get("LEGAL_AREA")),
        shape_area_sqm=_as_float(props.get("SHAPE_AREA")),
        settlement=_clean(props.get("LOCALITY_N")),
        county=_clean(props.get("COUNTY_NAM")),
        region=_clean(props.get("REGION_NAM")),
        status=_clean(props.get("STATUS_TEX")),
    )


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
