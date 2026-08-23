"""Демонстрационная сводка на вымышленных данных.

Нужна, чтобы увидеть формат сообщения в своём канале сразу после настройки,
не дожидаясь ответа государственных порталов. Данные придуманы и в базу не
попадают; сообщение помечено предупреждением, чтобы его нельзя было принять
за реальные тендеры.
"""

from __future__ import annotations

from .landuse import AGRICULTURE
from .models import (
    PRICE_KIND_ASKING,
    PRICE_KIND_FINAL,
    PRICE_KIND_MIN,
    UNITS_INFERRED,
    UNITS_REPORTED,
    Lot,
    RunResult,
    SourceReport,
)
from .money import FxRate, enrich_lot

DEMO_WARNING = (
    "⚠️ <b>Демонстрационная сводка</b>\n"
    "Данные ниже вымышлены и показаны только для проверки формата и "
    "подключения канала. Реальные лоты придут после <code>landtender run</code>."
)

FX_RATE = 3.6412


def _demo_lots() -> list[Lot]:
    """Лоты на все ветки отчёта: цена, реконструкция, сельхозземля, пустые поля."""
    return [
        Lot(
            source="rmi_michrazim",
            source_id="demo:1",
            tender_id="20250142",
            tender_name="חי/142/2025",
            url="https://apps.land.gov.il/MichrazimSite/",
            settlement="חיפה",
            neighborhood="נווה שאנן",
            purpose="מגורים",
            status="פתוח",
            gush="10769",
            chelka="42",
            area_sqm=4200.0,
            units=60,
            units_basis=UNITS_REPORTED,
            price_nis=18_500_000.0,
            price_kind=PRICE_KIND_MIN,
            development_costs_nis=3_400_000.0,
            closing_date="2026-09-15",
        ),
        Lot(
            source="rmi_michrazim",
            source_id="demo:2",
            tender_id="20250142",
            tender_name="חי/142/2025",
            url="https://apps.land.gov.il/MichrazimSite/",
            settlement="חיפה",
            neighborhood="נווה שאנן",
            purpose="מגורים",
            gush="10769",
            chelka="43",
            area_sqm=1850.0,
            units=24,
            units_basis=UNITS_REPORTED,
            price_nis=3_610_000.0,
            price_kind=PRICE_KIND_FINAL,
            closing_date="2026-09-15",
        ),
        Lot(
            source="rmi_michrazim",
            source_id="demo:3",
            tender_id="20250143",
            tender_name="מר/143/2025",
            url="https://apps.land.gov.il/MichrazimSite/",
            settlement="מודיעין",
            neighborhood="בוכמן",
            purpose="מגרש לבניית בית קרקע",
            area_sqm=520.0,
            units=1,
            units_basis=UNITS_INFERRED,
            price_nis=1_450_000.0,
            price_kind=PRICE_KIND_MIN,
            closing_date="2026-08-30",
        ),
        Lot(
            source="yad2",
            source_id="demo:4",
            tender_name="מגרש למכירה, רחוב הזית",
            url="https://www.yad2.co.il/realestate/forsale",
            settlement="כפר סבא",
            neighborhood="הדרים",
            purpose="מגרשים",
            area_sqm=780.0,
            price_nis=6_200_000.0,
            price_kind=PRICE_KIND_ASKING,
        ),
        Lot(
            source="rmi_michrazim",
            source_id="demo:6",
            tender_id="20250145",
            tender_name="מכרז פינוי בינוי 88/2026",
            url="https://apps.land.gov.il/MichrazimSite/",
            settlement="נתניה",
            neighborhood="קרית השרון",
            purpose="התחדשות עירונית",
            area_sqm=6400.0,
            built_area_sqm=11200.0,
            renewal_kind="pinui_binui",
            has_structure=True,
            units=180,
            units_basis=UNITS_REPORTED,
            price_nis=24_000_000.0,
            price_kind=PRICE_KIND_MIN,
            development_costs_nis=7_100_000.0,
            guarantee_nis=2_400_000.0,
            closing_date="2026-10-20",
        ),
        Lot(
            source="rmi_michrazim",
            source_id="demo:7",
            tender_id="20250151",
            tender_name="מכרז חקלאי 151/2026",
            url="https://apps.land.gov.il/MichrazimSite/",
            settlement="בקעת בית שאן",
            purpose="חקלאות — מטעים",
            status="פתוח",
            land_use=AGRICULTURE,
            area_sqm=145_000.0,
            price_nis=2_900_000.0,
            price_kind=PRICE_KIND_MIN,
            opening_date="2026-10-12",
            closing_date="2026-11-05",
        ),
        Lot(
            source="rmi_michrazim",
            source_id="demo:8",
            tender_id="20250406",
            tender_name="406/2025",
            url="https://apps.land.gov.il/MichrazimSite/",
            settlement="ירושלים",
            neighborhood="הכניסה לעיר",
            purpose="חקלאות",
            status="פתוח",
            land_use=AGRICULTURE,
            # Тендер объявлен, но приём заявок ещё не начался — портал держит
            # מחיר מינימום пустым до открытия. Так это и выглядит в сводке.
            opening_date="2026-10-26",
            closing_date="2026-12-28",
        ),
        Lot(
            source="gov_mr",
            source_id="demo:5",
            tender_name="מכרז מקרקעין: מגרש בעכו",
            url="https://mr.gov.il/",
            settlement="עכו",
            units=12,
            units_basis=UNITS_INFERRED,
        ),
    ]


def demo_result(threshold_usd: float = 1_000_000.0) -> RunResult:
    """Готовый результат запуска для демонстрации, включая ошибку источника.

    Долларовые суммы и группы считает тот же ``enrich_lot``, что и рабочий
    конвейер, — демонстрация не расходится с настроенным порогом.
    """
    fx = FxRate(rate=FX_RATE, as_of="2026-07-27", source="boi")
    lots = [enrich_lot(lot, fx, threshold_usd) for lot in _demo_lots()]
    changed = [
        (
            lots[0],
            {
                "price_usd": {"before": 5_400_000.0, "after": 5_080_742.0},
                "status": {"before": "טרם פורסם", "after": "פתוח"},
            },
        )
    ]
    return RunResult(
        started_at="2026-07-27T06:00:00+00:00",
        finished_at="2026-07-27T06:04:00+00:00",
        sources=[
            SourceReport(name="rmi_michrazim", ok=True, lots=3, duration_sec=42.1),
            SourceReport(name="data_gov_il", ok=True, lots=0, duration_sec=6.4),
            SourceReport(name="gov_mr", ok=True, lots=1, duration_sec=11.2),
            SourceReport(name="yad2", ok=False, error="HTTP 403 (защита от ботов)", duration_sec=3.0),
        ],
        new_lots=lots,
        changed_lots=changed,
        total_seen=412,
        fx_rate=FX_RATE,
        fx_date="2026-07-27",
        fx_source="boi",
    )


def demo_blocks(
    threshold_usd: float = 1_000_000.0, split_by_threshold: bool = True
) -> list[str]:
    """Блоки сообщения для демонстрации — с предупреждением в начале.

    Настройка деления по цене передаётся насквозь: демонстрация должна
    выглядеть так же, как настоящая сводка при текущем конфиге.
    """
    from .report import build_telegram_digest

    blocks = build_telegram_digest(
        demo_result(threshold_usd),
        threshold_usd=threshold_usd,
        split_by_threshold=split_by_threshold,
    )
    return [DEMO_WARNING, *blocks]
