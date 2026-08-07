"""HTML-отчёт: сводная таблица «категории × месяцы» и разбор периода.

Отдельный модуль, потому что вёрстка длинная и в :mod:`expenses.report`
она мешала бы читать текстовые рендеры. Страница самодостаточная: стили
внутри, внешних запросов нет, файл открывается двойным кликом и работает
без интернета — данные о расходах никуда не уходят.
"""

from __future__ import annotations

import html
from datetime import date
from typing import Sequence

from .analyze import (
    category_trends,
    compare_months,
    find_recurring,
    monthly_summary,
    overall_stats,
    top_expenses,
)
from .models import Transaction
from .report import money

#: Палитра ledger-бумаги: тёплый нейтральный фон, бледно-зелёная полоса,
#: чернильно-зелёный акцент. Оба режима описаны токенами, поэтому вёрстка
#: ниже не знает, светлая тема или тёмная.
STYLE = """
:root {
  color-scheme: light dark;
  --paper: #fbfbf7;
  --card: #ffffff;
  --stripe: #f1f5ed;
  --ink: #1b1f1a;
  --ink-soft: #4a5046;
  --ink-muted: #767c71;
  --rule: #dde3d7;
  --rule-strong: #c3cbbb;
  --accent: #2e6b4c;
  --accent-soft: rgba(46, 107, 76, 0.14);
  --up: #a34327;
  --down: #2e6b4c;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
          "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #111310;
    --card: #171a15;
    --stripe: #1c201a;
    --ink: #e7eae3;
    --ink-soft: #b6bcb1;
    --ink-muted: #8d9388;
    --rule: #272c24;
    --rule-strong: #3a4136;
    --accent: #74c295;
    --accent-soft: rgba(116, 194, 149, 0.16);
    --up: #e28a66;
    --down: #74c295;
  }
}
:root[data-theme="dark"] {
  --paper: #111310;
  --card: #171a15;
  --stripe: #1c201a;
  --ink: #e7eae3;
  --ink-soft: #b6bcb1;
  --ink-muted: #8d9388;
  --rule: #272c24;
  --rule-strong: #3a4136;
  --accent: #74c295;
  --accent-soft: rgba(116, 194, 149, 0.16);
  --up: #e28a66;
  --down: #74c295;
}
:root[data-theme="light"] {
  --paper: #fbfbf7;
  --card: #ffffff;
  --stripe: #f1f5ed;
  --ink: #1b1f1a;
  --ink-soft: #4a5046;
  --ink-muted: #767c71;
  --rule: #dde3d7;
  --rule-strong: #c3cbbb;
  --accent: #2e6b4c;
  --accent-soft: rgba(46, 107, 76, 0.14);
  --up: #a34327;
  --down: #2e6b4c;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 40px 24px 72px;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.sheet {
  max-width: 1120px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 40px;
}

.masthead { display: flex; flex-direction: column; gap: 6px; }
.eyebrow {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink-muted);
}
h1 {
  margin: 0;
  font-family: var(--mono);
  font-size: 26px;
  font-weight: 600;
  letter-spacing: -0.01em;
  text-wrap: balance;
}
.period { color: var(--ink-soft); font-size: 14px; }

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 1px;
  background: var(--rule);
  border: 1px solid var(--rule);
  border-radius: 4px;
  overflow: hidden;
}
.tile { background: var(--card); padding: 16px 18px; display: flex; flex-direction: column; gap: 4px; }
.tile dt {
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink-muted);
}
.tile dd {
  margin: 0;
  font-family: var(--mono);
  font-size: 21px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.tile .note { font-size: 12.5px; color: var(--ink-soft); font-family: var(--sans); }

section { display: flex; flex-direction: column; gap: 12px; }
h2 {
  margin: 0;
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-soft);
  padding-bottom: 8px;
  border-bottom: 1px solid var(--rule-strong);
}
.hint { margin: 0; font-size: 13px; color: var(--ink-muted); max-width: 68ch; }

.scroller { overflow-x: auto; border: 1px solid var(--rule); border-radius: 4px; background: var(--card); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { padding: 9px 14px; text-align: right; white-space: nowrap; }
thead th {
  position: sticky;
  top: 0;
  background: var(--card);
  font-family: var(--mono);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ink-muted);
  border-bottom: 1px solid var(--rule-strong);
}
th.rowhead, td.rowhead {
  text-align: left;
  position: sticky;
  left: 0;
  background: var(--card);
  font-weight: 500;
  white-space: nowrap;
}
tbody tr:nth-child(even) td { background: var(--stripe); }
tbody tr:nth-child(even) td.rowhead { background: var(--stripe); }
td.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
td.zero { color: var(--ink-muted); }
tfoot td {
  border-top: 1px solid var(--rule-strong);
  font-weight: 600;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  background: var(--card);
}
tfoot td.rowhead { background: var(--card); }

.meter { display: flex; align-items: center; gap: 8px; justify-content: flex-end; }
/* Полоски — span'ы внутри таблицы: без display:block ширина и высота к ним не применяются. */
.meter-track { display: block; width: 76px; height: 7px; background: var(--accent-soft); border-radius: 0 4px 4px 0; }
.meter-fill { display: block; height: 100%; background: var(--accent); border-radius: 0 4px 4px 0; }
.meter-value { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 13px; min-width: 44px; }

.months { display: flex; flex-direction: column; gap: 2px; }
.month-row { display: grid; grid-template-columns: 74px 1fr auto; align-items: center; gap: 14px; padding: 5px 2px; }
.month-row .label { font-family: var(--mono); font-size: 13px; color: var(--ink-soft); }
.month-row .track { display: block; background: var(--accent-soft); height: 14px; border-radius: 0 4px 4px 0; }
.month-row .fill { display: block; background: var(--accent); height: 100%; border-radius: 0 4px 4px 0; }
.month-row .value { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 13.5px; }
.month-row.partial .fill { background: repeating-linear-gradient(135deg, var(--accent) 0 5px, transparent 5px 10px), var(--accent-soft); }

.delta { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.delta.up { color: var(--up); }
.delta.down { color: var(--down); }

footer { font-size: 12.5px; color: var(--ink-muted); border-top: 1px solid var(--rule); padding-top: 16px; }
footer p { margin: 0 0 6px; max-width: 72ch; }

@media (max-width: 620px) {
  body { padding: 24px 14px 48px; }
  h1 { font-size: 21px; }
  .month-row { grid-template-columns: 62px 1fr auto; gap: 10px; }
}
@media print {
  body { background: #fff; padding: 0; }
  .scroller { border-color: #ccc; }
}
"""


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _int(value: float) -> str:
    """Целое с неразрывными пробелами: в узкой ячейке число не переносится."""
    return f"{value:,.0f}".replace(",", " ")


def _month_label(month: str) -> str:
    """``2026-08`` → ``авг 26``: в шапке таблицы месяцев много, нужно коротко."""
    names = (
        "янв", "фев", "мар", "апр", "май", "июн",
        "июл", "авг", "сен", "окт", "ноя", "дек",
    )
    year, _, number = month.partition("-")
    try:
        return f"{names[int(number) - 1]} {year[2:]}"
    except (ValueError, IndexError):
        return month


def _meter(share: float) -> str:
    """Доля категории: полоска плюс число. Одна шкала, один цвет — это величина."""
    width = max(2.0, min(100.0, share))
    return (
        '<div class="meter">'
        f'<span class="meter-value">{share:.1f}%</span>'
        '<span class="meter-track">'
        f'<span class="meter-fill" style="width:{width:.1f}%"></span>'
        "</span>"
        "</div>"
    )


def _pivot(summary, trends, currency: str) -> str:
    """Главная таблица: строки — категории, столбцы — месяцы."""
    months = [s.month for s in summary]
    grand = sum(t.total for t in trends)

    head = "".join(f"<th>{_esc(_month_label(m))}</th>" for m in months)
    rows: list[str] = []
    for trend in trends:
        cells = []
        for month in months:
            value = trend.by_month.get(month, 0.0)
            css = "num" if value else "num zero"
            text = _int(value) if value else "—"
            cells.append(f'<td class="{css}">{text}</td>')
        share = trend.total / grand * 100 if grand else 0.0
        rows.append(
            f'<tr><td class="rowhead">{_esc(trend.category)}</td>'
            + "".join(cells)
            + f'<td class="num"><strong>{_int(trend.total)}</strong></td>'
            + f'<td class="num">{_int(trend.average)}</td>'
            + f"<td>{_meter(share)}</td></tr>"
        )

    totals = "".join(f'<td class="num">{_int(s.expense)}</td>' for s in summary)
    average = grand / len(months) if months else 0.0
    return (
        '<div class="scroller"><table>'
        f'<thead><tr><th class="rowhead">Категория</th>{head}'
        "<th>Итого</th><th>Ср./мес</th><th>Доля</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        f'<tfoot><tr><td class="rowhead">Всего</td>{totals}'
        f'<td class="num">{_int(grand)}</td><td class="num">{_int(average)}</td>'
        f"<td></td></tr></tfoot>"
        "</table></div>"
        f'<p class="hint">Суммы в {_esc(_unit(currency))}, округлены до целых. '
        "Прочерк — в этом месяце трат по категории не было.</p>"
    )


def _unit(currency: str) -> str:
    from .report import _SYMBOLS

    return _SYMBOLS.get(currency.upper(), currency.upper())


def _months_block(summary, currency: str, partial_month: str | None) -> str:
    """Расход по месяцам: одна величина, поэтому одна шкала и без легенды."""
    peak = max((s.expense for s in summary), default=0.0)
    rows = []
    for stats in summary:
        width = (stats.expense / peak * 100) if peak else 0.0
        partial = " partial" if stats.month == partial_month else ""
        note = " · неполный месяц" if partial else ""
        rows.append(
            f'<div class="month-row{partial}">'
            f'<span class="label">{_esc(_month_label(stats.month))}</span>'
            f'<span class="track"><span class="fill" style="width:{width:.1f}%"></span></span>'
            f'<span class="value">{_esc(money(stats.expense, currency))}</span>'
            "</div>"
            + (f'<p class="hint">{_esc(_month_label(stats.month))}{note}</p>' if partial else "")
        )
    return f'<div class="months">{"".join(rows)}</div>'


def _changes_block(summary, currency: str, limit: int) -> str:
    """Что изменилось за последний месяц. Знак и стрелка дублируют цвет."""
    if len(summary) < 2:
        return ""
    previous, current = summary[-2], summary[-1]
    rows = []
    for category, was, now in compare_months(previous, current)[:limit]:
        delta = now - was
        if abs(delta) < 1:
            continue
        #: Стрелка и знак несут то же, что цвет: на ч/б печати и при
        #: дальтонизме строка остаётся читаемой.
        direction = "up" if delta > 0 else "down"
        arrow = "▲" if delta > 0 else "▼"
        percent = f"{delta / was * 100:+.0f}%" if was else "новая"
        signed = f"{delta:+,.0f}".replace(",", " ")
        rows.append(
            f'<tr><td class="rowhead">{_esc(category)}</td>'
            f'<td class="num">{_int(was)}</td>'
            f'<td class="num">{_int(now)}</td>'
            f'<td class="delta {direction}">{arrow} {_esc(signed)}</td>'
            f'<td class="delta {direction}">{_esc(percent)}</td></tr>'
        )
    if not rows:
        return ""
    return (
        f'<div class="scroller"><table><thead><tr><th class="rowhead">Категория</th>'
        f"<th>{_esc(_month_label(previous.month))}</th>"
        f"<th>{_esc(_month_label(current.month))}</th>"
        "<th>Разница</th><th>%</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _recurring_block(items) -> str:
    if not items:
        return ""
    rows = "".join(
        f'<tr><td class="rowhead">{_esc(item.merchant)}</td>'
        f'<td class="num">{_int(item.typical_amount)}</td>'
        f'<td class="num">{item.months_count}</td>'
        f'<td class="num">{_int(item.yearly_estimate)}</td>'
        f'<td class="rowhead">{_esc(item.category)}</td></tr>'
        for item in items
    )
    total = sum(i.yearly_estimate for i in items)
    return (
        '<div class="scroller"><table><thead><tr><th class="rowhead">Списание</th>'
        "<th>В месяц</th><th>Месяцев</th><th>За год</th>"
        '<th class="rowhead">Категория</th></tr></thead>'
        f"<tbody>{rows}</tbody>"
        f'<tfoot><tr><td class="rowhead">Итого за год</td><td></td><td></td>'
        f'<td class="num">{_int(total)}</td><td></td></tr></tfoot>'
        "</table></div>"
    )


def _top_block(items) -> str:
    rows = "".join(
        f'<tr><td class="rowhead">{_esc(tx.date.isoformat())}</td>'
        f'<td class="num">{_int(tx.report_amount)}</td>'
        f'<td class="rowhead">{_esc(tx.category)}</td>'
        f'<td class="rowhead">{_esc(tx.description)}</td></tr>'
        for tx in items
    )
    return (
        '<div class="scroller"><table><thead><tr><th class="rowhead">Дата</th>'
        '<th>Сумма</th><th class="rowhead">Категория</th>'
        '<th class="rowhead">Описание</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def render_html(
    transactions: Sequence[Transaction],
    *,
    currency: str = "ILS",
    top: int = 10,
    standalone: bool = True,
    title: str = "Расходы по месяцам",
    today: date | None = None,
) -> str:
    """Собирает отчёт-страницу.

    ``standalone=False`` отдаёт только ``<style>`` и содержимое страницы —
    так её можно вставить в чужой шаблон.
    """
    if not transactions:
        body = "<p class='hint'>Нет операций за выбранный период.</p>"
        return _wrap(body, standalone, title)

    summary = monthly_summary(transactions)
    trends = category_trends(summary)
    totals = overall_stats(summary)
    recurring = find_recurring(transactions)
    start, end = transactions[0].date, transactions[-1].date

    #: Последний месяц периода может быть неполным — это заметно портит
    #: «в среднем за месяц», поэтому о нём сказано прямо.
    today = today or date.today()
    partial = summary[-1].month if summary[-1].month == f"{today.year:04d}-{today.month:02d}" else None

    biggest = trends[0] if trends else None
    yearly = sum(i.yearly_estimate for i in recurring)
    unknown = sum(tx.report_amount for tx in transactions if tx.category_rule is None and tx.is_expense)

    tiles = [
        ("Всего за период", money(totals["expense_total"], currency), f"{int(totals['months'])} мес."),
        ("В среднем за месяц", money(totals["expense_avg"], currency),
         "последний месяц неполный" if partial else ""),
        ("Крупнейшая категория", biggest.category if biggest else "—",
         money(biggest.total, currency) if biggest else ""),
        ("Регулярные списания", money(yearly, currency) if recurring else "—",
         f"{len(recurring)} шт., оценка за год" if recurring else "не найдено"),
    ]
    tiles_html = "".join(
        f'<div class="tile"><dt>{_esc(label)}</dt><dd>{_esc(value)}</dd>'
        + (f'<span class="note">{_esc(note)}</span>' if note else "")
        + "</div>"
        for label, value, note in tiles
    )

    parts = [
        '<div class="sheet">',
        '<header class="masthead">',
        '<span class="eyebrow">Личные расходы</span>',
        f"<h1>{_esc(title)}</h1>",
        f'<span class="period">{_esc(start.isoformat())} — {_esc(end.isoformat())} · '
        f"{len(transactions)} операций · валюта отчёта {_esc(_unit(currency))}</span>",
        "</header>",
        f'<dl class="tiles">{tiles_html}</dl>',
        "<section><h2>Категории по месяцам</h2>",
        _pivot(summary, trends, currency),
        "</section>",
        "<section><h2>Расход по месяцам</h2>",
        _months_block(summary, currency, partial),
        "</section>",
    ]

    changes = _changes_block(summary, currency, top)
    if changes:
        parts += [
            f"<section><h2>Что изменилось: {_esc(_month_label(summary[-2].month))} → "
            f"{_esc(_month_label(summary[-1].month))}</h2>",
            changes,
            "</section>",
        ]

    if recurring:
        parts += [
            "<section><h2>Регулярные списания</h2>",
            '<p class="hint">Списания, которые повторяются из месяца в месяц и не '
            "скачут по сумме: подписки, аренда, страховки, связь.</p>",
            _recurring_block(recurring),
            "</section>",
        ]

    parts += [
        f"<section><h2>Крупнейшие траты (топ {top})</h2>",
        _top_block(top_expenses(transactions, top)),
        "</section>",
    ]

    footer = ["<footer>"]
    if unknown:
        footer.append(
            f"<p>Без категории осталось {_esc(money(unknown, currency))}. "
            "Посмотреть список: <code>expenses unknown</code> — и дописать правила "
            "в <code>expenses.toml</code>.</p>"
        )
    footer.append(
        "<p>Отчёт собран локально командой <code>expenses report --format html</code>. "
        "Страница самодостаточная: внешних запросов не делает, данные никуда не отправляет.</p>"
    )
    footer.append("</footer>")
    parts += footer
    parts.append("</div>")

    return _wrap("".join(parts), standalone, title)


def _wrap(body: str, standalone: bool, title: str) -> str:
    style = f"<style>{STYLE}</style>"
    if not standalone:
        return f"{style}\n{body}\n"
    return (
        "<!doctype html>\n"
        '<html lang="ru">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"{style}\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )
