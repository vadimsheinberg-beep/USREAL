"""Yad2 (יד2) — крупнейшая частная площадка объявлений о недвижимости.

Государственные тендеры показывают, за сколько государство продаёт землю;
Yad2 показывает, за сколько её перепродают на рынке. Вместе это даёт
сопоставимую картину по одному и тому же населённому пункту.

Yad2 отдаёт данные внутренним JSON-API своего фронтенда и активно защищается
от ботов. Поэтому: умеренный темп, ключ/куки из конфига при необходимости,
и мягкая деградация — недоступность Yad2 не роняет ежедневный запуск.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ..extract import as_list, clean_text, to_float, to_iso_date, walk_dicts
from ..models import PRICE_KIND_ASKING, Lot
from ..units import resolve_units
from .base import Source

log = logging.getLogger(__name__)

FEED_URL = "https://gw.yad2.co.il/feed-search-legacy/realestate/forsale"
ITEM_URL = "https://www.yad2.co.il/item"

#: Категории Yad2, относящиеся к земле. 39 — «מגרשים» (участки).
DEFAULT_PROPERTY_TYPES = [39]


class Yad2Source(Source):
    name = "yad2"
    title = "Yad2 — частные объявления по земле"
    kind = "private"

    def fetch(self) -> Iterable[Lot]:
        property_types = self.ctx.option("property_types") or DEFAULT_PROPERTY_TYPES
        max_pages = int(self.ctx.option("max_pages", 5))
        headers = self._headers()
        seen: set[str] = set()
        pages_ok = 0
        last_error: Exception | None = None

        for property_type in property_types:
            for page in range(1, max_pages + 1):
                try:
                    payload = self.ctx.http.get_json(
                        FEED_URL,
                        params={"property": property_type, "page": page, "forceLdLoad": "true"},
                        headers=headers,
                    )
                    pages_ok += 1
                except Exception as exc:  # noqa: BLE001 - обрыв на N-й странице не фатален
                    last_error = exc
                    log.warning("Yad2: страница %s категории %s недоступна: %s", page, property_type, exc)
                    break

                items = _feed_items(payload)
                if not items:
                    break

                fresh = 0
                for item in items:
                    lot = _item_to_lot(item, property_type)
                    if lot is None or lot.source_id in seen:
                        continue
                    seen.add(lot.source_id)
                    fresh += 1
                    yield lot

                if fresh == 0:
                    # Лента зациклилась на той же странице — дальше листать нечего.
                    break

        if pages_ok == 0 and last_error is not None:
            # Ни одной страницы: площадка недоступна. Молчать нельзя — иначе в
            # сводке источник выглядел бы исправным с нулём объявлений.
            raise last_error

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Referer": "https://www.yad2.co.il/realestate/forsale",
        }
        # Если площадка требует авторизованную сессию — кладём куки из конфига.
        cookie = self.ctx.option("cookie")
        if cookie:
            headers["Cookie"] = str(cookie)
        return headers

    def probe(self) -> str:
        property_types = self.ctx.option("property_types") or DEFAULT_PROPERTY_TYPES
        payload = self.ctx.http.get_json(
            FEED_URL,
            params={"property": property_types[0], "page": 1},
            headers=self._headers(),
        )
        return f"объявлений на первой странице: {len(_feed_items(payload))}"


def _feed_items(payload: Any) -> list[dict[str, Any]]:
    """Лента лежит в ``data.feed.feed_items``, но структура менялась не раз."""
    for node in walk_dicts(payload):
        for key in ("feed_items", "feedItems", "items"):
            value = node.get(key)
            if isinstance(value, list) and value:
                return [item for item in value if isinstance(item, dict)]
    return [item for item in as_list(payload) if isinstance(item, dict)]


def _item_to_lot(item: dict[str, Any], property_type: int) -> Lot | None:
    # В ленте попадаются рекламные и служебные карточки без идентификатора.
    token = item.get("link_token") or item.get("id") or item.get("ad_number")
    if not token:
        return None
    if item.get("type") in {"ad", "banner", "platinum_ad"} and not item.get("price"):
        return None

    price = to_float(item.get("price"))
    title = clean_text(item.get("title_1") or item.get("title") or item.get("row_1"))
    subtitle = clean_text(item.get("title_2") or item.get("row_2"))
    area = to_float(item.get("square_meters") or item.get("SquareMeter") or item.get("area"))

    units, basis = resolve_units(item, f"{title or ''} {subtitle or ''}")

    return Lot(
        source=Yad2Source.name,
        source_id=str(token),
        tender_name=title or subtitle,
        url=f"{ITEM_URL}/{token}",
        settlement=clean_text(item.get("city") or item.get("city_text")),
        neighborhood=clean_text(item.get("neighborhood") or item.get("area_text")),
        region=clean_text(item.get("area_text") or item.get("region")),
        purpose=clean_text(item.get("HomeTypeID_text") or item.get("property_type")) or f"yad2:{property_type}",
        area_sqm=area,
        units=units,
        units_basis=basis,
        price_nis=price,
        price_kind=PRICE_KIND_ASKING if price else None,
        published_date=to_iso_date(item.get("date") or item.get("date_added")),
        raw=item,
    )
