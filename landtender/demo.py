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
            zoning="קרקע חקלאית",
            area_sqm=145_000.0,
            price_nis=2_900_000.0,
            price_kind=PRICE_KIND_MIN,
            opening_date="2026-10-12",
            closing_date="2026-11-05",
            # Поле, которое переводят под застройку: депонированный план и
            # ссылка на него, чтобы утверждение можно было проверить самому.
            plan_signal="likely",
            plan_number="353-0061416",
            plan_url="https://mavat.iplan.gov.il/SV4/1/3000220263/310",
            # Оценка по сделкам с соседними участками: запрошено заметно
            # ниже — именно такие лоты и надо замечать.
            estimate_nis=4_100_000.0,
            estimate_low_nis=3_400_000.0,
            estimate_high_nis=4_900_000.0,
            estimate_n=23,
            estimate_r2=0.58,
            estimate_method="regression",
            # Пять показателей и запас прочности: до 3.4 млн ₪ ставка ещё
            # даёт целевую доходность, минимум — 2.9 млн.
            score_total=84.0,
            score_price=95.0,
            score_rezoning=70.0,
            score_density=None,
            score_market=77.0,
            score_timing=100.0,
            score_coverage=4,
            max_bid_nis=3_390_000.0,
            bid_headroom_pct=16.9,
            roi_at_min=32.0 / 100,
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
        # Хвост сводки: лоты с оценкой, но без выдающегося балла. Показаны
        # строкой — так видно обе формы карточки сразу, подробную и короткую.
        Lot(
            source="rmi_michrazim",
            source_id="demo:9",
            tender_id="20250161",
            tender_name="161/2026",
            url="https://apps.land.gov.il/MichrazimSite/",
            settlement="באר שבע",
            purpose="מגורים",
            area_sqm=3_100.0,
            units=36,
            units_basis=UNITS_REPORTED,
            price_nis=7_800_000.0,
            price_kind=PRICE_KIND_MIN,
            closing_date="2026-11-18",
            estimate_nis=10_900_000.0,
            estimate_n=64,
            estimate_r2=0.41,
            estimate_method="regression",
            score_total=52.0,
            score_price=71.0,
            score_market=44.0,
            score_coverage=3,
        ),
        Lot(
            source="rmi_michrazim",
            source_id="demo:10",
            tender_id="20250162",
            tender_name="162/2026",
            url="https://apps.land.gov.il/MichrazimSite/",
            settlement="דימונה",
            purpose="תעסוקה",
            area_sqm=9_400.0,
            price_nis=4_200_000.0,
            price_kind=PRICE_KIND_MIN,
            closing_date="2026-10-08",
            estimate_nis=4_050_000.0,
            estimate_n=31,
            estimate_r2=0.28,
            estimate_method="regression",
            score_total=38.0,
            score_price=40.0,
            score_market=36.0,
            score_coverage=2,
        ),
        Lot(
            source="rmi_michrazim",
            source_id="demo:11",
            tender_id="20250163",
            tender_name="163/2026",
            url="https://apps.land.gov.il/MichrazimSite/",
            settlement="טבריה",
            purpose="חקלאות",
            land_use=AGRICULTURE,
            area_sqm=62_000.0,
            price_nis=6_100_000.0,
            price_kind=PRICE_KIND_MIN,
            closing_date="2026-12-01",
            estimate_nis=4_400_000.0,
            estimate_n=48,
            estimate_r2=0.35,
            estimate_method="regression",
            plan_signal="early",
            score_total=29.0,
            score_price=12.0,
            score_rezoning=35.0,
            score_market=44.0,
            score_coverage=3,
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
