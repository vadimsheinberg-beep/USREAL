"""Макропоказатели: индексы ЦСБ и ставка Банка Израиля.

Три ряда, каждый со своей ролью в оценке земли:

* **מדד מחירי תשומה בבנייה למגורים** — к нему привязаны платежи победителя
  тендера рм"и. Между подачей заявки и оплатой проходят месяцы, и цена лота
  за это время растёт вместе с индексом. Без него «цена участка» — это цена
  на дату публикации, а не та, которую придётся заплатить.
* **מדד ומחירים ממוצעים משוק הדירות** — рынок жилья. Им приводим цены старых
  сделок к сегодняшним деньгам, иначе сравнивать участок 2019 года с участком
  2026-го бессмысленно.
* **ставка Банка Израиля** — стоимость денег. Земля приносит доход через годы,
  и при дорогих деньгах за неё платят меньше.

Коды индексов сняты из каталога ЦСБ, а не угаданы: первая попытка назначила
строительным затратам код 160010, и API ответил HTTP 500.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

CBS_INDEX_DATA = "https://api.cbs.gov.il/index/data/price"

#: Коды из каталога ЦСБ (``/index/catalog/catalog``, поле ``mainCode``).
CPI = "120010"  # מדד המחירים לצרכן
HOUSING = "40010"  # מדד ומחירים ממוצעים משוק הדירות
BUILDING_INPUTS = "200010"  # מדד מחירי תשומה בבנייה למגורים

INDEX_TITLES = {
    CPI: "индекс потребительских цен",
    HOUSING: "рынок жилья",
    BUILDING_INPUTS: "затраты на строительство",
}

#: Банк Израиля. Курс валют мы уже берём с этого хоста, он доступен.
BOI_RATE_URL = "https://www.boi.org.il/PublicApi/GetInterest"


@dataclass(frozen=True)
class IndexPoint:
    """Одно значение индекса."""

    code: str
    name: str | None
    year: int
    month: int
    value: float
    #: Изменение за месяц и за год, в процентах, как их считает само ЦСБ.
    change_month: float | None = None
    change_year: float | None = None

    @property
    def period(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True)
class Macro:
    """Снимок макропоказателей на день запуска."""

    building_inputs: IndexPoint | None = None
    housing: IndexPoint | None = None
    cpi: IndexPoint | None = None
    boi_rate: float | None = None
    boi_rate_date: str | None = None

    @property
    def empty(self) -> bool:
        return not any((self.building_inputs, self.housing, self.cpi, self.boi_rate))


class CbsIndices:
    """Чтение индексов ЦСБ с кешем на диске.

    Индексы выходят раз в месяц, поэтому ходить за ними на каждый запуск
    незачем; кеш заодно спасает сводку, когда API недоступен.
    """

    def __init__(self, http: HttpClient, cache_path: Path | None = None, cache_hours: int = 24):
        self.http = http
        self.cache_path = cache_path
        self.cache_hours = cache_hours

    def latest(self, code: str) -> IndexPoint | None:
        """Последнее опубликованное значение индекса."""
        points = self.series(code, last=1)
        return points[0] if points else None

    def series(self, code: str, last: int = 24) -> list[IndexPoint]:
        """Ряд значений, свежие первыми. Пустой список — данных нет."""
        cached = self._read_cache(code, last)
        if cached is not None:
            return cached

        try:
            data = self.http.get_json(
                CBS_INDEX_DATA,
                params={"id": code, "format": "json", "download": "false", "last": last},
            )
        except HttpError as exc:
            log.warning("ЦСБ: индекс %s недоступен: %s", code, exc)
            return self._read_cache(code, last, ignore_age=True) or []

        points = parse_index(data, code)
        if points:
            self._write_cache(code, points)
        return points

    # ------------------------------------------------------------- кеш ------

    def _file(self, code: str) -> Path | None:
        return self.cache_path / f"cbs_{code}.json" if self.cache_path else None

    def _read_cache(
        self, code: str, last: int, ignore_age: bool = False
    ) -> list[IndexPoint] | None:
        path = self._file(code)
        if path is None or not path.exists():
            return None
        try:
            if not ignore_age:
                age_hours = (time.time() - path.stat().st_mtime) / 3600
                if age_hours > self.cache_hours:
                    return None
            rows = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        points = [IndexPoint(**row) for row in rows]
        return points[:last] if len(points) >= last else points

    def _write_cache(self, code: str, points: list[IndexPoint]) -> None:
        path = self._file(code)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps([p.__dict__ for p in points], ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.debug("ЦСБ: кеш индекса %s не записался: %s", code, exc)


def parse_index(payload: Any, code: str) -> list[IndexPoint]:
    """Разбирает ответ ``/index/data/price``.

    Форма ответа снята с живого API: ``month`` → список рядов, у каждого
    ``date`` → список точек, а само значение лежит в ``currBase.value``.
    Квартальные индексы приходят в ключе ``quarter`` той же формы.
    """
    if not isinstance(payload, dict):
        return []

    points: list[IndexPoint] = []
    for bucket in ("month", "quarter"):
        for series in payload.get(bucket) or []:
            if not isinstance(series, dict):
                continue
            name = series.get("name")
            series_code = str(series.get("code") or code)
            for entry in series.get("date") or []:
                point = _to_point(entry, series_code, name)
                if point is not None:
                    points.append(point)

    # ЦСБ отдаёт свежие первыми, но полагаться на это не будем.
    points.sort(key=lambda p: (p.year, p.month), reverse=True)
    return points


def _to_point(entry: Any, code: str, name: str | None) -> IndexPoint | None:
    if not isinstance(entry, dict):
        return None
    base = entry.get("currBase") or {}
    value = _as_float(base.get("value"))
    year = _as_int(entry.get("year"))
    # У квартальных рядов месяца нет — берём последний месяц квартала.
    month = _as_int(entry.get("month")) or _quarter_month(entry.get("quarter"))
    if value is None or year is None or month is None:
        return None
    return IndexPoint(
        code=code,
        name=name,
        year=year,
        month=month,
        value=value,
        change_month=_as_float(entry.get("percent")),
        change_year=_as_float(entry.get("percentYear")),
    )


def _quarter_month(quarter: Any) -> int | None:
    number = _as_int(quarter)
    return number * 3 if number and 1 <= number <= 4 else None


def fetch_boi_rate(http: HttpClient) -> tuple[float | None, str | None]:
    """Ставка Банка Израиля. ``(None, None)``, если API её не отдал.

    Выдумывать ставку нельзя: она входит в оценку, и подставленное «примерно
    4.5» испортило бы вывод молча.
    """
    try:
        data = http.get_json(BOI_RATE_URL)
    except HttpError as exc:
        log.warning("Банк Израиля: ставка недоступна: %s", exc)
        return None, None

    rate = _find_number(data, ("interestRate", "rate", "value", "currentInterest"))
    as_of = _find_text(data, ("date", "asOf", "effectiveDate", "lastUpdate"))
    return rate, as_of


def collect(
    http: HttpClient,
    cache_path: Path | None = None,
    with_rate: bool = True,
) -> Macro:
    """Снимок всех показателей разом. Отказ любого из них не фатален."""
    indices = CbsIndices(http, cache_path=cache_path)
    rate, rate_date = fetch_boi_rate(http) if with_rate else (None, None)
    return Macro(
        building_inputs=indices.latest(BUILDING_INPUTS),
        housing=indices.latest(HOUSING),
        cpi=indices.latest(CPI),
        boi_rate=rate,
        boi_rate_date=rate_date,
    )


def index_factor(points: list[IndexPoint], when: str | None) -> float | None:
    """Во сколько раз индекс вырос от даты ``when`` до последнего значения.

    Этим коэффициентом приводят цену старой сделки к сегодняшним деньгам.
    ``None`` — если ряда нет или дата вне его покрытия: множитель «1.0» по
    умолчанию тихо соврал бы, что инфляции не было.
    """
    if not points or not when:
        return None
    try:
        year, month = int(when[:4]), int(when[5:7])
    except (ValueError, IndexError):
        return None

    latest = points[0]
    # Ближайшая точка, не позже нужного месяца.
    past = None
    for point in points:
        if (point.year, point.month) <= (year, month):
            past = point
            break
    if past is None or not past.value:
        return None
    return latest.value / past.value


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_number(node: Any, keys: tuple[str, ...]) -> float | None:
    """Ищет число под одним из имён в ответе любой формы."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in {k.lower() for k in keys}:
                number = _as_float(value)
                if number is not None:
                    return number
        for value in node.values():
            found = _find_number(value, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_number(item, keys)
            if found is not None:
                return found
    return None


def _find_text(node: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in {k.lower() for k in keys} and isinstance(value, str):
                return value[:10]
        for value in node.values():
            found = _find_text(value, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_text(item, keys)
            if found is not None:
                return found
    return None
