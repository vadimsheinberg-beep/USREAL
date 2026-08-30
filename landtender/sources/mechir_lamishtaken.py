"""Программа «דירה בהנחה» — субсидированные квартиры с ценой за метр.

Это единственный найденный на data.gov.il набор, где государство публикует
цену жилья. Сделок рашут а-мисим там нет: запросы «נדל"ן», «עסקאות מקרקעין»,
«מחירי דירות» и «רשות המסים» возвращают ноль наборов — разведка проверена,
не предположена.

Что даёт набор:
  * цену за квадратный метр по проекту (``PriceForMeter``);
  * город кодом ЦСБ (``LamasCode``) — тем же, по которому в проекте
    сравниваются участки, так что квартиры и земля сходятся по месту;
  * число квартир, квартал, застройщика и стадию проекта.

Чего не даёт: площади конкретной квартиры. Поэтому полная цена не считается —
её пришлось бы выводить из выдуманной площади. Хранится цена метра как есть.

Имена колонок сняты разведкой ``landtender inspect --ckan דירות``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ..extract import clean_text, to_float, to_int, to_iso_date
from ..http import HttpError
from ..models import UNITS_REPORTED, Lot
from .base import Source

log = logging.getLogger(__name__)

DATASTORE_SEARCH_URL = "https://data.gov.il/api/3/action/datastore_search"

#: Ресурс набора ``mechir-lamishtaken`` с периодическими данными по розыгрышам.
RESOURCE_ID = "7c8255d0-49ef-49db-8904-4cf917586031"

#: Страница программы: из строки сводки нужно попадать на первоисточник.
PROGRAM_URL = "https://www.gov.il/he/Departments/publications/reports/mishtaken_statistics"

PAGE_SIZE = 1000

#: Стадии, на которых квартиру ещё можно получить. Проект с опубликованными
#: результатами розыгрыша — уже история, а не предложение.
OPEN_STATUSES = ("בתהליכי הגרלה", "נרשמים", "פתוח")


class MechirLamishtakenSource(Source):
    name = "mechir_lamishtaken"
    title = "דירה בהנחה — субсидированные квартиры (data.gov.il)"
    kind = "government"

    def fetch(self) -> Iterable[Lot]:
        max_rows = int(self.ctx.option("max_rows", 3000))
        only_open = bool(self.ctx.option("only_open", False))

        for row in self._records(max_rows):
            lot = _row_to_lot(row)
            if lot is None:
                continue
            if only_open and lot.status not in OPEN_STATUSES:
                continue
            yield lot

    def _records(self, limit: int) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0
        while len(records) < limit:
            batch_size = min(PAGE_SIZE, limit - len(records))
            try:
                data = self.ctx.http.get_json(
                    DATASTORE_SEARCH_URL,
                    params={
                        "resource_id": RESOURCE_ID,
                        "limit": batch_size,
                        "offset": offset,
                    },
                )
            except HttpError as exc:
                log.warning("דירה בהנחה: набор недоступен: %s", exc)
                break
            batch = (data or {}).get("result", {}).get("records", [])
            if not batch:
                break
            records.extend(r for r in batch if isinstance(r, dict))
            offset += len(batch)
            if len(batch) < batch_size:
                break
        return records

    def probe(self) -> str:
        rows = self._records(1)
        return f"записей доступно: {'да' if rows else 'нет'}"


def _row_to_lot(row: dict[str, Any]) -> Lot | None:
    """Строка набора → лот. Без цены метра запись бесполезна и пропускается."""
    lottery_id = to_int(row.get("LotteryId"))
    if lottery_id is None:
        return None

    # Цена приходит строкой с разделителями разрядов: «9,242.00».
    price_per_sqm = to_float(str(row.get("PriceForMeter") or "").replace(",", ""))
    if not price_per_sqm or price_per_sqm <= 0:
        return None

    project = clean_text(row.get("ProjectName"))
    city = clean_text(row.get("LamasName"))
    project_id = to_int(row.get("ProjectId"))
    # Номер проекта приходит числом; в подписи он нужен, чтобы отличать
    # одноимённые проекты в разных городах.
    name = " · ".join(
        part for part in (project, str(project_id) if project_id else "") if part
    )

    return Lot(
        source="mechir_lamishtaken",
        source_id=f"lottery:{lottery_id}",
        tender_id=str(project_id or lottery_id),
        tender_name=clean_text(name) or f"הגרלה {lottery_id}",
        url=PROGRAM_URL,
        settlement=city,
        settlement_code=to_int(row.get("LamasCode")),
        neighborhood=clean_text(row.get("Neighborhood")),
        purpose="מגורים",
        status=clean_text(row.get("ProjectStatus")),
        tender_type=clean_text(row.get("MarketingMethodDesc")),
        units=to_int(row.get("LotteryHousingUnits")),
        units_basis=UNITS_REPORTED,
        price_per_sqm_nis=price_per_sqm,
        published_date=to_iso_date(row.get("LotteryEndSignupDate")),
        closing_date=to_iso_date(row.get("LotteryExecutionDate")),
        raw={},
    )
