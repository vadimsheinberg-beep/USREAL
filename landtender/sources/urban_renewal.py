"""Городское обновление: площадки со старыми домами под снос и реконструкцию.

Правительственное управление городского обновления
(הרשות הממשלתית להתחדשות עירונית) ведёт реестр объявленных комплексов
פינוי בינוי и проектов תמ"א 38 — это именно застроенные участки, где стоят
старые дома, а не пустая земля, которой торгует рм"י.

Данные берутся через CKAN портала открытых данных: протокол стандартный и
уже используется в проекте. Отдельный источник, а не запрос внутри
``data_gov_il``, потому что здесь другой смысл записи — не лот на торгах, а
объявленный комплекс, и цены у него, как правило, нет вовсе.

Замечание о полноте: карта управления опубликована ещё и слоем 200720 на
govmap.gov.il. Слой богаче, но у govmap нет документированного API, поэтому
здесь он не используется.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ..extract import (
    AREA_KEYS,
    CLOSING_KEYS,
    NEIGHBORHOOD_KEYS,
    PRICE_APPRAISAL_KEYS,
    PRICE_FINAL_KEYS,
    PRICE_MIN_KEYS,
    PUBLISHED_KEYS,
    REGION_KEYS,
    SETTLEMENT_KEYS,
    STATUS_KEYS,
    TENDER_NAME_KEYS,
    UNITS_KEYS,
    clean_text,
    pick,
    to_float,
    to_int,
    to_iso_date,
)
from ..http import HttpError
from ..models import Lot
from ..money import choose_price
from ..renewal import EXISTING_STRUCTURE, PINUI_BINUI, classify_text
from .base import Source
from .data_gov_il import DATASET_PAGE, DATASTORE_SEARCH_URL, PACKAGE_SEARCH_URL

log = logging.getLogger(__name__)

DEFAULT_QUERIES = (
    "התחדשות עירונית",
    "פינוי בינוי",
    "מתחמי התחדשות עירונית",
)

#: Название комплекса — у реестра обновления свои заголовки колонок.
COMPLEX_NAME_KEYS = (
    "שם המתחם",
    "שם מתחם",
    "שם הפרויקט",
    "מתחם",
    "ComplexName",
    "SiteName",
) + TENDER_NAME_KEYS

#: Существующие единицы жилья — прямое свидетельство того, что дома уже стоят.
EXISTING_UNITS_KEYS = (
    "יחידות דיור קיימות",
    'יח"ד קיימות',
    "מספר יחידות קיימות",
    "ExistingUnits",
    "YechidotKayamot",
)

#: Планируемые единицы — сколько будет после стройки.
PLANNED_UNITS_KEYS = (
    "יחידות דיור מתוכננות",
    'יח"ד מתוכננות',
    "יחידות דיור בתכנון",
    "PlannedUnits",
) + UNITS_KEYS

#: Маршрут проекта: פינוי בינוי, תמ"א 38 и т.п.
TRACK_KEYS = ("מסלול", "סוג מתחם", "מסלול תכנוני", "Track", "ProjectType")


class UrbanRenewalSource(Source):
    name = "urban_renewal"
    title = "Управление городского обновления — комплексы под реконструкцию"
    kind = "government"

    def fetch(self) -> Iterable[Lot]:
        queries = self.ctx.option("queries") or list(DEFAULT_QUERIES)
        max_rows = int(self.ctx.option("max_rows", 2000))

        seen_resources: set[str] = set()
        produced = 0

        for query in queries:
            try:
                resources = self._resources_for(query)
            except HttpError as exc:
                log.warning("Обновление: поиск наборов по %r не удался: %s", query, exc)
                continue

            for resource_id, package_name in resources:
                if resource_id in seen_resources:
                    continue
                seen_resources.add(resource_id)

                try:
                    records = self._records(resource_id, max_rows - produced)
                except HttpError as exc:
                    log.warning("Обновление: набор %s недоступен: %s", resource_id, exc)
                    continue

                for row in records:
                    lot = _row_to_lot(row, resource_id, package_name)
                    if lot is None:
                        continue
                    produced += 1
                    yield lot
                    if produced >= max_rows:
                        return

    # --------------------------------------------------------------- CKAN ---

    def _resources_for(self, query: str) -> list[tuple[str, str]]:
        data = self.ctx.http.get_json(PACKAGE_SEARCH_URL, params={"q": query, "rows": 20})
        results = (data or {}).get("result", {}).get("results", [])
        out: list[tuple[str, str]] = []
        for package in results:
            name = package.get("name") or package.get("title") or ""
            haystack = f"{name} {package.get('title', '')} {package.get('notes', '')}"
            # Поиск CKAN нечёткий и приносит лишнее — оставляем то, что
            # действительно про обновление
            if not _is_renewal_dataset(haystack):
                continue
            for resource in package.get("resources", []) or []:
                if resource.get("datastore_active") and resource.get("id"):
                    out.append((resource["id"], name))
        return out

    def _records(self, resource_id: str, limit: int) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0
        while len(records) < limit:
            batch_size = min(1000, limit - len(records))
            data = self.ctx.http.get_json(
                DATASTORE_SEARCH_URL,
                params={"resource_id": resource_id, "limit": batch_size, "offset": offset},
            )
            batch = (data or {}).get("result", {}).get("records", [])
            if not batch:
                break
            records.extend(r for r in batch if isinstance(r, dict))
            offset += len(batch)
            if len(batch) < batch_size:
                break
        return records

    def probe(self) -> str:
        found = self._resources_for(DEFAULT_QUERIES[0])
        return f"наборов по городскому обновлению: {len(found)}"


# ------------------------------------------------------------------ разбор --


def _is_renewal_dataset(text: str) -> bool:
    """Набор действительно про городское обновление?"""
    return classify_text(text) is not None


def _row_to_lot(row: dict[str, Any], resource_id: str, package_name: str) -> Lot | None:
    """Запись реестра → лот.

    В отличие от торгов рм"י здесь не требуется цена: комплекс интересен сам
    по себе. Обязателен только населённый пункт — без него запись бесполезна.
    """
    settlement = clean_text(pick(row, SETTLEMENT_KEYS))
    name = clean_text(pick(row, COMPLEX_NAME_KEYS))
    if not settlement and not name:
        return None

    row_id = row.get("_id") or name or ""
    if not row_id:
        return None

    existing_units = to_int(pick(row, EXISTING_UNITS_KEYS))
    planned_units = to_int(pick(row, PLANNED_UNITS_KEYS))
    track = clean_text(pick(row, TRACK_KEYS))

    # Всё в этом реестре стоит на застроенной земле — вопрос лишь в том,
    # какой именно маршрут. Если текст молчит, считаем расселением.
    kind = classify_text(track, name, str(row)) or (
        PINUI_BINUI if existing_units else EXISTING_STRUCTURE
    )

    prices = {
        "final": to_float(pick(row, PRICE_FINAL_KEYS)),
        "min": to_float(pick(row, PRICE_MIN_KEYS)),
        "appraisal": to_float(pick(row, PRICE_APPRAISAL_KEYS)),
    }
    price_nis, price_kind = choose_price(prices, ["final", "min", "appraisal"])

    return Lot(
        source=UrbanRenewalSource.name,
        source_id=f"{resource_id}:{row_id}",
        # Без названия оставляем нейтральную подпись: город добавит label,
        # иначе он печатается дважды.
        tender_name=name or "מתחם התחדשות עירונית",
        url=f"{DATASET_PAGE}?q={package_name}",
        settlement=settlement,
        neighborhood=clean_text(pick(row, NEIGHBORHOOD_KEYS)),
        region=clean_text(pick(row, REGION_KEYS)),
        purpose=track or "התחדשות עירונית",
        status=clean_text(pick(row, STATUS_KEYS)),
        area_sqm=to_float(pick(row, AREA_KEYS)),
        units=planned_units or existing_units,
        units_basis="reported" if (planned_units or existing_units) else None,
        renewal_kind=kind,
        has_structure=True,
        price_nis=price_nis,
        price_kind=price_kind,
        published_date=to_iso_date(pick(row, PUBLISHED_KEYS)),
        closing_date=to_iso_date(pick(row, CLOSING_KEYS)),
        raw=row,
    )
