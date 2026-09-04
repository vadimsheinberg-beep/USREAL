"""Открытые данные правительства Израиля — data.gov.il (CKAN).

Портал отдаёт наборы данных разных ведомств: тендеры рм"י, сделки с землёй,
планы застройки. Схемы у наборов разные, поэтому колонки распознаём по
синонимам, а не по фиксированному списку.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ..extract import (
    AREA_KEYS,
    CHELKA_KEYS,
    CLOSING_KEYS,
    DEVELOPMENT_KEYS,
    GUSH_KEYS,
    NEIGHBORHOOD_KEYS,
    PRICE_APPRAISAL_KEYS,
    PRICE_ASKING_KEYS,
    PRICE_FINAL_KEYS,
    PRICE_MIN_KEYS,
    PUBLISHED_KEYS,
    PURPOSE_KEYS,
    REGION_KEYS,
    SETTLEMENT_KEYS,
    STATUS_KEYS,
    TENDER_NAME_KEYS,
    clean_text,
    looks_like_lot,
    pick,
    to_float,
    to_iso_date,
)
from ..http import HttpError
from ..landuse import classify_lot as classify_landuse
from ..models import Lot
from ..money import choose_price
from ..units import resolve_units
from .base import Source

log = logging.getLogger(__name__)

BASE_URL = "https://data.gov.il/api/3/action"
PACKAGE_SEARCH_URL = f"{BASE_URL}/package_search"
DATASTORE_SEARCH_URL = f"{BASE_URL}/datastore_search"
DATASET_PAGE = "https://data.gov.il/dataset"

#: CKAN режет выдачу; больше 32k записей забирать смысла нет для дневного среза.
PAGE_SIZE = 1000


class DataGovIlSource(Source):
    name = "data_gov_il"
    title = "data.gov.il — открытые данные правительства"
    kind = "government"

    def fetch(self) -> Iterable[Lot]:
        queries = self.ctx.option("queries") or ["מכרזי מקרקעין"]
        max_rows = int(self.ctx.option("max_rows", 2000))

        seen_resources: set[str] = set()
        produced = 0

        for query in queries:
            for resource_id, package_name in self._resources_for(query):
                if resource_id in seen_resources:
                    continue
                seen_resources.add(resource_id)

                try:
                    records = self._records(resource_id, min(max_rows - produced, max_rows))
                except HttpError as exc:
                    log.warning("data.gov.il: набор %s недоступен: %s", resource_id, exc)
                    continue

                for row in records:
                    if not looks_like_lot(row):
                        continue
                    lot = _row_to_lot(row, resource_id, package_name)
                    if lot is not None:
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
            for resource in package.get("resources", []) or []:
                # datastore_search работает только по загруженным в datastore ресурсам
                if resource.get("datastore_active") and resource.get("id"):
                    out.append((resource["id"], name))
        return out

    def _records(self, resource_id: str, limit: int) -> list[dict[str, Any]]:
        """Читает ресурс постранично: CKAN не поддерживает keyset-пагинацию."""
        records: list[dict[str, Any]] = []
        offset = 0
        while len(records) < limit:
            batch_size = min(PAGE_SIZE, limit - len(records))
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
        queries = self.ctx.option("queries") or ["מכרזי מקרקעין"]
        resources = self._resources_for(queries[0])
        return f"наборов с datastore по запросу {queries[0]!r}: {len(resources)}"


def _row_to_lot(row: dict[str, Any], resource_id: str, package_name: str) -> Lot | None:
    prices = {
        "final": to_float(pick(row, PRICE_FINAL_KEYS)),
        "min": to_float(pick(row, PRICE_MIN_KEYS)),
        "appraisal": to_float(pick(row, PRICE_APPRAISAL_KEYS)),
        "asking": to_float(pick(row, PRICE_ASKING_KEYS)),
    }
    price_nis, price_kind = choose_price(prices, ["final", "min", "appraisal", "asking"])

    row_id = row.get("_id") or pick(row, TENDER_NAME_KEYS) or ""
    if not row_id:
        return None

    name = clean_text(pick(row, TENDER_NAME_KEYS))
    units, basis = resolve_units(row, name, clean_text(pick(row, PURPOSE_KEYS)))

    return Lot(
        source=DataGovIlSource.name,
        source_id=f"{resource_id}:{row_id}",
        tender_id=clean_text(pick(row, TENDER_NAME_KEYS)),
        tender_name=name or package_name,
        url=f"{DATASET_PAGE}?q={package_name}",
        settlement=clean_text(pick(row, SETTLEMENT_KEYS)),
        neighborhood=clean_text(pick(row, NEIGHBORHOOD_KEYS)),
        region=clean_text(pick(row, REGION_KEYS)),
        gush=clean_text(pick(row, GUSH_KEYS)),
        chelka=clean_text(pick(row, CHELKA_KEYS)),
        purpose=clean_text(pick(row, PURPOSE_KEYS)),
        status=clean_text(pick(row, STATUS_KEYS)),
        area_sqm=to_float(pick(row, AREA_KEYS)),
        land_use=classify_landuse(clean_text(pick(row, PURPOSE_KEYS)), name),
        units=units,
        units_basis=basis,
        price_nis=price_nis,
        price_kind=price_kind,
        reserve_price_nis=prices.get("min"),
        development_costs_nis=to_float(pick(row, DEVELOPMENT_KEYS)),
        published_date=to_iso_date(pick(row, PUBLISHED_KEYS)),
        closing_date=to_iso_date(pick(row, CLOSING_KEYS)),
        raw=row,
    )
