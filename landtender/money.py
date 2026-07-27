"""Курс шекеля и расчёт стоимости земли в долларах.

Источники в отчёте отдают шекели. Порог «выше миллиона долларов» требует
курса, поэтому берём официальный представительный курс Банка Израиля,
кешируем его на сутки и в крайнем случае откатываемся к курсу из конфига.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .extract import to_float, to_iso_date, walk_dicts
from .http import HttpClient, HttpError
from .models import (
    PRICE_KIND_APPRAISAL,
    PRICE_KIND_ASKING,
    PRICE_KIND_FINAL,
    PRICE_KIND_MIN,
    TIER_PREMIUM,
    TIER_STANDARD,
    TIER_UNKNOWN,
    Lot,
)

log = logging.getLogger(__name__)

BOI_URL = "https://boi.org.il/PublicApi/GetExchangeRate?key=USD"
BOI_ALL_URL = "https://boi.org.il/PublicApi/GetExchangeRates"


@dataclass
class FxRate:
    """Курс USD/ILS: сколько шекелей за один доллар."""

    rate: float
    as_of: str
    source: str

    def to_usd(self, nis: float | None) -> float | None:
        if nis is None or self.rate <= 0:
            return None
        return nis / self.rate


class FxProvider:
    """Достаёт курс с кешем на диске, чтобы не дёргать БИ на каждый запуск."""

    def __init__(
        self,
        http: HttpClient,
        provider: str = "boi",
        static_rate: float = 3.70,
        cache_hours: int = 12,
        cache_path: Path | None = None,
    ) -> None:
        self.http = http
        self.provider = provider
        self.static_rate = float(static_rate)
        self.cache_hours = cache_hours
        self.cache_path = cache_path

    def get(self) -> FxRate:
        if self.provider == "static":
            return FxRate(self.static_rate, date.today().isoformat(), "static")

        cached = self._read_cache()
        if cached is not None:
            return cached

        try:
            rate = self._fetch_boi()
        except (HttpError, ValueError) as exc:
            log.warning("Курс Банка Израиля недоступен (%s), беру статический %.4f", exc, self.static_rate)
            return FxRate(self.static_rate, date.today().isoformat(), "static-fallback")

        self._write_cache(rate)
        return rate

    # ------------------------------------------------------------------ BOI --

    def _fetch_boi(self) -> FxRate:
        payload = self.http.get_json(BOI_URL)
        rate = _extract_usd_rate(payload)
        if rate is None:
            payload = self.http.get_json(BOI_ALL_URL)
            rate = _extract_usd_rate(payload)
        if rate is None:
            raise ValueError("в ответе Банка Израиля нет курса USD")
        value, as_of = rate
        return FxRate(value, as_of or date.today().isoformat(), "boi")

    # ---------------------------------------------------------------- кеш ----

    def _read_cache(self) -> FxRate | None:
        if self.cache_path is None or not self.cache_path.exists():
            return None
        try:
            age_hours = (time.time() - self.cache_path.stat().st_mtime) / 3600
            if age_hours > self.cache_hours:
                return None
            data = json.loads(self.cache_path.read_text("utf-8"))
            return FxRate(float(data["rate"]), str(data["as_of"]), f"{data.get('source', 'boi')}-cached")
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_cache(self, rate: FxRate) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"rate": rate.rate, "as_of": rate.as_of, "source": rate.source}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.debug("Не удалось записать кеш курса: %s", exc)


def _extract_usd_rate(payload: Any) -> tuple[float, str | None] | None:
    """Вытаскивает курс USD из любого из форматов ответа Банка Израиля."""
    for node in walk_dicts(payload):
        key = node.get("key") or node.get("Key") or node.get("currencyCode")
        if key is not None and str(key).upper() not in {"USD", "USD/ILS", "$"}:
            continue
        value = None
        for field in ("currentExchangeRate", "CurrentExchangeRate", "rate", "Rate", "value", "lastRate"):
            value = to_float(node.get(field))
            if value:
                break
        if not value:
            continue
        unit = to_float(node.get("unit") or node.get("Unit")) or 1.0
        if unit and unit != 1.0:
            value = value / unit
        as_of = None
        for field in ("lastUpdate", "LastUpdate", "date", "Date", "asOf", "timeStamp"):
            as_of = to_iso_date(node.get(field))
            if as_of:
                break
        if 0.5 < value < 100:  # шекель за доллар исторически в этом коридоре
            return value, as_of
    return None


# ------------------------------------------------------- оценка стоимости ---

def choose_price(
    candidates: dict[str, float | None],
    preference: list[str],
) -> tuple[float | None, str | None]:
    """Выбирает «стоимость земли» из доступных цен по приоритету из конфига.

    Приоритет по умолчанию: цена сделки → минимальная цена → шумá → запрос.
    """
    for kind in preference:
        value = candidates.get(kind)
        if value is not None and value > 0:
            return float(value), kind
    return None, None


def enrich_lot(
    lot: Lot,
    fx: FxRate,
    threshold_usd: float,
    include_development_costs: bool = False,
) -> Lot:
    """Досчитывает долларовые поля и относит лот к группе относительно порога."""
    price_nis = lot.price_nis
    if include_development_costs and price_nis is not None and lot.development_costs_nis:
        price_nis = price_nis + lot.development_costs_nis

    lot.fx_rate = fx.rate
    lot.fx_date = fx.as_of
    lot.price_usd = fx.to_usd(price_nis)

    if lot.price_usd is not None and lot.units:
        lot.price_per_unit_usd = round(lot.price_usd / lot.units, 2)
    if lot.price_usd is not None and lot.area_sqm:
        lot.price_per_sqm_usd = round(lot.price_usd / lot.area_sqm, 2)
    if lot.price_usd is not None:
        lot.price_usd = round(lot.price_usd, 2)

    if lot.price_usd is None:
        lot.tier = TIER_UNKNOWN
    elif lot.price_usd >= threshold_usd:
        lot.tier = TIER_PREMIUM
    else:
        lot.tier = TIER_STANDARD
    return lot


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
