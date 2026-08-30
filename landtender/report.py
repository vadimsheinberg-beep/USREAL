"""Сборка дневной сводки: текст для Telegram, консоли и выгрузок."""

from __future__ import annotations

import csv
import json
from datetime import date
from html import escape
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN, Lot, RunResult
from .invest import SIGNAL_BADGES, SIGNAL_CONFIRMED, SIGNAL_LIKELY
from .landuse import AGRICULTURE
from .landuse import badge as landuse_badge
from .renewal import badge as renewal_badge

TIER_TITLES = {
    TIER_PREMIUM: "Дороже порога",
    TIER_STANDARD: "Дешевле порога",
    TIER_UNKNOWN: "Без цены",
}


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.2f} млн".replace(",", " ")
    return f"${value:,.0f}".replace(",", " ")


def fmt_nis(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} ₪".replace(",", " ")


def fmt_nis_short(value: float | None) -> str:
    """Сокращённая сумма для однострочной карточки: «18.5 млн ₪».

    В подробной карточке шекели пишутся полностью — там важна точность.
    В строке-перечислении важнее, чтобы порядок величины читался с одного
    взгляда, а не подсчитывался по разрядам.
    """
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:,.1f} млн ₪".replace(",", " ")
    if value >= 10_000:
        return f"{value / 1_000:,.0f} тыс ₪".replace(",", " ")
    return fmt_nis(value)


#: Насколько цена должна отличаться от оценки, чтобы это стоило отмечать.
#: Ближе — разница тонет в погрешности самой модели.
BARGAIN_RATIO = 0.8
OVERPRICED_RATIO = 1.25


def _price_verdict(lot: Lot) -> str:
    """«Дёшево» или «дорого» относительно оценки — одной короткой фразой.

    Это главный вывод по лоту: цена сама по себе не говорит ничего, пока не
    с чем сравнить. Поэтому вердикт есть в обеих формах карточки.
    """
    if not (lot.estimate_nis and lot.price_nis):
        return ""
    ratio = lot.price_nis / lot.estimate_nis
    if ratio <= BARGAIN_RATIO:
        return f"🟢 −{(1 - ratio) * 100:.0f}% к оценке"
    if ratio >= OVERPRICED_RATIO:
        return f"🔴 +{(ratio - 1) * 100:.0f}% к оценке"
    # Цена рядом с оценкой — не новость. Строка «≈ по оценке» стояла бы почти
    # у каждого лота и превратила бы вердикт в фон, на котором не видно
    # редких 🟢 и 🔴 — ровно тех, ради которых сводку и читают.
    return ""


def fmt_units(lot: Lot) -> str:
    if lot.units is None:
        return "—"
    suffix = "" if lot.units_basis == "reported" else "≈"
    return f"{suffix}{lot.units}"


TIER_ICONS = {TIER_PREMIUM: "🔥", TIER_STANDARD: "▫️", TIER_UNKNOWN: "❔"}


def sort_by_price(lots: Sequence[Lot]) -> list[Lot]:
    """Сначала по баллу полезности, затем по цене.

    Балл отвечает на вопрос «стоит ли этим заниматься», цена — только «сколько
    это стоит». Пока балла нет (нет оценки), порядок прежний, по цене.
    """
    return sorted(
        lots,
        key=lambda lot: (
            lot.score_total is None,
            -(lot.score_total or 0),
            lot.price_usd is None,
            -(lot.price_usd or 0),
        ),
    )


def split_by_tier(lots: Sequence[Lot]) -> dict[str, list[Lot]]:
    """Делит лоты на группы относительно порога в 1 млн долларов."""
    buckets: dict[str, list[Lot]] = {TIER_PREMIUM: [], TIER_STANDARD: [], TIER_UNKNOWN: []}
    for lot in lots:
        buckets.setdefault(lot.tier, []).append(lot)
    return {tier: sort_by_price(bucket) for tier, bucket in buckets.items()}


# --------------------------------------------------------------- Telegram ---


def _score_badge(lot: Lot) -> str:
    """Общий балл со степенью полноты: 3/5 показателей — не то же, что 5/5."""
    if lot.score_total is None:
        return ""
    coverage = f"/{lot.score_coverage}п" if lot.score_coverage else ""
    return f" <b>[{lot.score_total:.0f}{coverage}]</b>"


def _lot_line_html(lot: Lot) -> str:
    name = escape(lot.label)
    link = f'<a href="{escape(lot.url)}">{name}</a>' if lot.url else name
    parts = [f"• {link}{_score_badge(lot)}"]

    money = fmt_usd(lot.price_usd)
    if lot.price_nis:
        money += f" ({fmt_nis(lot.price_nis)})"
    if lot.price_kind:
        money += f" · {escape(lot.price_kind)}"
    elif lot.price_nis is None and lot.opening_date:
        # У неоткрытого тендера цены нет не по нашей вине: портал отвечает
        # «המכרז טרם נפתח להגשת הצעות» и держит מחיר מינימום пустым до открытия.
        money += f" · цена будет с {lot.opening_date}"
    parts.append(f"  💰 {money}")

    detail = [f"🏘 единиц: {fmt_units(lot)}"]
    if lot.area_sqm:
        detail.append(f"📐 {lot.area_sqm:,.0f} м²".replace(",", " "))
    if lot.price_per_unit_usd:
        detail.append(f"за единицу {fmt_usd(lot.price_per_unit_usd)}")
    parts.append("  " + " · ".join(detail))

    # Что нужно иметь на руках сверх цены земли: развитие и банковская гарантия
    extra = []
    if lot.development_costs_nis:
        extra.append(f"развитие {fmt_nis(lot.development_costs_nis)}")
    if lot.guarantee_nis:
        extra.append(f"ערבות {fmt_nis(lot.guarantee_nis)}")
    if extra:
        parts.append("  🏦 " + " · ".join(extra))

    # Оценка по сделкам — всегда вместе с тем, на чём она построена.
    # Число без выборки и R² выглядело бы точнее, чем оно есть.
    if lot.estimate_nis:
        estimate = f"  📊 оценка {fmt_nis(lot.estimate_nis)}"
        if lot.estimate_low_nis and lot.estimate_high_nis:
            estimate += f" ({fmt_nis(lot.estimate_low_nis)} — {fmt_nis(lot.estimate_high_nis)})"
        basis = [f"по {lot.estimate_n} сделк(ам)"] if lot.estimate_n else []
        if lot.estimate_r2 is not None:
            basis.append(f"R²={lot.estimate_r2:.2f}")
        elif lot.estimate_method == "median":
            basis.append("медиана")
        if basis:
            estimate += " · " + ", ".join(basis)
        parts.append(estimate)
        # Сравнение с ценой тендера — то, ради чего оценка и нужна.
        if lot.price_nis:
            ratio = lot.price_nis / lot.estimate_nis
            if ratio <= BARGAIN_RATIO:
                parts.append(f"  🟢 запрошено на {(1 - ratio) * 100:.0f}% ниже оценки")
            elif ratio >= OVERPRICED_RATIO:
                parts.append(f"  🔴 запрошено на {(ratio - 1) * 100:.0f}% выше оценки")

    # Запас прочности: до какой суммы можно поднимать заявку и что будет,
    # если выиграть по минимальной цене.
    if lot.max_bid_nis:
        bid = f"  🎯 предельная ставка {fmt_nis(lot.max_bid_nis)}"
        if lot.bid_headroom_pct is not None:
            sign = "+" if lot.bid_headroom_pct >= 0 else ""
            bid += f" ({sign}{lot.bid_headroom_pct:.0f}% к минимуму)"
        parts.append(bid)
        if lot.roi_at_min is not None:
            parts.append(f"  💼 ROI при выигрыше по минимуму: {lot.roi_at_min * 100:.0f}%")
        # Предельная ставка ниже минимальной цены — участвовать незачем.
        if lot.price_nis and lot.max_bid_nis < lot.price_nis:
            parts.append("  ⚠️ минимальная цена уже выше предельной ставки")

    # Смена назначения — единственное, что делает дешёвую землю дорогой,
    # поэтому она идёт отдельной строкой, а не теряется в хвосте.
    signal = SIGNAL_BADGES.get(lot.plan_signal or "")
    if signal:
        plan = escape(lot.plan_number) if lot.plan_number else ""
        link = f'<a href="{escape(lot.plan_url)}">{plan}</a>' if lot.plan_url and plan else plan
        parts.append(f"  {signal}" + (f" · {link}" if link else ""))

    # Разбивка балла: общий балл без слагаемых нечем проверить.
    breakdown = [
        (title, value)
        for title, value in (
            ("цена", lot.score_price),
            ("назначение", lot.score_rezoning),
            ("плотность", lot.score_density),
            ("рынок", lot.score_market),
            ("срок", lot.score_timing),
        )
        if value is not None
    ]
    if breakdown:
        parts.append("  🧭 " + " · ".join(f"{t} {v:.0f}" for t, v in breakdown))

    tail = []
    field = landuse_badge(lot.land_use)
    if field:
        tail.append(field)
    if lot.zoning:
        tail.append(escape(lot.zoning))
    mark = renewal_badge(lot.renewal_kind)
    if mark:
        area = f", застройка {lot.built_area_sqm:,.0f} м²".replace(",", " ") if lot.built_area_sqm else ""
        tail.append(f"🏚 {escape(mark)}{area}")
    if lot.purpose:
        tail.append(escape(lot.purpose))
    if lot.opening_date and lot.closing_date:
        tail.append(f"заявки {lot.opening_date} — {lot.closing_date}")
    elif lot.closing_date:
        tail.append(f"до {lot.closing_date}")
    if tail:
        parts.append("  " + " · ".join(tail))

    return "\n".join(parts)


#: Сколько лотов в секции показывать подробно. Остальные идут строкой.
#:
#: Пока оценка была редкостью, подробная карточка занимала три строки и
#: длину сводки не определяла. С накопленной базой сравнимых сделок оценку,
#: ставку и балл получает почти каждый лот, и та же карточка выросла до
#: десяти строк: шестьдесят лотов — это девять сообщений подряд, в которых
#: выгодный лот выглядит ровно так же, как проходной. Подробно показываются
#: те, что стоят наверху по баллу; хвост — строкой, но с вердиктом по цене,
#: чтобы решение «открывать или нет» принималось прямо в ленте.
FULL_CARDS = 8


def _lot_compact_html(lot: Lot) -> str:
    """Лот одной строкой: чем он является, сколько стоит и стоит ли смотреть."""
    name = escape(lot.label)
    link = f'<a href="{escape(lot.url)}">{name}</a>' if lot.url else name

    facts = []
    if lot.price_nis:
        facts.append(fmt_nis_short(lot.price_nis))
    elif lot.price_usd:
        facts.append(fmt_usd(lot.price_usd))
    elif lot.opening_date:
        facts.append(f"цена с {lot.opening_date}")
    if lot.units:
        facts.append(f"{fmt_units(lot)} ед.")
    if lot.area_sqm:
        facts.append(fmt_area(lot.area_sqm))

    head = f"• {link}{_score_badge(lot)}"
    if facts:
        head += " · " + " · ".join(facts)

    tail = []
    verdict = _price_verdict(lot)
    if verdict:
        tail.append(verdict)
    field = landuse_badge(lot.land_use)
    if field:
        tail.append(field)
    signal = SIGNAL_BADGES.get(lot.plan_signal or "")
    if signal:
        tail.append(signal)
    if lot.closing_date:
        tail.append(f"до {lot.closing_date}")

    return head + ("\n  " + " · ".join(tail) if tail else "")


def _change_line_html(lot: Lot, changes: dict[str, Any]) -> str:
    name = escape(lot.label)
    link = f'<a href="{escape(lot.url)}">{name}</a>' if lot.url else name
    bits = []
    for field, delta in changes.items():
        before, after = delta.get("before"), delta.get("after")
        if field in {"price_usd"}:
            bits.append(f"цена {fmt_usd(before)} → {fmt_usd(after)}")
        elif field == "price_nis":
            continue  # уже показано в долларах
        elif field == "units":
            bits.append(f"единиц {before or '—'} → {after or '—'}")
        else:
            bits.append(f"{field}: {escape(str(before))} → {escape(str(after))}")
    # Лот считается изменившимся по более широкому набору полей, чем мы
    # расписываем построчно (площадь, назначение, примечания). Без этой
    # оговорки такая строка приходила пустой: «• тендер ♻️» и всё.
    return f"• {link}\n  ♻️ " + ("; ".join(bits) if bits else "обновлены данные тендера")


def build_telegram_digest(
    result: RunResult,
    threshold_usd: float,
    include_standard: bool = True,
    max_per_tier: int = 25,
    include_changes: bool = True,
    split_by_threshold: bool = True,
) -> list[str]:
    """Собирает блоки сообщения. Разбивку по лимиту Telegram делает нотифаер.

    При ``split_by_threshold=False`` выдача не делится по цене: одна секция со
    всеми лотами, дорогие сверху. Цены при этом остаются на месте — убран
    только отбор по ним.
    """
    blocks: list[str] = []
    stats = result.stats()

    fx_line = (
        f"курс USD/ILS: {result.fx_rate:.4f} ({escape(result.fx_source or '?')})"
        if result.fx_rate
        else "курс недоступен"
    )
    header = [
        "<b>🇮🇱 Земельные тендеры Израиля — дневная сводка</b>",
        f"Дата: {result.started_at[:10]}",
        (
            f"Порог: {fmt_usd(threshold_usd)} · {fx_line}"
            if split_by_threshold
            else f"Без отбора по цене и городам · {fx_line}"
        ),
        f"Просмотрено записей: {stats['total_seen']} · новых: {stats['new']} · изменившихся: {stats['changed']}",
    ]
    renewal = [lot for lot in result.new_lots if lot.renewal_kind]
    if renewal:
        header.append(f"🏚 Со строениями / под реконструкцию: {len(renewal)}")
    moving = [
        lot for lot in result.new_lots
        if lot.plan_signal in (SIGNAL_LIKELY, SIGNAL_CONFIRMED)
    ]
    if moving:
        header.append(f"📈 Со сменой назначения: {len(moving)}")
    farmland = [lot for lot in result.new_lots if lot.land_use == AGRICULTURE]
    if farmland:
        area = sum(lot.area_sqm or 0 for lot in farmland)
        size = f", всего {fmt_area(area)}" if area else ""
        header.append(f"🌾 Сельхозземля: {len(farmland)} лот(ов){size}")
    blocks.append("\n".join(header))

    if split_by_threshold:
        buckets = split_by_tier(result.new_lots)
        order = [TIER_PREMIUM] + ([TIER_STANDARD] if include_standard else []) + [TIER_UNKNOWN]
        sections = [(TIER_TITLES[t], TIER_ICONS[t], buckets.get(t, [])) for t in order]
    else:
        sections = [("Все лоты", "📋", sort_by_price(result.new_lots))]

    for title_text, icon, lots in sections:
        if not lots:
            continue
        total_units = sum(lot.units or 0 for lot in lots)
        structures = sum(1 for lot in lots if lot.renewal_kind)
        title = (
            f"{icon} <b>{title_text}</b> — {len(lots)} лот(ов), "
            f"единиц строений: {total_units or '—'}"
        )
        if structures:
            title += f", со строениями: {structures}"
        farm = sum(1 for lot in lots if lot.land_use == AGRICULTURE)
        if farm:
            title += f", сельхоз: {farm}"
        shown = lots[:max_per_tier]
        if len(shown) > FULL_CARDS:
            title += f"\nПодробно — первые {FULL_CARDS} по баллу, остальные строкой"
        lines = [title]
        for position, lot in enumerate(shown):
            lines.append(
                _lot_line_html(lot) if position < FULL_CARDS else _lot_compact_html(lot)
            )
        if len(lots) > max_per_tier:
            lines.append(f"…и ещё {len(lots) - max_per_tier} — полный список в CSV")
        blocks.append("\n".join(lines))

    if include_changes and result.changed_lots:
        lines = [f"♻️ <b>Изменения по ранее найденным</b> — {len(result.changed_lots)}"]
        for lot, changes in result.changed_lots[:max_per_tier]:
            lines.append(_change_line_html(lot, changes))
        blocks.append("\n".join(lines))

    failed = [s for s in result.sources if not s.ok]
    if failed:
        lines = ["⚠️ <b>Источники с ошибкой</b>"]
        for source in failed:
            lines.append(f"• {escape(source.name)}: {escape(source.error or 'неизвестная ошибка')}")
        blocks.append("\n".join(lines))

    return blocks


def fmt_area(sqm: float) -> str:
    """Гектары для полей, метры для мелочи — «0.0 га» ничего не сообщает."""
    if sqm >= 1_000:
        return f"{sqm / 10_000:,.1f} га".replace(",", " ")
    return f"{sqm:,.0f} м²".replace(",", " ")


#: Показатели в табличной строке, в порядке вывода. Имена печатаются в
#: начале сообщения, значения — через запятую под ними: так строка остаётся
#: короткой, а прочесть её можно, не помня порядок наизусть.
TABLE_COLUMNS = (
    "тендер",
    "город",
    "назначение",
    "цена ₪",
    "площадь м²",
    "единиц",
    "оценка ₪",
    "к оценке %",
    "балл",
    "цена",
    "смена назначения",
    "плотность",
    "рынок",
    "срок",
    "ставка ₪",
    "ROI %",
    "подача до",
)


def _num(value: float | None, digits: int = 0) -> str:
    """Число с разделителями разрядов; пустое значение — прочерк, не ноль."""
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", " ")


def _table_row(lot: Lot) -> list[str]:
    """Значения показателей одного лота в порядке ``TABLE_COLUMNS``."""
    deviation = None
    if lot.estimate_nis and lot.price_nis:
        deviation = (lot.price_nis / lot.estimate_nis - 1.0) * 100

    return [
        escape(lot.tender_name or lot.source_id),
        escape(lot.settlement or "—"),
        escape(lot.purpose or "—"),
        _num(lot.price_nis),
        _num(lot.area_sqm),
        fmt_units(lot),
        _num(lot.estimate_nis),
        f"{deviation:+.0f}" if deviation is not None else "—",
        _num(lot.score_total),
        _num(lot.score_price),
        _num(lot.score_rezoning),
        _num(lot.score_density),
        _num(lot.score_market),
        _num(lot.score_timing),
        _num(lot.max_bid_nis),
        _num(lot.roi_at_min * 100) if lot.roi_at_min is not None else "—",
        lot.closing_date or "—",
    ]


def table_lines(lots: Sequence[Lot]) -> list[str]:
    """Лоты таблицей: строка заголовка и по строке значений на лот.

    Название лота остаётся ссылкой — иначе из строки нельзя попасть на сам
    тендер, а проверить утверждение по первоисточнику важнее краткости.
    """
    lines = ["<i>" + ", ".join(TABLE_COLUMNS) + "</i>"]
    for lot in lots:
        values = _table_row(lot)
        if lot.url:
            values[0] = f'<a href="{escape(lot.url)}">{values[0]}</a>'
        lines.append("• " + ", ".join(values))
    return lines


def build_farmland_digest(
    lots: Sequence[Lot],
    max_lots: int = 60,
    only_active: bool = True,
    max_usd: float | None = None,
) -> list[str]:
    """Сводка по сельхозземле из базы — ответ на «покажи всю сельхозземлю».

    Дневная сводка по устройству показывает только новое, поэтому земля,
    найденная неделю назад, в неё уже не попадёт. Здесь наоборот: срез базы,
    отсортированный по баллу и цене.

    ``max_usd`` отсекает дорогое. Лоты, у которых цены ещё нет, под порог не
    подпадают — про них неизвестно, дороги они или дёшевы, — но и молча
    исчезнуть не должны: их число называется в заголовке.
    """
    scope = "действующие тендеры" if only_active else "вся база, включая закрытые"
    priced = [lot for lot in lots if lot.price_usd]
    priceless = [lot for lot in lots if not lot.price_usd]
    if max_usd is not None:
        priced = [lot for lot in priced if (lot.price_usd or 0) <= max_usd]

    shown = sort_by_price(priced)
    # «Ничего не нашлось» и «нашлось, но дороже порога» — разные сообщения:
    # первое про пустую базу, второе про заданное условие.
    if not lots:
        return [f"🌾 <b>Сельхозземля на тендерах</b>\nНичего не найдено ({scope})."]

    area = sum(lot.area_sqm or 0 for lot in shown)
    header = [
        "🌾 <b>Сельхозземля на тендерах</b>",
        f"Дата: {date.today().isoformat()} · {scope}",
    ]
    if max_usd is not None:
        header.append(f"Порог цены: до {fmt_usd(max_usd)}")
    header.append(
        f"Лотов: {len(shown)}" + (f" · площадь: {fmt_area(area)}" if area else "")
    )
    if shown:
        total = sum(lot.price_usd or 0 for lot in shown)
        header.append(f"Общая цена: {fmt_usd(total)}")
    if priceless:
        header.append(
            f"Без объявленной цены: {len(priceless)} — попадут в срез, когда цена появится"
        )

    blocks = ["\n".join(header)]
    if not shown:
        blocks.append("Под порог цены пока ничего не подходит.")
        return blocks

    lines = table_lines(shown[:max_lots])
    if len(shown) > max_lots:
        lines.append(f"…и ещё {len(shown) - max_lots}")
    blocks.append("\n".join(lines))
    return blocks


def build_city_digest(
    lots: Sequence[Lot],
    city: str,
    max_usd: float | None = None,
    max_lots: int = 60,
    only_active: bool = True,
) -> list[str]:
    """Срез по городу и назначению — «что есть в Иерусалиме до миллиона».

    Заголовок называет вещи своими именами: это участки земельных торгов, а
    не квартиры вторичного рынка. Реестр рм"и торгует землёй; принять одно за
    другое — самая дорогая ошибка, которую здесь можно сделать.
    """
    scope = "действующие тендеры" if only_active else "вся база, включая закрытые"
    shown = sort_by_price(lots)
    header = [
        f"🏙 <b>Участки под жильё · {escape(city)}</b>",
        f"Дата: {date.today().isoformat()} · {scope}",
    ]
    if max_usd is not None:
        header.append(f"Порог цены: до {fmt_usd(max_usd)}")
    header.append("Это участки земельных торгов, а не квартиры вторичного рынка.")

    if not shown:
        header.append("Под условия ничего не подошло.")
        return ["\n".join(header)]

    total = sum(lot.price_usd or 0 for lot in shown if lot.price_usd)
    header.append(f"Лотов: {len(shown)}" + (f" · общая цена: {fmt_usd(total)}" if total else ""))

    blocks = ["\n".join(header)]
    lines = table_lines(shown[:max_lots])
    if len(shown) > max_lots:
        lines.append(f"…и ещё {len(shown) - max_lots}")
    blocks.append("\n".join(lines))
    return blocks


def build_top_digest(lots: Sequence[Lot], limit: int = 10, only_active: bool = True) -> list[str]:
    """Лучшие предложения из базы — по баллу, все показатели в строке.

    В отличие от дневной сводки здесь не новинки, а лучшее из накопленного.
    Номера мест не украшение: это и есть содержание — порядок по баллу.
    """
    scope = "действующие тендеры" if only_active else "вся база, включая закрытые"
    if not lots:
        return [
            "🏆 <b>Лучшие предложения</b>\n"
            f"Пока нечего показать ({scope}): ни у одного лота нет балла. "
            "Балл считается по оценке, а оценка — по сделкам из архива: "
            "<code>landtender harvest</code>."
        ]

    shown = list(lots[:limit])
    header = [
        f"🏆 <b>Лучшие предложения — топ-{len(shown)}</b>",
        f"Дата: {date.today().isoformat()} · {scope}",
        "Порядок по общему баллу",
    ]
    priced = [lot for lot in shown if lot.price_usd]
    if priced:
        total = sum(lot.price_usd or 0 for lot in priced)
        header.append(f"С ценой: {len(priced)} на {fmt_usd(total)}")
    bargains = [lot for lot in shown if _price_verdict(lot).startswith("🟢")]
    if bargains:
        header.append(f"🟢 Дешевле оценки более чем на 20%: {len(bargains)}")

    blocks = ["\n".join(header)]
    lines = table_lines(shown)
    # Номер места заменяет маркер списка: порядок здесь и есть содержание.
    numbered = [lines[0]] + [
        (f"<b>{place}.</b> " + line[2:]) if line.startswith("• ") else line
        for place, line in enumerate(lines[1:], start=1)
    ]
    blocks.append("\n".join(numbered))
    return blocks


def preview_messages(blocks: Sequence[str]) -> str:
    """Показывает сводку так, как она придёт в канал — по сообщениям, а не блокам."""
    from .notify.telegram import chunk_blocks

    messages = chunk_blocks(blocks)
    parts = []
    for number, message in enumerate(messages, start=1):
        parts.append(f"───── сообщение {number}/{len(messages)} ({len(message)} симв.) ─────")
        parts.append(message)
    return "\n".join(parts)


# ---------------------------------------------------------------- консоль ---


def build_console_report(
    result: RunResult, threshold_usd: float, split_by_threshold: bool = True
) -> str:
    stats = result.stats()
    lines = [
        "=" * 72,
        "ЗЕМЕЛЬНЫЕ ТЕНДЕРЫ ИЗРАИЛЯ — сводка за " + result.started_at[:10],
        "=" * 72,
        f"Курс USD/ILS: {result.fx_rate} ({result.fx_source}, {result.fx_date})",
        f"Порог фильтра: {fmt_usd(threshold_usd)}" if split_by_threshold
        else "Отбор по цене и городам отключён",
        "",
        "Источники:",
    ]
    for source in result.sources:
        status = "ok " if source.ok else "ERR"
        note = f" — {source.error}" if source.error else ""
        dropped = []
        if source.skipped_expired:
            dropped.append(f"просрочено: {source.skipped_expired}")
        if source.skipped_elsewhere:
            dropped.append(f"другие города: {source.skipped_elsewhere}")
        expired = f" ({', '.join(dropped)})" if dropped else ""
        lines.append(
            f"  [{status}] {source.name:<16} лотов: {source.lots:<5} "
            f"{source.duration_sec:.1f}с{expired}{note}"
        )

    lines += ["", f"Всего записей: {stats['total_seen']}, новых: {stats['new']}, изменилось: {stats['changed']}", ""]

    if split_by_threshold:
        buckets = split_by_tier(result.new_lots)
        groups = [(TIER_TITLES[t], buckets.get(t, [])) for t in (TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN)]
    else:
        groups = [("Все лоты", sort_by_price(result.new_lots))]

    for group_title, lots in groups:
        if not lots:
            continue
        total_units = sum(lot.units or 0 for lot in lots)
        structures = sum(1 for lot in lots if lot.renewal_kind)
        lines.append(
            f"--- {group_title}: {len(lots)} лот(ов), единиц строений: {total_units}"
            f", со строениями: {structures} ---"
        )
        for lot in lots[:50]:
            mark = renewal_badge(lot.renewal_kind)
            suffix = f"  🏚 {mark}" if mark else ""
            lines.append(
                f"  {fmt_usd(lot.price_usd):>16}  ед.: {fmt_units(lot):>5}  {lot.label}{suffix}"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- выгрузки --

EXPORT_FIELDS = (
    "uid", "source", "tender_id", "tender_name", "settlement", "neighborhood",
    "gush", "chelka", "purpose", "status", "area_sqm", "built_area_sqm", "renewal_kind",
    "has_structure", "land_use", "zoning",
    "plan_signal", "plan_number", "plan_url",
    "estimate_nis", "estimate_low_nis", "estimate_high_nis",
    "estimate_n", "estimate_r2", "estimate_method",
    "score_total", "score_price", "score_rezoning", "score_density",
    "score_market", "score_timing", "score_coverage",
    "max_bid_nis", "bid_headroom_pct", "roi_at_min",
    "units", "units_basis",
    "price_nis", "price_kind", "development_costs_nis", "guarantee_nis", "price_usd", "price_per_unit_usd", "price_per_sqm_usd",
    "tier", "published_date", "opening_date", "closing_date", "url",
)


def export_csv(lots: Iterable[Lot | dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(EXPORT_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for lot in lots:
            row = lot.to_dict() if isinstance(lot, Lot) else dict(lot)
            writer.writerow({key: row.get(key) for key in EXPORT_FIELDS})
    return path


def export_json(lots: Iterable[Lot | dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [lot.to_dict() if isinstance(lot, Lot) else dict(lot) for lot in lots]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
