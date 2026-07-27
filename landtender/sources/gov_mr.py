"""Портал государственных закупок — mr.gov.il (מנהל הרכש הממשלתי).

Здесь публикуются тендеры всех министерств, включая земельные лоты рм"י,
которые идут через закупочную процедуру. Открытого JSON-API у портала нет,
поэтому разбираем HTML выдачи поиска и берём только записи с земельными
ключевыми словами.

Источник вспомогательный: цена в карточке выдачи есть не всегда, но сам
факт публикации и ссылка на тендер попадают в дневную сводку.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Iterable

from ..extract import clean_text, to_float, to_iso_date
from ..models import PRICE_KIND_MIN, Lot
from ..units import resolve_units
from .base import Source

log = logging.getLogger(__name__)

BASE_URL = "https://mr.gov.il"
SEARCH_URL = f"{BASE_URL}/ilgstorefront/he/search/"

#: Земельные ключевые слова — отсекают закупки канцтоваров от лотов по земле.
LAND_KEYWORDS = (
    "מקרקעין",
    "קרקע",
    "מגרש",
    "מגרשים",
    "חכירה",
    "הקצאת קרקע",
    "מכרז מקרקעי",
)

_PRICE_RE = re.compile(r"(?:₪|ש\"ח)\s*([\d,\.]+)|([\d,\.]{7,})\s*(?:₪|ש\"ח)")
_DATE_RE = re.compile(r"\b(\d{1,2}[./]\d{1,2}[./]\d{2,4})\b")


class _ResultParser(HTMLParser):
    """Собирает пары «ссылка на тендер → текст карточки» из HTML выдачи."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self._href: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if "/p/" in href or "tender" in href.lower():
            self._flush()
            self._href = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buffer.append(data)

    def _flush(self) -> None:
        if self._href is not None:
            text = clean_text(" ".join(self._buffer)) or ""
            if text:
                self.items.append({"href": self._href, "text": text})
        self._href = None
        self._buffer = []

    def close(self) -> None:  # noqa: D102 - дозакрываем последнюю карточку
        super().close()
        self._flush()


class GovMrSource(Source):
    name = "gov_mr"
    title = "mr.gov.il — государственные закупки и тендеры"
    kind = "government"

    def fetch(self) -> Iterable[Lot]:
        max_pages = int(self.ctx.option("max_pages", 5))
        keywords = self.ctx.option("keywords") or ["מקרקעין", "מגרש"]
        seen: set[str] = set()

        for keyword in keywords:
            for page in range(max_pages):
                items = self._search_page(keyword, page)
                if not items:
                    break
                for item in items:
                    lot = _item_to_lot(item)
                    if lot is None or lot.source_id in seen:
                        continue
                    seen.add(lot.source_id)
                    yield lot

    def _search_page(self, keyword: str, page: int) -> list[dict[str, str]]:
        response = self.ctx.http.get(
            SEARCH_URL,
            params={"text": keyword, "s": "TENDER", "page": page},
        )
        parser = _ResultParser()
        parser.feed(response.text)
        parser.close()
        return parser.items

    def probe(self) -> str:
        items = self._search_page("מקרקעין", 0)
        return f"карточек на первой странице: {len(items)}"


def _item_to_lot(item: dict[str, str]) -> Lot | None:
    text = item["text"]
    if not any(keyword in text for keyword in LAND_KEYWORDS):
        return None

    href = item["href"]
    url = href if href.startswith("http") else f"{BASE_URL}{href}"
    source_id = href.rstrip("/").rsplit("/", 1)[-1] or href

    price = None
    match = _PRICE_RE.search(text)
    if match:
        price = to_float(match.group(1) or match.group(2))

    published = None
    date_match = _DATE_RE.search(text)
    if date_match:
        published = to_iso_date(date_match.group(1))

    units, basis = resolve_units(text=text)

    return Lot(
        source=GovMrSource.name,
        source_id=source_id,
        tender_name=text[:200],
        url=url,
        units=units,
        units_basis=basis,
        price_nis=price,
        price_kind=PRICE_KIND_MIN if price else None,
        published_date=published,
        raw=dict(item),
    )
