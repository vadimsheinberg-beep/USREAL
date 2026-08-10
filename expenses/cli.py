"""Командная строка ``expenses``.

Типичный сценарий::

    expenses demo                       # посмотреть, как выглядит отчёт
    expenses import выписка.csv         # загрузить выгрузку из банка
    expenses probe --source bybit       # проверить ключи и доступ к API
    expenses fetch --months 6           # забрать операции с Bybit
    expenses report --months 6          # помесячная сводка по категориям
    expenses unknown                    # что осталось без категории
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

from . import fx
from .analyze import filter_period, find_recurring, monthly_summary
from .categories import Categorizer, rules_from_config
from .config import load_config
from .demo import generate as generate_demo
from .html_report import render_html
from .models import Transaction
from .notify import TelegramError, TelegramNotifier, build_summary, month_name
from .report import (
    money,
    render_csv,
    render_json,
    render_markdown,
    render_recurring,
    render_text,
)
from .sources.base import FieldMap, SourceError
from .sources.bybit import BybitClient, BybitConfig, BybitError
from .sources.files import load_file
from .sources.rest import RestClient, RestConfig, RestError
from .storage import Store

log = logging.getLogger("expenses")

#: Сетевые источники: имя в конфиге → как построить клиент и его ошибка.
API_SOURCES = {
    "bybit": (BybitClient, BybitConfig, BybitError),
    "rest": (RestClient, RestConfig, RestError),
}
#: Ошибки источников ловятся одним except — сообщение у них уже понятное.
API_ERRORS = (BybitError, RestError)

#: Куда класть html-отчёт, если файл не указан явно.
DEFAULT_HTML_OUT = "expenses-report.html"


def setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expenses",
        description="Анализ личных расходов: импорт транзакций, категоризация, "
        "помесячные отчёты и поиск регулярных списаний.",
    )
    parser.add_argument("--config", help="путь к TOML-конфигу (по умолчанию expenses.toml рядом)")
    parser.add_argument("--data", help="путь к файлу с операциями (перекрывает конфиг)")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    parser.add_argument("-q", "--quiet", action="store_true", help="только ошибки")

    sub = parser.add_subparsers(dest="command")

    import_cmd = sub.add_parser("import", help="загрузить выписку из CSV/JSON")
    import_cmd.add_argument("path", help="файл выписки")
    import_cmd.add_argument("--source", help="как пометить источник (по умолчанию по расширению)")
    import_cmd.add_argument("--dry-run", action="store_true", help="показать итог, но не сохранять")

    fetch_cmd = sub.add_parser("fetch", help="забрать операции из API (Bybit или свой REST)")
    fetch_cmd.add_argument(
        "--source", choices=sorted(API_SOURCES), default="bybit", help="откуда забирать"
    )
    _add_period_args(fetch_cmd)
    fetch_cmd.add_argument("--dry-run", action="store_true", help="не сохранять, только показать")

    probe_cmd = sub.add_parser("probe", help="проверить доступ к API и показать его поля")
    probe_cmd.add_argument("--source", choices=sorted(API_SOURCES), default="bybit")
    _add_period_args(probe_cmd)

    report_cmd = sub.add_parser("report", help="помесячный отчёт по категориям")
    _add_period_args(report_cmd)
    report_cmd.add_argument(
        "--format",
        choices=("text", "html", "md", "csv", "json"),
        default="text",
        help="формат вывода; html — страница со сводной таблицей",
    )
    report_cmd.add_argument("--out", help="записать в файл вместо вывода в консоль")
    report_cmd.add_argument("--top", type=int, help="сколько строк показывать в топах")
    report_cmd.add_argument("--category", help="показать только одну категорию")

    recurring_cmd = sub.add_parser("recurring", help="регулярные списания и подписки")
    _add_period_args(recurring_cmd)

    unknown_cmd = sub.add_parser("unknown", help="операции, не попавшие ни в одну категорию")
    _add_period_args(unknown_cmd)
    unknown_cmd.add_argument("--limit", type=int, default=40, help="сколько строк показать")

    test_cmd = sub.add_parser("test-rule", help="проверить, в какую категорию попадёт описание")
    test_cmd.add_argument("text", nargs="+", help="текст операции")

    demo_cmd = sub.add_parser("demo", help="сгенерировать демо-выписку и показать отчёт")
    demo_cmd.add_argument("--months", type=int, default=6, help="за сколько месяцев")
    demo_cmd.add_argument("--save", action="store_true", help="сохранить демо в хранилище")

    monthly_cmd = sub.add_parser(
        "monthly", help="собрать данные и прислать месячный отчёт в Telegram"
    )
    monthly_cmd.add_argument("--months", type=int, help="глубина отчёта, месяцев")
    monthly_cmd.add_argument(
        "--dry-run", action="store_true", help="показать сводку, но не отправлять"
    )
    monthly_cmd.add_argument(
        "--no-fetch", action="store_true", help="не ходить в API, взять что уже в хранилище"
    )

    sub.add_parser("telegram-test", help="проверить доступ к Telegram")

    sub.add_parser("stats", help="что лежит в хранилище")

    return parser


def _add_period_args(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument("--months", type=int, help="за сколько последних месяцев")
    cmd.add_argument("--since", help="начало периода, YYYY-MM-DD")
    cmd.add_argument("--until", help="конец периода, YYYY-MM-DD")


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"Плохая дата {value!r}, нужен формат YYYY-MM-DD") from exc


def _open_store(args: argparse.Namespace, config) -> Store:
    path = Path(args.data) if getattr(args, "data", None) else config.data_path
    return Store(path)


def _categorizer(config) -> Categorizer:
    section = config.section("categories")
    return Categorizer(
        rules_from_config(config.rules),
        use_defaults=bool(section.get("use_defaults", True)),
        trust_source_category=bool(section.get("trust_source_category", False)),
    )


def _prepare(
    transactions: Sequence[Transaction], config, args: argparse.Namespace
) -> list[Transaction]:
    """Категоризация, пересчёт валют и отбор периода — общий шаг всех отчётов."""
    categorizer = _categorizer(config)
    items = categorizer.categorize_all(transactions)
    items = fx.convert(items, config.currency, config.fx_rates)
    months = getattr(args, "months", None) or int(config.get("general", "months", 6))
    return filter_period(
        items,
        since=_parse_day(getattr(args, "since", None)),
        until=_parse_day(getattr(args, "until", None)),
        months=months if not getattr(args, "since", None) else None,
    )


def cmd_import(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    mapping_raw = config.section("import").get("fields") if "import" in config.data else None
    mapping = FieldMap.from_config(mapping_raw) if mapping_raw else None

    try:
        transactions = load_file(args.path, mapping, source=args.source)
    except SourceError as exc:
        print(f"Не смог прочитать файл: {exc}", file=sys.stderr)
        return 2

    if not transactions:
        print("В файле не нашлось операций.")
        return 0

    categorizer = _categorizer(config)
    categorizer.categorize_all(transactions)
    unknown = len(categorizer.uncategorized(transactions))

    print(f"Прочитано операций: {len(transactions)}")
    print(f"Период: {transactions[0].date} — {transactions[-1].date}")
    print(f"Без категории: {unknown}")

    if args.dry_run:
        print("(--dry-run: в хранилище ничего не записано)")
        return 0

    store = _open_store(args, config)
    added, duplicates = store.add(transactions)
    store.save()
    print(f"Добавлено новых: {added}, пропущено дубликатов: {duplicates}")
    print(f"Хранилище: {store.path}")
    return 0


def _since_from_months(months: int | None) -> date | None:
    """Начало периода для сетевых источников — первое число N-го месяца назад."""
    if not months:
        return None
    today = date.today()
    year, month = today.year, today.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _build_client(name: str, config):
    client_cls, config_cls, error_cls = API_SOURCES[name]
    return client_cls(config_cls.from_dict(config.source_config(name))), error_cls


def cmd_fetch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    name = args.source
    if not config.source_enabled(name):
        print(
            f"Источник {name} выключен. Включите [sources.{name}] enabled = true в expenses.toml.",
            file=sys.stderr,
        )
        return 2

    since = _parse_day(args.since) or _since_from_months(args.months)
    until = _parse_day(args.until)

    try:
        client, error_cls = _build_client(name, config)
        transactions = client.fetch(since, until)
    except API_ERRORS as exc:
        print(f"{name}: {exc}", file=sys.stderr)
        return 2

    if not transactions:
        print(f"{name} не вернул операций за период.")
        return 0

    print(f"Получено операций: {len(transactions)}")
    if args.dry_run:
        print("(--dry-run: в хранилище ничего не записано)")
        return 0

    store = _open_store(args, config)
    added, duplicates = store.add(transactions)
    store.save()
    print(f"Добавлено новых: {added}, пропущено дубликатов: {duplicates}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    try:
        client, _ = _build_client(args.source, config)
        print(client.probe(_parse_day(args.since), _parse_day(args.until)))
    except API_ERRORS as exc:
        print(f"{args.source}: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = _open_store(args, config)
    stored = store.load()
    if not stored:
        print(
            f"Хранилище пустое ({store.path}). Загрузите выписку: expenses import файл.csv "
            "или посмотрите демо: expenses demo",
        )
        return 0

    items = _prepare(stored, config, args)
    if args.category:
        wanted = args.category.strip().lower()
        items = [tx for tx in items if tx.category.lower() == wanted]
        if not items:
            print(f"В категории «{args.category}» операций за период нет.")
            return 0

    top = args.top or int(config.get("general", "top", 10))
    currency = config.currency
    if args.format == "html":
        text = render_html(items, currency=currency, top=top)
    elif args.format == "md":
        text = render_markdown(items, currency=currency, top=top)
    elif args.format == "csv":
        text = render_csv(items)
    elif args.format == "json":
        text = render_json(items, currency=currency)
    else:
        text = render_text(items, currency=currency, top=top)

    #: Вываливать разметку в терминал бессмысленно, поэтому у html есть
    #: имя файла по умолчанию — его сразу можно открыть.
    out = args.out or (DEFAULT_HTML_OUT if args.format == "html" else None)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"Отчёт записан: {Path(out).resolve()}")
    else:
        print(text)
    return 0


def cmd_recurring(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = _open_store(args, config)
    stored = store.load()
    if not stored:
        print("Хранилище пустое.")
        return 0

    items = _prepare(stored, config, args)
    section = config.section("categories")
    found = find_recurring(
        items,
        min_months=int(section.get("recurring_min_months", 3)),
        tolerance=float(section.get("recurring_tolerance", 0.25)),
    )
    print(render_recurring(found, config.currency))
    return 0


def cmd_unknown(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = _open_store(args, config)
    stored = store.load()
    if not stored:
        print("Хранилище пустое.")
        return 0

    items = _prepare(stored, config, args)
    unknown = [tx for tx in items if tx.category_rule is None and tx.is_expense]
    if not unknown:
        print("Все операции разнесены по категориям.")
        return 0

    total = sum(tx.report_amount for tx in unknown)
    print(f"Без категории: {len(unknown)} оп. на {money(total, config.currency)}")
    print("-" * 60)
    #: Группируем по мерчанту: одно правило обычно закрывает целую пачку.
    grouped: dict[str, tuple[int, float]] = {}
    for tx in unknown:
        count, amount = grouped.get(tx.merchant or tx.description, (0, 0.0))
        grouped[tx.merchant or tx.description] = (count + 1, amount + tx.report_amount)
    rows = sorted(grouped.items(), key=lambda kv: -kv[1][1])[: args.limit]
    for merchant, (count, amount) in rows:
        print(f"  {money(amount, config.currency):>14}  ×{count:<4} {merchant[:40]}")
    print()
    print("Добавьте в expenses.toml, например:")
    if rows:
        print("  [[rules]]")
        print('  category = "Своя категория"')
        print(f'  patterns = ["{rows[0][0][:24]}"]')
    return 0


def cmd_test_rule(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    categorizer = _categorizer(config)
    text = " ".join(args.text)
    tx = Transaction(date=date.today(), amount=100.0, description=text)
    categorizer.categorize(tx)
    print(f"Описание:  {text}")
    print(f"Мерчант:   {tx.merchant}")
    print(f"Категория: {tx.category}")
    print(f"Правило:   {tx.category_rule or 'не сработало ни одно'}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    transactions = generate_demo(months=args.months)
    categorizer = _categorizer(config)
    categorizer.categorize_all(transactions)
    transactions = fx.convert(transactions, config.currency, config.fx_rates)

    if args.save:
        store = _open_store(args, config)
        added, duplicates = store.add(transactions)
        store.save()
        print(f"Демо сохранено в {store.path}: новых {added}, дубликатов {duplicates}\n")

    print(render_text(transactions, currency=config.currency))
    return 0


def _notifier(config) -> TelegramNotifier:
    section = config.section("telegram")
    return TelegramNotifier(
        bot_token=_secret(section.get("bot_token", "env:TELEGRAM_BOT_TOKEN")),
        chat_id=_secret(section.get("chat_id", "env:TELEGRAM_CHAT_ID")),
    )


def _secret(value) -> str:
    """``env:NAME`` разворачивает в переменную окружения — токены не в конфиге."""
    text = str(value or "")
    if text.startswith("env:"):
        return os.environ.get(text[4:].strip(), "")
    return text


def _collect_inbox(config, store: Store, categorizer: Categorizer) -> int:
    """Забирает выписки, положенные в каталог inbox, и убирает их в архив.

    Банковские выгрузки автоматически не скачиваются, поэтому файл просто
    кладут на сервер; разобранные складываются в ``archive``, чтобы
    следующий запуск не читал их снова.
    """
    raw = config.get("general", "inbox_path")
    if not raw:
        return 0
    inbox = Path(str(raw)).expanduser()
    if not inbox.is_absolute() and config.path is not None:
        inbox = config.path.parent / inbox
    if not inbox.is_dir():
        return 0

    archive = inbox / "archive"
    added_total = 0
    for path in sorted(inbox.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".csv", ".tsv", ".json", ".jsonl"}:
            continue
        try:
            items = load_file(path)
        except SourceError as exc:
            log.warning("выписка %s не разобралась: %s", path.name, exc)
            continue
        categorizer.categorize_all(items)
        added, duplicates = store.add(items)
        added_total += added
        log.info("%s: новых %d, дубликатов %d", path.name, added, duplicates)
        archive.mkdir(parents=True, exist_ok=True)
        path.replace(archive / path.name)
    return added_total


def cmd_monthly(args: argparse.Namespace) -> int:
    """Один запуск по расписанию: собрать, посчитать, отправить."""
    config = load_config(args.config)
    store = _open_store(args, config)
    store.load()
    categorizer = _categorizer(config)

    if not args.no_fetch:
        for name in sorted(API_SOURCES):
            if not config.source_enabled(name):
                continue
            try:
                client, _ = _build_client(name, config)
                fetched = client.fetch()
            except API_ERRORS as exc:
                #: Отчёт важнее свежести: шлём по тому, что уже накоплено.
                log.error("%s: %s", name, exc)
                continue
            categorizer.categorize_all(fetched)
            added, duplicates = store.add(fetched)
            log.info("%s: новых %d, дубликатов %d", name, added, duplicates)

    _collect_inbox(config, store, categorizer)
    store.save()

    stored = store.transactions
    if not stored:
        print("Хранилище пустое — отправлять нечего.", file=sys.stderr)
        return 1

    items = _prepare(stored, config, args)
    if not items:
        print("За период нет операций.", file=sys.stderr)
        return 1

    currency = config.currency
    summary = build_summary(items, currency)

    if args.dry_run:
        print(summary)
        print("\n(--dry-run: в Telegram ничего не отправлено)")
        return 0

    report_dir = config.data_path.parent
    report_dir.mkdir(parents=True, exist_ok=True)
    last_month = monthly_summary(items)[-1].month
    report_path = report_dir / f"expenses-{last_month}.html"
    report_path.write_text(
        render_html(items, currency=currency, title=f"Расходы, {month_name(last_month)}"),
        encoding="utf-8",
    )

    try:
        notifier = _notifier(config)
        notifier.send_message(summary)
        notifier.send_document(report_path, caption="Полный отчёт: таблица по категориям")
    except TelegramError as exc:
        print(f"Telegram: {exc}", file=sys.stderr)
        return 2

    print(f"Отчёт за {month_name(last_month)} отправлен. Файл: {report_path}")
    return 0


def cmd_telegram_test(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    try:
        print("Telegram доступен:", _notifier(config).check())
    except TelegramError as exc:
        print(f"Telegram: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = _open_store(args, config)
    stored = store.load()
    print(f"Файл:     {store.path}")
    print(f"Операций: {len(stored)}")
    if not stored:
        return 0
    print(f"Период:   {stored[0].date} — {stored[-1].date}")
    print(f"Валюты:   " + ", ".join(f"{c} ×{n}" for c, n in fx.currencies(stored).items()))
    sources: dict[str, int] = {}
    for tx in stored:
        sources[tx.source or "?"] = sources.get(tx.source or "?", 0) + 1
    print("Источники: " + ", ".join(f"{s} ×{n}" for s, n in sources.items()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose, args.quiet)

    commands = {
        "import": cmd_import,
        "fetch": cmd_fetch,
        "probe": cmd_probe,
        "report": cmd_report,
        "recurring": cmd_recurring,
        "unknown": cmd_unknown,
        "test-rule": cmd_test_rule,
        "monthly": cmd_monthly,
        "telegram-test": cmd_telegram_test,
        "demo": cmd_demo,
        "stats": cmd_stats,
    }
    handler = commands.get(args.command or "")
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
