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
from .pipeline import (
    backfill_land_use,
    farmland_lots,
    notify,
    open_storage,
    probe_sources,
    run_once,
    stored_lots,
)
from .report import (
    build_console_report,
    build_farmland_digest,
    build_top_digest,
    export_csv,
    export_json,
    preview_messages,
)
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

    farm_cmd = sub.add_parser(
        "farmland", help="вся сельхозземля, доступная на тендерах (срез базы)"
    )
    farm_cmd.add_argument(
        "--all", action="store_true", help="включая тендеры с истёкшим сроком подачи"
    )
    farm_cmd.add_argument("--send", action="store_true", help="отправить сводку в Telegram")
    farm_cmd.add_argument("--out", help="сохранить список в CSV")
    farm_cmd.add_argument("--limit", type=int, default=60, help="сколько лотов показать")

    harvest_cmd = sub.add_parser(
        "harvest", help="собрать архив закрытых торгов — база сравнимых сделок"
    )
    harvest_cmd.add_argument(
        "--budget", type=int, default=3000,
        help="потолок по числу тендеров за заход "
             "(сбор накопительный: следующий запуск добирает остальные)",
    )
    harvest_cmd.add_argument(
        "--minutes", type=float, default=165.0,
        help="сколько минут ходить за деталями; по истечении срока обход "
             "прекращается, и собранное сохраняется (0 — без ограничения)",
    )

    top_cmd = sub.add_parser(
        "top", help="лучшие предложения из базы по общему баллу"
    )
    top_cmd.add_argument("--limit", type=int, default=10, help="сколько мест показать")
    top_cmd.add_argument("--send", action="store_true", help="отправить в Telegram")
    top_cmd.add_argument("--all", action="store_true", help="включая закрытые тендеры")
    top_cmd.add_argument("--out", help="выгрузить список в CSV")

    sub.add_parser("stats", help="показать состояние базы и последний запуск")

    inspect_cmd = sub.add_parser(
        "inspect", help="разведка: что портал рм\"י реально отдаёт по тендеру"
    )
    inspect_cmd.add_argument("tender_ids", nargs="*", help="номера тендеров; по умолчанию первые из поиска")
    inspect_cmd.add_argument("--limit", type=int, default=3, help="сколько тендеров смотреть")
    inspect_cmd.add_argument("--all", action="store_true", help="искать среди всех, а не только активных")
    inspect_cmd.add_argument("--ckan", help="вместо портала рм\"י разведать наборы data.gov.il по запросу")
    inspect_cmd.add_argument(
        "--service",
        choices=("iplan", "govmap", "nadlan", "macro"),
        help="разведать карт-сервис: реестр планов, участки govmap или рынок nadlan",
    )
    inspect_cmd.add_argument("--gush", help="гуш для примера записи (iplan, govmap)")
    inspect_cmd.add_argument("--helka", help="хелка для примера участка (govmap)")
    inspect_cmd.add_argument("--settlement-code", default="5000", help="код НП для nadlan")

    setup_cmd = sub.add_parser(
        "setup", help="мастер настройки Telegram: токен, канал, .env и пробная сводка"
    )
    setup_cmd.add_argument("--env-file", help="куда записать .env (по умолчанию в текущий каталог)")
    setup_cmd.add_argument("--no-demo", action="store_true", help="не слать демонстрационную сводку")

    demo_cmd = sub.add_parser(
        "demo", help="отправить в канал демонстрационную сводку на вымышленных данных"
    )
    demo_cmd.add_argument("--dry-run", action="store_true", help="показать в консоли, не отправляя")

    tg_cmd = sub.add_parser(
        "telegram-test", help="проверить подключение к Telegram и отправить пробное сообщение"
    )
    tg_cmd.add_argument(
        "--discover", action="store_true",
        help="показать chat_id всех чатов, где боту приходили сообщения",
    )
    tg_cmd.add_argument("--no-send", action="store_true", help="только проверки, без пробного сообщения")

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
        print(build_console_report(
            result,
            config.threshold_usd,
            split_by_threshold=bool(config.get("general", "split_by_threshold", True)),
        ))

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


def cmd_inspect(args: argparse.Namespace) -> int:
    from .inspect import inspect_tenders
    from .pipeline import build_http

    config = load_config(args.config)
    http = build_http(config)

    if args.service:
        from .mapping import (
            inspect_govmap,
            inspect_iplan,
            inspect_macro,
            inspect_nadlan,
        )

        if args.service == "iplan":
            return inspect_iplan(http, gush=args.gush)
        if args.service == "govmap":
            return inspect_govmap(http, gush=args.gush, helka=args.helka)
        if args.service == "macro":
            return inspect_macro(http)
        return inspect_nadlan(http, settlement_code=args.settlement_code)

    if args.ckan:
        from .inspect import inspect_ckan

        return inspect_ckan(http, args.ckan, limit=args.limit)

    return inspect_tenders(
        http,
        limit=args.limit,
        tender_ids=args.tender_ids or None,
        active_only=not args.all,
    )


def cmd_setup(args: argparse.Namespace) -> int:
    from .config import ENV_FILE_NAME
    from .setup_wizard import run_wizard

    config = load_config(args.config)
    env_path = Path(args.env_file) if args.env_file else Path.cwd() / ENV_FILE_NAME
    return run_wizard(
        env_path=env_path,
        send_demo=not args.no_demo,
        threshold_usd=config.threshold_usd,
    )


def cmd_demo(args: argparse.Namespace) -> int:
    """Показывает, как выглядит сводка, на вымышленных данных."""
    from .demo import demo_blocks
    from .notify import TelegramError, TelegramNotifier

    config = load_config(args.config)
    blocks = demo_blocks(
        config.threshold_usd,
        split_by_threshold=bool(config.get("general", "split_by_threshold", True)),
    )

    if args.dry_run:
        print(preview_messages(blocks))
        return 0

    token = config.get("telegram", "bot_token")
    chat_id = config.get("telegram", "chat_id")
    if not token or not chat_id:
        print("✗ Нет токена или канала. Запустите: landtender setup")
        return 2

    try:
        sent = TelegramNotifier(bot_token=str(token), chat_id=str(chat_id)).send_blocks(blocks)
    except TelegramError as exc:
        print(f"✗ Отправка не прошла: {exc}")
        return 1
    print(f"✓ Демонстрационная сводка отправлена ({sent} сообщ.) — проверьте канал")
    return 0


def cmd_telegram_test(args: argparse.Namespace) -> int:
    """Пошагово проверяет цепочку токен → канал → отправка."""
    from .notify import TelegramError, TelegramNotifier

    config = load_config(args.config)
    token = config.get("telegram", "bot_token")
    chat_id = config.get("telegram", "chat_id")

    if not token:
        print("✗ Токен не найден. Задайте TELEGRAM_BOT_TOKEN в окружении или в файле .env")
        return 2

    if args.discover:
        # Для поиска chat_id канал ещё не нужен — подставляем заглушку.
        notifier = TelegramNotifier(bot_token=token, chat_id=chat_id or "0")
        try:
            chats = notifier.discover_chats()
        except TelegramError as exc:
            print(f"✗ getUpdates: {exc}")
            return 1
        if not chats:
            print(
                "Чатов не найдено. Добавьте бота администратором канала, напишите "
                "туда любое сообщение и повторите команду."
            )
            return 1
        print("Найденные чаты (скопируйте нужный chat_id в TELEGRAM_CHAT_ID):")
        for chat in chats:
            print(f"  {chat['chat_id']:<16} {chat['type']:<10} {chat['title'] or ''}")
        return 0

    if not chat_id:
        print("✗ Не задан TELEGRAM_CHAT_ID. Найти его поможет: landtender telegram-test --discover")
        return 2

    notifier = TelegramNotifier(bot_token=token, chat_id=chat_id)

    try:
        me = notifier.get_me()
        print(f"✓ Токен принят: @{me.get('username')} ({me.get('first_name')})")
    except TelegramError as exc:
        print(f"✗ Токен отклонён: {exc}")
        return 1

    try:
        chat = notifier.get_chat()
        print(f"✓ Канал доступен: {chat.get('title') or chat.get('username')} (id {chat.get('id')})")
    except TelegramError as exc:
        print(f"✗ Канал недоступен: {exc}")
        print("  Проверьте, что бот добавлен в канал администратором и chat_id указан верно.")
        return 1

    if args.no_send:
        return 0

    try:
        notifier.send_blocks(["<b>landtender</b>\nПробное сообщение — канал подключён."])
        print("✓ Пробное сообщение отправлено")
    except TelegramError as exc:
        print(f"✗ Отправка не прошла: {exc}")
        return 1
    return 0


def cmd_harvest(args: argparse.Namespace) -> int:
    """Забирает архив закрытых торгов рм"и — цены состоявшихся сделок.

    Отдельная команда, а не часть ежедневного запуска: архив большой, идёт
    долго и нужен редко. Сводка при этом не рассылается — накопление базы не
    повод писать в канал.
    """
    config = load_config(args.config)
    # Архив: не только действующие тендеры, и сроки подачи давно прошли.
    config.data.setdefault("sources", {}).setdefault("rmi_michrazim", {}).update(
        {
            "active_only": False,
            "details_budget": args.budget,
            "details_time_budget_sec": args.minutes * 60,
        }
    )
    config.data.setdefault("general", {})["hide_expired"] = False
    config.data.setdefault("valuation", {})["estimate"] = False

    with open_storage(config, args.db) as storage:
        before = storage.count_lots()
        result = run_once(config, storage, only_sources=["rmi_michrazim"])
        after = storage.count_lots()

        from .valuation import age_histogram, collect_comparables, explain_rejections

        rows = stored_lots(storage)
        deals = collect_comparables(rows)
        breakdown = explain_rejections(rows)
        years = age_histogram(rows)

    print(f"Просмотрено записей: {result.total_seen}")
    print(f"Лотов в базе: {before} → {after}")
    print(f"Сделок с ценой, годных для сравнения: {len(deals)}")
    print("\nПочему остальные не годятся:")
    for reason, count in breakdown.items():
        print(f"  {reason:<28} {count}")
    if years:
        print("\nСостоявшиеся сделки по годам:")
        # Без обрезки: смысл гистограммы в том, чтобы видеть весь ряд целиком,
        # а лет в архиве больше, чем помещалось в прежний лимит.
        for year, count in years.items():
            print(f"  {year}  {'#' * min(count // 10 + 1, 40)} {count}")
    if not deals:
        print("\nЦен сделок пока нет — повторите команду, лимит деталей расходуется постепенно.")
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    """Лучшие предложения из всей базы, а не только за сегодня.

    Оценка и балл пересчитываются на текущих сравнимых сделках: сохранённые
    в базе числа посчитаны в день загрузки лота, когда сравнимых было меньше,
    и рейтинг по ним сравнивал бы лоты по разным меркам.
    """
    from .notify import TelegramError, TelegramNotifier
    from .pipeline import build_http, top_lots

    config = load_config(args.config)
    # Рейтинг строится на оценке, поэтому она нужна независимо от конфига:
    # без неё у показателя «цена против оценки» нет числа, и топ вырождается.
    config.data.setdefault("valuation", {})["estimate"] = True

    # Сеть нужна только за индексом ЦСБ, которым старые сделки приводятся к
    # сегодняшним деньгам; сами лоты берутся из базы.
    http = build_http(config)
    with open_storage(config, args.db) as storage:
        backfill_land_use(storage)
        lots = top_lots(config, http, storage, limit=args.limit, only_active=not args.all)

    blocks = build_top_digest(lots, limit=args.limit, only_active=not args.all)

    if args.out:
        export_csv(lots, Path(args.out))
        print(f"CSV: {args.out}")

    if not args.send:
        print(preview_messages(blocks))
        return 0

    token = config.get("telegram", "bot_token")
    chat_id = config.get("telegram", "chat_id")
    if not token or not chat_id:
        print("✗ Нет токена или канала. Запустите: landtender setup")
        return 2
    try:
        sent = TelegramNotifier(bot_token=str(token), chat_id=str(chat_id)).send_blocks(blocks)
    except TelegramError as exc:
        print(f"✗ Отправка не прошла: {exc}")
        return 1
    print(f"✓ Топ отправлен ({sent} сообщ.): мест {len(lots)}")
    return 0


def cmd_farmland(args: argparse.Namespace) -> int:
    """Показывает всю сельхозземлю из базы, а не только новую за сегодня."""
    from .notify import TelegramError, TelegramNotifier

    config = load_config(args.config)
    with open_storage(config, args.db) as storage:
        # База могла накопиться до появления разбора назначения — доразбираем
        backfill_land_use(storage)
        lots = farmland_lots(storage, only_active=not args.all)

    blocks = build_farmland_digest(lots, max_lots=args.limit, only_active=not args.all)

    if args.out:
        export_csv(lots, Path(args.out))
        print(f"CSV: {args.out}")

    if not args.send:
        print(preview_messages(blocks))
        return 0

    token = config.get("telegram", "bot_token")
    chat_id = config.get("telegram", "chat_id")
    if not token or not chat_id:
        print("✗ Нет токена или канала. Запустите: landtender setup")
        return 2
    try:
        sent = TelegramNotifier(bot_token=str(token), chat_id=str(chat_id)).send_blocks(blocks)
    except TelegramError as exc:
        print(f"✗ Отправка не прошла: {exc}")
        return 1
    print(f"✓ Сводка по сельхозземле отправлена ({sent} сообщ.): лотов {len(lots)}")
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
    if command == "farmland":
        return cmd_farmland(args)
    if command == "top":
        return cmd_top(args)
    if command == "harvest":
        return cmd_harvest(args)
    if command == "export":
        return cmd_export(args)
    if command == "stats":
        return cmd_stats(args)
    if command == "telegram-test":
        return cmd_telegram_test(args)
    if command == "setup":
        return cmd_setup(args)
    if command == "demo":
        return cmd_demo(args)
    if command == "inspect":
        return cmd_inspect(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
