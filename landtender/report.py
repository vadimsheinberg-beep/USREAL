"""Сборка дневной сводки: текст для Telegram, консоли и выгрузок."""

from __future__ import annotations

import csv
import json
from html import escape
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN, Lot, RunResult
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


def fmt_units(lot: Lot) -> str:
    if lot.units is None:
        return "—"
    suffix = "" if lot.units_basis == "reported" else "≈"
    return f"{suffix}{lot.units}"


TIER_ICONS = {TIER_PREMIUM: "🔥", TIER_STANDARD: "▫️", TIER_UNKNOWN: "❔"}


def sort_by_price(lots: Sequence[Lot]) -> list[Lot]:
    """Дорогие сверху, лоты без цены — в конце."""
    return sorted(lots, key=lambda lot: (lot.price_usd is None, -(lot.price_usd or 0)))


def split_by_tier(lots: Sequence[Lot]) -> dict[str, list[Lot]]:
    """Делит лоты на группы относительно порога в 1 млн долларов."""
    buckets: dict[str, list[Lot]] = {TIER_PREMIUM: [], TIER_STANDARD: [], TIER_UNKNOWN: []}
    for lot in lots:
        buckets.setdefault(lot.tier, []).append(lot)
    return {tier: sort_by_price(bucket) for tier, bucket in buckets.items()}


# --------------------------------------------------------------- Telegram ---


def _lot_line_html(lot: Lot) -> str:
    name = escape(lot.label)
    link = f'<a href="{escape(lot.url)}">{name}</a>' if lot.url else name
    parts = [f"• {link}"]

    money = fmt_usd(lot.price_usd)
    if lot.price_nis:
        money += f" ({fmt_nis(lot.price_nis)})"
    if lot.price_kind:
        money += f" · {escape(lot.price_kind)}"
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

    tail = []
    mark = renewal_badge(lot.renewal_kind)
    if mark:
        area = f", застройка {lot.built_area_sqm:,.0f} м²".replace(",", " ") if lot.built_area_sqm else ""
        tail.append(f"🏚 {escape(mark)}{area}")
    if lot.purpose:
        tail.append(escape(lot.purpose))
    if lot.closing_date:
        tail.append(f"до {lot.closing_date}")
    if tail:
        parts.append("  " + " · ".join(tail))

    return "\n".join(parts)


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
    return f"• {link}\n  ♻️ " + "; ".join(bits)


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
        lines = [title]
        for lot in lots[:max_per_tier]:
            lines.append(_lot_line_html(lot))
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
    "has_structure", "units", "units_basis",
    "price_nis", "price_kind", "development_costs_nis", "guarantee_nis", "price_usd", "price_per_unit_usd", "price_per_sqm_usd",
    "tier", "published_date", "closing_date", "url",
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
