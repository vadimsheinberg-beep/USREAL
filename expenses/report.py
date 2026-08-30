"""Рендер отчётов: текст для терминала, Markdown, CSV и JSON."""

from __future__ import annotations

import csv
import io
import json
from typing import Iterable, Sequence

from .analyze import (
    MonthStats,
    RecurringCharge,
    category_trends,
    compare_months,
    find_recurring,
    monthly_summary,
    overall_stats,
    top_expenses,
)
from .models import Transaction

_SYMBOLS = {"ILS": "₪", "USD": "$", "EUR": "€", "RUB": "₽", "GBP": "£"}


def money(value: float, currency: str = "ILS") -> str:
    symbol = _SYMBOLS.get(currency.upper(), currency.upper())
    return f"{value:,.2f} {symbol}".replace(",", " ")


def _bar(value: float, peak: float, width: int = 24) -> str:
    if peak <= 0:
        return ""
    return "█" * max(1, round(value / peak * width)) if value > 0 else ""


def render_text(
    transactions: Sequence[Transaction],
    *,
    currency: str = "ILS",
    top: int = 10,
    show_recurring: bool = True,
    show_unknown: bool = True,
) -> str:
    """Полный отчёт для терминала."""
    out: list[str] = []
    if not transactions:
        return "Нет операций за выбранный период."

    summary = monthly_summary(transactions)
    totals = overall_stats(summary)
    start, end = transactions[0].date, transactions[-1].date

    out.append("=" * 64)
    out.append(f"РАСХОДЫ: {start.isoformat()} — {end.isoformat()}")
    out.append("=" * 64)
    out.append(f"Месяцев в периоде:  {int(totals['months'])}")
    out.append(f"Всего потрачено:    {money(totals['expense_total'], currency)}")
    out.append(f"В среднем за месяц: {money(totals['expense_avg'], currency)}")
    if totals.get("income_total"):
        out.append(f"Поступления:        {money(totals['income_total'], currency)}")
        out.append(f"Баланс за период:   {money(totals['net_total'], currency)}")
    out.append("")

    out.append("ПО МЕСЯЦАМ")
    out.append("-" * 64)
    peak = max((s.expense for s in summary), default=0.0)
    for stats in summary:
        out.append(
            f"  {stats.month}  {money(stats.expense, currency):>16}  "
            f"{stats.count:>4} оп.  {_bar(stats.expense, peak)}"
        )
    out.append("")

    out.append("ПО КАТЕГОРИЯМ ЗА ВЕСЬ ПЕРИОД")
    out.append("-" * 64)
    trends = category_trends(summary)
    grand_total = sum(t.total for t in trends)
    peak = max((t.total for t in trends), default=0.0)
    for trend in trends:
        share = trend.total / grand_total * 100 if grand_total else 0.0
        out.append(
            f"  {trend.category:<22} {money(trend.total, currency):>16} "
            f"{share:5.1f}%  ср/мес {money(trend.average, currency):>14}"
        )
    out.append("")

    if len(summary) >= 2:
        prev, cur = summary[-2], summary[-1]
        out.append(f"ЧТО ИЗМЕНИЛОСЬ: {prev.month} → {cur.month}")
        out.append("-" * 64)
        for category, was, now in compare_months(prev, cur)[:top]:
            delta = now - was
            if abs(delta) < 0.01:
                continue
            sign = "+" if delta > 0 else "−"
            pct = f"{delta / was * 100:+.0f}%" if was else "новая"
            out.append(
                f"  {category:<22} {money(was, currency):>14} → {money(now, currency):>14}"
                f"  {sign}{money(abs(delta), currency)} ({pct})"
            )
        out.append("")

    out.append(f"КРУПНЕЙШИЕ ТРАТЫ (топ {top})")
    out.append("-" * 64)
    for tx in top_expenses(transactions, top):
        out.append(
            f"  {tx.date.isoformat()}  {money(tx.report_amount, currency):>14}  "
            f"{tx.category:<18} {tx.description[:32]}"
        )
    out.append("")

    if show_recurring:
        recurring = find_recurring(transactions)
        if recurring:
            out.append("РЕГУЛЯРНЫЕ СПИСАНИЯ (подписки, аренда, страховки)")
            out.append("-" * 64)
            yearly = 0.0
            for item in recurring:
                yearly += item.yearly_estimate
                out.append(
                    f"  {item.merchant[:26]:<26} {money(item.typical_amount, currency):>14}/мес"
                    f"  {item.months_count} мес.  [{item.category}]"
                )
            out.append(f"  {'':<26} {money(yearly, currency):>14}/год всего")
            out.append("")

    if show_unknown:
        unknown = [tx for tx in transactions if tx.category_rule is None and tx.is_expense]
        if unknown:
            total_unknown = sum(tx.report_amount for tx in unknown)
            out.append(
                f"БЕЗ КАТЕГОРИИ: {len(unknown)} оп. на {money(total_unknown, currency)}"
            )
            out.append("-" * 64)
            worst = sorted(unknown, key=lambda t: -t.report_amount)[:top]
            for tx in worst:
                out.append(
                    f"  {money(tx.report_amount, currency):>14}  {tx.description[:44]}"
                )
            out.append("  Добавьте правила в expenses.toml, чтобы разнести их.")
            out.append("")

    return "\n".join(out)


def render_markdown(
    transactions: Sequence[Transaction], *, currency: str = "ILS", top: int = 10
) -> str:
    """Markdown — удобно вставить в заметки или отправить в мессенджер."""
    if not transactions:
        return "_Нет операций за выбранный период._"

    summary = monthly_summary(transactions)
    totals = overall_stats(summary)
    trends = category_trends(summary)
    months = [s.month for s in summary]

    out = [
        f"# Расходы {transactions[0].date.isoformat()} — {transactions[-1].date.isoformat()}",
        "",
        f"- Всего: **{money(totals['expense_total'], currency)}**",
        f"- В среднем за месяц: **{money(totals['expense_avg'], currency)}**",
        "",
        "## По месяцам",
        "",
        "| Месяц | Расход | Доход | Операций |",
        "|---|---:|---:|---:|",
    ]
    for stats in summary:
        out.append(
            f"| {stats.month} | {money(stats.expense, currency)} | "
            f"{money(stats.income, currency)} | {stats.count} |"
        )

    out += ["", "## По категориям", "", "| Категория | " + " | ".join(months) + " | Итого |"]
    out.append("|---|" + "---:|" * (len(months) + 1))
    for trend in trends:
        cells = " | ".join(f"{trend.by_month[m]:,.0f}".replace(",", " ") for m in months)
        out.append(f"| {trend.category} | {cells} | **{money(trend.total, currency)}** |")

    recurring = find_recurring(transactions)
    if recurring:
        out += ["", "## Регулярные списания", "", "| Мерчант | В месяц | Месяцев | Категория |"]
        out.append("|---|---:|---:|---|")
        for item in recurring:
            out.append(
                f"| {item.merchant} | {money(item.typical_amount, currency)} | "
                f"{item.months_count} | {item.category} |"
            )

    out += ["", f"## Крупнейшие траты (топ {top})", "", "| Дата | Сумма | Категория | Описание |"]
    out.append("|---|---:|---|---|")
    for tx in top_expenses(transactions, top):
        out.append(
            f"| {tx.date.isoformat()} | {money(tx.report_amount, currency)} | "
            f"{tx.category} | {tx.description} |"
        )

    return "\n".join(out) + "\n"


def render_csv(transactions: Iterable[Transaction]) -> str:
    """Плоская выгрузка операций — чтобы дальше крутить в Excel."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["date", "month", "amount", "currency", "direction", "category", "merchant", "description", "source"]
    )
    for tx in transactions:
        writer.writerow(
            [
                tx.date.isoformat(),
                tx.month,
                f"{tx.amount:.2f}",
                tx.currency,
                tx.direction,
                tx.category,
                tx.merchant or "",
                tx.description,
                tx.source,
            ]
        )
    return buf.getvalue()


def render_json(transactions: Sequence[Transaction], *, currency: str = "ILS") -> str:
    """Структурированный отчёт — если поверх нужен свой график или бот."""
    summary = monthly_summary(transactions)
    payload = {
        "currency": currency,
        "period": {
            "from": transactions[0].date.isoformat() if transactions else None,
            "to": transactions[-1].date.isoformat() if transactions else None,
        },
        "totals": overall_stats(summary),
        "months": [
            {
                "month": s.month,
                "expense": round(s.expense, 2),
                "income": round(s.income, 2),
                "count": s.count,
                "by_category": {k: round(v, 2) for k, v in s.top_categories(100)},
            }
            for s in summary
        ],
        "recurring": [
            {
                "merchant": r.merchant,
                "category": r.category,
                "typical_amount": round(r.typical_amount, 2),
                "months": r.months,
                "yearly_estimate": round(r.yearly_estimate, 2),
            }
            for r in find_recurring(transactions)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_recurring(items: Sequence[RecurringCharge], currency: str = "ILS") -> str:
    """Отдельный вывод для команды ``expenses recurring``."""
    if not items:
        return "Регулярных списаний не найдено."
    lines = ["Регулярные списания:", "-" * 56]
    for item in items:
        lines.append(
            f"  {item.merchant[:26]:<26} {money(item.typical_amount, currency):>14}/мес"
            f"  ~{money(item.yearly_estimate, currency)}/год"
        )
    total = sum(i.yearly_estimate for i in items)
    lines.append("-" * 56)
    lines.append(f"  Итого в год: {money(total, currency)}")
    return "\n".join(lines)
