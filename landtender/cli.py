"""Командная строка ``landtender``.

Ежедневный запуск:      landtender run
Проверка источников:    landtender check
Выгрузка накопленного:  landtender export --format csv --tier premium
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import load_config
from .models import TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN
from .pipeline import notify, open_storage, probe_sources, run_once, stored_lots
from .report import build_console_report, export_csv, export_json
from .sources import SOURCES_BY_NAME

log = logging.getLogger("landtender")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="landtender",
        description="Ежедневный трекер земельных тендеров Израиля: стоимость земли, "
        "количество единиц строений, фильтр по порогу в 1 млн долларов.",
    )
    parser.add_argument("--config", help="путь к TOML-конфигу (по умолчанию landtender.toml рядом)")
    parser.add_argument("--db", help="путь к файлу SQLite (перекрывает конфиг)")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    parser.add_argument("-q", "--quiet", action="store_true", help="только ошибки")

    sub = parser.add_subparsers(dest="command")

    run_cmd = sub.add_parser("run", help="ежедневный сбор и рассылка сводки")
    run_cmd.add_argument(
        "--sources", nargs="*", choices=sorted(SOURCES_BY_NAME), help="запустить только эти источники"
    )
    run_cmd.add_argument(
        "--threshold-usd", type=float, help="порог отсечки в долларах (по умолчанию 1 000 000)"
    )
    run_cmd.add_argument(
        "--full-refresh", action="store_true", help="перезабрать детали всех тендеров, игнорируя кеш"
    )
    run_cmd.add_argument(
        "--dry-run", action="store_true", help="не отправлять в Telegram, показать сводку в консоли"
    )
    run_cmd.add_argument("--no-notify", action="store_true", help="только собрать данные, без рассылки")
    run_cmd.add_argument("--export-csv", help="дополнительно сохранить новые лоты в CSV")
    run_cmd.add_argument("--export-json", help="дополнительно сохранить новые лоты в JSON")

    check_cmd = sub.add_parser("check", help="проверить доступность источников")
    check_cmd.add_argument("--sources", nargs="*", choices=sorted(SOURCES_BY_NAME))

    export_cmd = sub.add_parser("export", help="выгрузить накопленные лоты из базы")
    export_cmd.add_argument("--format", choices=("csv", "json"), default="csv")
    export_cmd.add_argument("--out", help="файл назначения")
    export_cmd.add_argument(
        "--tier", choices=(TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN), help="только одна группа"
    )
    export_cmd.add_argument("--days", type=int, help="только лоты, впервые увиденные за N дней")

    sub.add_parser("stats", help="показать состояние базы и последний запуск")

    return parser


def setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.threshold_usd:
        config.data.setdefault("general", {})["threshold_usd"] = args.threshold_usd

    with open_storage(config, args.db) as storage:
        result = run_once(
            config,
            storage,
            only_sources=args.sources,
            full_refresh=args.full_refresh,
        )
        print(build_console_report(result, config.threshold_usd))

        if args.export_csv:
            path = export_csv(result.new_lots, Path(args.export_csv))
            print(f"CSV: {path}")
        if args.export_json:
            path = export_json(result.new_lots, Path(args.export_json))
            print(f"JSON: {path}")

        if not args.no_notify:
            sent = notify(config, storage, result, dry_run=args.dry_run)
            if sent:
                print(f"Telegram: отправлено сообщений — {sent}")

    if not result.sources:
        log.error("Не выбран ни один источник — проверьте [sources] в конфиге")
        return 2
    # Код 1, если упали все источники: удобно для алертов cron/CI.
    return 0 if result.ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    reports = probe_sources(config, args.sources)
    failed = 0
    for report in reports:
        if report.ok:
            print(f"[ ok ] {report.name:<16} {report.duration_sec:>5.1f}с  {report.note or ''}")
        else:
            failed += 1
            print(f"[FAIL] {report.name:<16} {report.duration_sec:>5.1f}с  {report.error}")
    return 1 if failed else 0


def cmd_export(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    since = (date.today() - timedelta(days=args.days)).isoformat() if args.days else None

    with open_storage(config, args.db) as storage:
        lots = stored_lots(storage, tier=args.tier, since=since)

    default_name = f"landtender-{date.today().isoformat()}.{args.format}"
    path = Path(args.out) if args.out else config.db_path.parent / default_name
    writer = export_csv if args.format == "csv" else export_json
    writer(lots, path)
    print(f"Выгружено лотов: {len(lots)} → {path}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with open_storage(config, args.db) as storage:
        total = storage.count_lots()
        premium = sum(1 for _ in storage.iter_lots(tier=TIER_PREMIUM))
        standard = sum(1 for _ in storage.iter_lots(tier=TIER_STANDARD))
        last = storage.last_run()

    print(f"База: {config.db_path}")
    print(f"Всего лотов: {total} (дороже порога: {premium}, дешевле: {standard})")
    if last is None:
        print("Запусков ещё не было")
    else:
        print(f"Последний запуск: {last['started_at']} → {last['finished_at']}")
        print(last["stats_json"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose, args.quiet)

    command = args.command or "run"
    if command == "run":
        # ``landtender`` без подкоманды = ``landtender run`` с умолчаниями
        for name, default in (
            ("sources", None), ("threshold_usd", None), ("full_refresh", False),
            ("dry_run", False), ("no_notify", False), ("export_csv", None), ("export_json", None),
        ):
            if not hasattr(args, name):
                setattr(args, name, default)
        return cmd_run(args)
    if command == "check":
        return cmd_check(args)
    if command == "export":
        return cmd_export(args)
    if command == "stats":
        return cmd_stats(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
