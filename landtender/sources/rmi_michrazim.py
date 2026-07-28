"""Земельные тендеры Управления земель Израиля (רשות מקרקעי ישראל, рм"י).

Главный источник программы: apps.land.gov.il/MichrazimSite — единственный
официальный реестр земельных тендеров государства.

Схема работы:
  1. ``POST /api/SearchApi/Search`` — список тендеров с назначением, статусом
     и количеством единиц жилья;
  2. ``GET  /api/MichrazDetailsApi/Get`` — детали конкретного тендера, где
     лежат участки с ценами (минимальная, шумá, цена сделки).

Знание об эндпоинтах и именах полей взято из MIT-проекта barvhaim/remy-mcp.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ..extract import (
    AREA_KEYS,
    CHELKA_KEYS,
    CLOSING_KEYS,
    COMMITTEE_KEYS,
    DEVELOPMENT_KEYS,
    GUARANTEE_KEYS,
    GUSH_KEYS,
    NEIGHBORHOOD_KEYS,
    PRICE_APPRAISAL_KEYS,
    PRICE_FINAL_KEYS,
    PRICE_MIN_KEYS,
    PUBLISHED_KEYS,
    PURPOSE_KEYS,
    REGION_KEYS,
    SETTLEMENT_KEYS,
    STATUS_KEYS,
    TENDER_NAME_KEYS,
    TENDER_TYPE_KEYS,
    as_list,
    clean_text,
    looks_like_lot,
    pick,
    UNITS_KEYS,
    to_float,
    to_int,
    to_iso_date,
    walk_dicts,
)
from ..http import HttpError
from ..models import UNITS_REPORTED, Lot
from ..money import choose_price
from ..units import resolve_units, units_from_record
from .base import Source

log = logging.getLogger(__name__)

BASE_URL = "https://apps.land.gov.il/MichrazimSite"
API_URL = f"{BASE_URL}/api"
SEARCH_URL = f"{API_URL}/SearchApi/Search"
DETAILS_URL = f"{API_URL}/MichrazDetailsApi/Get"

#: Портал отвечает только на запросы, которые выглядят как запросы его же SPA.
SITE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://apps.land.gov.il",
    "Referer": f"{BASE_URL}/",
}

#: Расшифровка числовых кодов портала.
#:
#: Портал отдаёт назначение и статус числами, а названия подставляет фронтенд.
#: Таблицы ниже — рабочие значения; если ваш экземпляр портала нумерует иначе,
#: перекройте их в конфиге (``purpose_codes`` / ``status_codes`` в секции
#: ``[sources.rmi_michrazim]``). Незнакомый код НЕ подписывается наугад —
#: поле остаётся пустым, чтобы в отчёт не попала выдуманная категория.
PURPOSE_CODES = {
    1: "מגורים",  # жильё
    2: "תעסוקה",  # занятость/коммерция
    3: "מסחר",  # торговля
    4: "תעשייה",  # промышленность
    5: "חקלאות",  # сельское хозяйство
    6: "מבני ציבור",  # общественные здания
}

STATUS_CODES = {
    1: "פתוח",  # открыт
    2: "סגור",  # закрыт
    3: "בוטל",  # отменён
    4: "הסתיים",  # завершён
}


class RmiMichrazimSource(Source):
    name = "rmi_michrazim"
    title = 'רמ"י — земельные тендеры (apps.land.gov.il)'
    kind = "government"

    def _codes(self) -> dict[str, dict[int, str]]:
        """Таблицы кодов с учётом переопределений из конфига."""
        purpose = dict(PURPOSE_CODES)
        status = dict(STATUS_CODES)
        for key, target in (("purpose_codes", purpose), ("status_codes", status)):
            override = self.ctx.option(key) or {}
            for code, name in dict(override).items():
                code_int = to_int(code)
                if code_int is not None:
                    target[code_int] = str(name)
        return {"purpose": purpose, "status": status}

    def fetch(self) -> Iterable[Lot]:
        tenders = self._search()
        log.info("рм\"י: тендеров в выдаче — %d", len(tenders))

        budget = int(self.ctx.option("details_budget", 400))
        codes = self._codes()
        fetched_details = 0

        for tender in tenders:
            meta = _tender_meta(tender, codes)
            tender_id = meta.get("tender_id")
            if not tender_id:
                continue

            fingerprint = _fingerprint(tender)
            needs_details = True
            if self.ctx.cache is not None and not self.ctx.full_refresh:
                needs_details = self.ctx.cache.tender_changed(self.name, tender_id, fingerprint)

            details: Any = None
            if needs_details and fetched_details < budget:
                try:
                    details = self._details(tender_id)
                    fetched_details += 1
                except HttpError as exc:
                    log.warning('рм"י: детали тендера %s недоступны: %s', tender_id, exc)

            lots = list(_lots_from_details(details, meta, codes)) if details else []
            if not lots:
                # Без деталей отдаём тендер как один лот: цены нет, но назначение,
                # количество единиц и сроки уже полезны и попадут в отчёт.
                lots = [_tender_level_lot(meta, tender)]

            yield from lots

            if self.ctx.cache is not None and details is not None:
                self.ctx.cache.remember_tender(self.name, tender_id, fingerprint)

        if fetched_details >= budget:
            log.warning(
                'рм"י: достигнут лимит details_budget=%d, часть цен будет добрана в следующий запуск',
                budget,
            )

    # --------------------------------------------------------------- API ----

    def _search(self) -> list[dict[str, Any]]:
        payload = {
            "ActiveQuickSearch": False,
            "ActiveMichraz": bool(self.ctx.option("active_only", False)),
        }
        data = self.ctx.http.post_json(SEARCH_URL, json=payload, headers=SITE_HEADERS)
        return [item for item in as_list(data) if isinstance(item, dict)]

    def _details(self, tender_id: str) -> Any:
        return self.ctx.http.get_json(
            DETAILS_URL, params={"michrazID": tender_id}, headers=SITE_HEADERS
        )

    def probe(self) -> str:
        tenders = self._search()
        return f"тендеров в поиске: {len(tenders)}"


# ------------------------------------------------------------ разбор ответа --


def _fingerprint(tender: dict[str, Any]) -> str:
    """Отпечаток тендера по полям поиска — меняется, когда меняется тендер."""
    parts = [
        str(pick(tender, STATUS_KEYS)),
        str(pick(tender, CLOSING_KEYS)),
        str(pick(tender, COMMITTEE_KEYS)),
        str(pick(tender, ("YechidotDiur",))),
        str(tender.get("ChoveretUpdateDate")),
    ]
    return "|".join(parts)


def _decode(codes: dict[int, str], value: Any) -> str | None:
    """Числовой код → ивритское название; строку оставляем как есть.

    Числу без расшифровки соответствует ``None``: пустое поле честнее, чем
    «3» в колонке «назначение».
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip().isdigit():
        return clean_text(value)
    number = to_int(value)
    if number is not None:
        return codes.get(number)
    return clean_text(value)


def _tender_meta(tender: dict[str, Any], codes: dict[str, dict[int, str]]) -> dict[str, Any]:
    tender_id = pick(tender, ("MichrazID", "MichrazId", "michrazID"))
    tender_id = str(to_int(tender_id) or "") or clean_text(tender_id)
    return {
        "tender_id": tender_id,
        "tender_name": clean_text(pick(tender, TENDER_NAME_KEYS)),
        "settlement": clean_text(pick(tender, SETTLEMENT_KEYS)),
        "neighborhood": clean_text(pick(tender, NEIGHBORHOOD_KEYS)),
        # Для мерхава таблицы кодов нет — числовой код показывать бессмысленно.
        "region": _decode({}, pick(tender, REGION_KEYS)),
        "purpose": _decode(codes["purpose"], pick(tender, PURPOSE_KEYS)),
        "tender_type": _decode({}, pick(tender, TENDER_TYPE_KEYS)),
        "status": _decode(codes["status"], pick(tender, STATUS_KEYS)),
        "published_date": to_iso_date(pick(tender, PUBLISHED_KEYS)),
        "closing_date": to_iso_date(pick(tender, CLOSING_KEYS)),
        "committee_date": to_iso_date(pick(tender, COMMITTEE_KEYS)),
        "units": to_int(pick(tender, ("YechidotDiur", "YechidotDiyur", "Kibolet"))),
        "guarantee_nis": to_float(pick(tender, GUARANTEE_KEYS)),
        "url": f"{BASE_URL}/#/michraz/{tender_id}" if tender_id else BASE_URL,
    }


def _tender_level_lot(meta: dict[str, Any], raw: dict[str, Any]) -> Lot:
    units, basis = _resolve_lot_units(raw, meta, meta.get("purpose"))
    return Lot(
        source=RmiMichrazimSource.name,
        source_id=f"{meta['tender_id']}",
        tender_id=meta["tender_id"],
        tender_name=meta["tender_name"],
        url=meta["url"],
        settlement=meta["settlement"],
        neighborhood=meta["neighborhood"],
        region=meta["region"],
        purpose=meta["purpose"],
        tender_type=meta["tender_type"],
        status=meta["status"],
        units=units,
        units_basis=basis,
        published_date=meta["published_date"],
        closing_date=meta["closing_date"],
        committee_date=meta["committee_date"],
        raw=raw,
    )


def _resolve_lot_units(
    node: dict[str, Any], meta: dict[str, Any], purpose: str | None
) -> tuple[int | None, str | None]:
    """Единицы строений для участка, от самого надёжного источника к слабому.

    1. поле участка (``YechidotDiur`` внутри миграша);
    2. поле тендера — когда участок один, это то же самое число;
    3. эвристики по тексту и назначению.
    """
    units, basis = units_from_record(node)
    if units is not None:
        return units, basis
    if meta.get("units"):
        return int(meta["units"]), UNITS_REPORTED
    return resolve_units(node, meta.get("tender_name"), purpose)


def _migrash_name(node: dict[str, Any]) -> str | None:
    """Номер участка: у рм"י это ``TikID`` либо ``MigrashName`` внутри плана."""
    direct = clean_text(pick(node, ("TikID", "MitchamName", "MigrashNumber", "Migrash", "מגרש")))
    if direct:
        return direct
    plans = node.get("TochnitMigrash")
    if isinstance(plans, list):
        for plan in plans:
            if isinstance(plan, dict):
                name = clean_text(pick(plan, ("MigrashName", "MigrashNumber")))
                if name:
                    return name
    return None


def _gush_chelka(node: dict[str, Any]) -> tuple[str | None, str | None]:
    """Гуш и хелька участка.

    Портал кладёт их в массив ``GushHelka`` — участок может лежать сразу на
    нескольких кадастровых единицах. Берём первую; остальные видны в raw.
    """
    entries = node.get("GushHelka")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            gush = clean_text(pick(entry, GUSH_KEYS))
            chelka = clean_text(pick(entry, CHELKA_KEYS))
            if gush or chelka:
                return gush, chelka
    return clean_text(pick(node, GUSH_KEYS)), clean_text(pick(node, CHELKA_KEYS))


def _lot_key(
    node: dict[str, Any], gush: str | None, chelka: str | None, prices: dict[str, float | None]
) -> str:
    """Устойчивый ключ участка внутри тендера.

    Гуш и хелька однозначно определяют участок, когда они есть. Если их нет,
    опознаём участок по совокупности его характеристик — два одинаковых
    набора цифр это один и тот же участок, встреченный дважды.
    """
    if gush or chelka:
        return f"{gush or ''}/{chelka or ''}/{_migrash_name(node) or ''}"

    parts = [
        _migrash_name(node),
        to_float(pick(node, AREA_KEYS)),
        to_int(pick(node, UNITS_KEYS)),
        prices["final"],
        prices["min"],
        prices["appraisal"],
    ]
    return "|".join("" if part is None else str(part) for part in parts)


def _lots_from_details(
    details: Any, meta: dict[str, Any], codes: dict[str, dict[int, str]]
) -> Iterable[Lot]:
    """Достаёт участки с ценами из ответа ``MichrazDetailsApi``.

    Портал прячет участки на разной глубине и по-разному их называет, поэтому
    обходим всё дерево и берём узлы, похожие на участок.
    """
    seen: set[str] = set()

    for node in walk_dicts(details):
        if not looks_like_lot(node):
            continue

        prices = {
            "final": to_float(pick(node, PRICE_FINAL_KEYS)),
            "min": to_float(pick(node, PRICE_MIN_KEYS)),
            "appraisal": to_float(pick(node, PRICE_APPRAISAL_KEYS)),
        }
        gush, chelka = _gush_chelka(node)

        # Ключ участка строится по его содержимому, а не по порядку обхода:
        # портал повторяет одни и те же данные на разных уровнях вложенности,
        # и порядковый номер превращал каждый повтор в отдельный «участок».
        key = _lot_key(node, gush, chelka, prices)
        if key in seen:
            continue
        seen.add(key)

        price_nis, price_kind = choose_price(prices, ["final", "min", "appraisal"])
        # Назначение участка точнее общего назначения тендера — берём его первым.
        purpose = _decode(codes["purpose"], pick(node, PURPOSE_KEYS)) or meta["purpose"]
        units, basis = _resolve_lot_units(node, meta, purpose)

        yield Lot(
            source=RmiMichrazimSource.name,
            source_id=f"{meta['tender_id']}:{key}",
            tender_id=meta["tender_id"],
            tender_name=meta["tender_name"],
            url=meta["url"],
            settlement=clean_text(pick(node, SETTLEMENT_KEYS)) or meta["settlement"],
            neighborhood=clean_text(pick(node, NEIGHBORHOOD_KEYS)) or meta["neighborhood"],
            region=meta["region"],
            gush=gush,
            chelka=chelka,
            purpose=purpose,
            tender_type=meta["tender_type"],
            status=meta["status"],
            area_sqm=to_float(pick(node, AREA_KEYS)),
            units=units,
            units_basis=basis,
            price_nis=price_nis,
            price_kind=price_kind,
            development_costs_nis=to_float(pick(node, DEVELOPMENT_KEYS)),
            guarantee_nis=to_float(pick(node, GUARANTEE_KEYS)) or meta.get("guarantee_nis"),
            published_date=meta["published_date"],
            closing_date=meta["closing_date"],
            committee_date=meta["committee_date"],
            raw=node,
        )
