"""Ежедневный конвейер: собрать → оценить → сравнить со вчера → уведомить."""

from __future__ import annotations

import logging
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from .config import Config
from .http import HttpClient
from .models import Lot, RunResult, SourceReport
from .money import FxProvider, FxRate, enrich_lot, utcnow_iso
from .places import matches as place_matches
from .sources import SOURCES_BY_NAME, Source, SourceContext
from .storage import Storage

log = logging.getLogger(__name__)

#: Статусы рм"י, означающие, что подавать заявку уже нельзя.
CLOSED_STATUSES = {"סגור", "בוטל", "הסתיים"}


def build_http(config: Config) -> HttpClient:
    return HttpClient(
        user_agent=str(config.get("general", "user_agent", "landtender/0.1")),
        timeout=int(config.get("general", "request_timeout", 45)),
        rate_limit_delay=float(config.get("general", "rate_limit_delay", 1.0)),
    )


def select_sources(config: Config, only: Sequence[str] | None = None) -> list[str]:
    """Имена источников к запуску: из ``--sources`` либо из конфига."""
    if only:
        unknown = [name for name in only if name not in SOURCES_BY_NAME]
        if unknown:
            raise ValueError(
                f"Неизвестные источники: {', '.join(unknown)}. "
                f"Доступны: {', '.join(SOURCES_BY_NAME)}"
            )
        return list(only)
    return [name for name in SOURCES_BY_NAME if config.source_enabled(name)]


def build_source(name: str, config: Config, http: HttpClient, storage: Storage | None,
                 full_refresh: bool = False) -> Source:
    ctx = SourceContext(
        http=http,
        options=config.source_config(name),
        lookback_days=int(config.get("general", "lookback_days", 30)),
        cache=storage,
        full_refresh=full_refresh,
    )
    return SOURCES_BY_NAME[name](ctx)


def build_enricher(config: Config, http: HttpClient) -> Any:
    """Дополнение лотов кадастром и планами, если включено в конфиге.

    По умолчанию выключено: это два-три чужих запроса на лот, и включать их
    молча в ежедневный обход неправильно.
    """
    section = config.section("enrichment")
    if not section.get("enabled", False):
        return None

    from .invest import Enricher
    from .parcels import GovmapParcels
    from .plans import IplanRegistry

    return Enricher(
        parcels=GovmapParcels(http),
        plans=IplanRegistry(http),
        budget=int(section.get("budget", 40)),
        only_agricultural=bool(section.get("only_agricultural", False)),
    )


def build_appraiser(config: Config, http: HttpClient, storage: Storage) -> Any:
    """Оценка по сделкам с соседними участками, если включена в конфиге.

    Сравнимые берутся из собственной базы: сделки там появляются после
    ``landtender harvest``, который забирает архив закрытых торгов рм"и.
    Без него оценивать не по чему, и это честнее сказать сразу.
    """
    section = config.section("valuation")
    if not section.get("estimate", False):
        return None

    from .macro import HOUSING, CbsIndices
    from .valuation import MAX_AGE_YEARS, collect_comparables

    # Ряд запрашивается длиннее окна сравнения: сделка, для которой индекса
    # нет, из выборки выбрасывается, и короткий ряд молча резал бы базу.
    max_age = int(section.get("max_age_years", MAX_AGE_YEARS))
    housing = CbsIndices(http, cache_path=config.db_path.parent).series(
        HOUSING, last=(max_age + 2) * 12
    )
    comparables = collect_comparables(
        stored_lots(storage), housing_index=housing, max_age_years=max_age
    )
    if not comparables:
        log.info("Оценка включена, но сделок в базе нет — запустите landtender harvest")
        return None

    log.info(
        "Оценка: сравнимых сделок %d за %d лет%s",
        len(comparables),
        max_age,
        "" if housing else " (без поправки на индекс — ряд недоступен)",
    )
    return comparables


def _apply_estimate(lot: Lot, comparables: Any) -> None:
    """Оценка одного лота. Мало данных — поля остаются пустыми."""
    from .valuation import estimate

    try:
        value = estimate(lot, comparables)
    except Exception as exc:  # noqa: BLE001 - сводка важнее одной оценки
        log.warning("Оценка лота %s не удалась: %s", lot.uid, exc)
        return
    if value is None:
        return

    lot.estimate_nis = value.price_nis
    lot.estimate_low_nis = value.low_nis
    lot.estimate_high_nis = value.high_nis
    lot.estimate_n = value.n
    lot.estimate_r2 = value.r_squared
    lot.estimate_method = value.method


def _apply_scoring(lot: Lot, options: dict[str, Any]) -> None:
    """Пять показателей и совет по ставке. Считается локально, без сети."""
    from .bidding import (
        DEFAULT_OVERHEAD,
        DEFAULT_PURCHASE_TAX,
        DEFAULT_TARGET_ROI,
        advise,
    )
    from .scoring import score

    card = score(lot)
    lot.score_total = card.total
    lot.score_price = card.price
    lot.score_rezoning = card.rezoning
    lot.score_density = card.density
    lot.score_market = card.market
    lot.score_timing = card.timing
    lot.score_coverage = card.coverage or None

    advice = advise(
        lot,
        target_roi=float(options.get("target_roi", DEFAULT_TARGET_ROI)),
        purchase_tax=float(options.get("purchase_tax", DEFAULT_PURCHASE_TAX)),
        overhead=float(options.get("overhead", DEFAULT_OVERHEAD)),
    )
    if advice is None:
        return
    lot.max_bid_nis = advice.max_bid_nis
    lot.bid_headroom_pct = advice.headroom_pct
    lot.roi_at_min = advice.roi_at_min


def _apply_insight(lot: Lot, enricher: Any) -> None:
    """Кадастр и планы для одного лота. Отказ сервиса лот не роняет."""
    from .invest import SIGNAL_NONE, apply as apply_insight

    try:
        insight = enricher.enrich(lot)
    except Exception as exc:  # noqa: BLE001 - сводка важнее одного участка
        log.warning("Дополнение лота %s не удалось: %s", lot.uid, exc)
        return
    if insight is None:
        return

    apply_insight(lot, insight)
    lot.zoning = insight.current_use
    if insight.signal != SIGNAL_NONE:
        lot.plan_signal = insight.signal
        if insight.leading_plan is not None:
            lot.plan_number = insight.leading_plan.number
            lot.plan_url = insight.leading_plan.url


def get_fx(config: Config, http: HttpClient) -> FxRate:
    cache_path = config.db_path.parent / "fx_cache.json"
    provider = FxProvider(
        http=http,
        provider=str(config.get("fx", "provider", "boi")),
        static_rate=float(config.get("fx", "static_usd_ils", 3.70)),
        cache_hours=int(config.get("fx", "cache_hours", 12)),
        cache_path=cache_path,
    )
    return provider.get()


def run_once(
    config: Config,
    storage: Storage,
    only_sources: Sequence[str] | None = None,
    full_refresh: bool = False,
) -> RunResult:
    """Один ежедневный проход. Падение источника не роняет запуск."""
    started_at = utcnow_iso()
    backfill_land_use(storage)
    http = build_http(config)

    fx = get_fx(config, http)
    log.info("Курс USD/ILS = %.4f (%s, %s)", fx.rate, fx.source, fx.as_of)

    threshold = config.threshold_usd
    include_dev = bool(config.get("valuation", "include_development_costs", False))
    keep_priceless = bool(config.get("valuation", "keep_priceless", True))
    hide_expired = bool(config.get("general", "hide_expired", True))
    settlements = list(config.get("general", "settlements", []) or [])
    today = date.today().isoformat()

    enricher = build_enricher(config, http)
    appraiser = build_appraiser(config, http, storage)
    bidding_options = dict(config.section("bidding"))

    result = RunResult(
        started_at=started_at,
        finished_at=started_at,
        fx_rate=fx.rate,
        fx_date=fx.as_of,
        fx_source=fx.source,
    )

    now = utcnow_iso()
    for name in select_sources(config, only_sources):
        report = SourceReport(name=name, ok=False)
        source_started = time.time()
        try:
            source = build_source(name, config, http, storage, full_refresh)
            for lot in source.fetch():
                if not in_settlements(lot, settlements):
                    report.skipped_elsewhere += 1
                    continue
                if hide_expired and is_expired(lot, today):
                    report.skipped_expired += 1
                    continue
                if enricher is not None:
                    _apply_insight(lot, enricher)
                enrich_lot(lot, fx, threshold, include_dev)
                if appraiser is not None:
                    _apply_estimate(lot, appraiser)
                _apply_scoring(lot, bidding_options)
                if lot.price_usd is None and not keep_priceless:
                    continue
                report.lots += 1
                result.total_seen += 1
                status, changes = storage.upsert_lot(lot, now)
                if status == "new":
                    result.new_lots.append(lot)
                elif status == "changed":
                    result.changed_lots.append((lot, changes))
            report.ok = True
        except Exception as exc:  # noqa: BLE001 - сводка важнее одного источника
            report.error = f"{type(exc).__name__}: {exc}"
            log.warning("Источник %s упал: %s", name, report.error)
        finally:
            report.duration_sec = round(time.time() - source_started, 2)
            result.sources.append(report)
            storage.commit()

    result.finished_at = utcnow_iso()
    storage.record_run(result.started_at, result.finished_at, result.stats())
    return result


def notify(config: Config, storage: Storage, result: RunResult, dry_run: bool = False) -> int:
    """Шлёт сводку в Telegram, не повторяя уже отправленные лоты."""
    from .notify import TelegramError, TelegramNotifier
    from .report import build_telegram_digest

    telegram = config.section("telegram")
    if not telegram.get("enabled", False):
        log.info("Telegram выключен в конфиге — сводка не отправляется")
        return 0

    fresh = [lot for lot in result.new_lots if not storage.was_notified(lot.uid, "new")]
    changed = [
        (lot, changes)
        for lot, changes in result.changed_lots
        if not storage.was_notified(f"{lot.uid}:{lot.content_hash()[:12]}", "changed")
    ]
    if not fresh and not changed:
        log.info("Новых лотов нет — сводка не отправляется")
        return 0

    digest_result = RunResult(
        started_at=result.started_at,
        finished_at=result.finished_at,
        sources=result.sources,
        new_lots=fresh,
        changed_lots=changed,
        total_seen=result.total_seen,
        fx_rate=result.fx_rate,
        fx_date=result.fx_date,
        fx_source=result.fx_source,
    )
    blocks = build_telegram_digest(
        digest_result,
        threshold_usd=config.threshold_usd,
        include_standard=bool(telegram.get("send_standard_tier", True)),
        max_per_tier=int(telegram.get("max_lots_per_tier", 25)),
        include_changes=bool(telegram.get("notify_changes", True)),
        split_by_threshold=bool(config.get("general", "split_by_threshold", True)),
    )

    if dry_run:
        from .report import preview_messages

        print(preview_messages(blocks))
        return 0

    notifier = TelegramNotifier(
        bot_token=str(config.get("telegram", "bot_token") or ""),
        chat_id=str(config.get("telegram", "chat_id") or ""),
    )
    try:
        sent = notifier.send_blocks(blocks)
    except TelegramError as exc:
        log.error("Telegram: %s", exc)
        raise

    if telegram.get("attach_csv", False) and fresh:
        # Сводка уже доставлена; неудача с файлом не должна её отменять,
        # иначе лоты не пометятся отправленными и придут ещё раз завтра.
        try:
            _attach_csv(notifier, fresh, result.started_at[:10])
        except TelegramError as exc:
            log.warning("Telegram: не удалось приложить CSV: %s", exc)

    now = utcnow_iso()
    for lot in fresh:
        storage.mark_notified(lot.uid, "new", now)
    for lot, _ in changed:
        storage.mark_notified(f"{lot.uid}:{lot.content_hash()[:12]}", "changed", now)
    storage.commit()
    return sent


def in_settlements(lot: Lot, wanted: list[str]) -> bool:
    """Относится ли лот к одному из нужных городов.

    Пустой список означает «города не ограничены». Лот без указания места
    отбрасывается: попадёт он в нужный город или нет — неизвестно, а гадать
    в пользу включения значит засорять сводку.
    """
    if not wanted:
        return True
    return any(
        place_matches(text, wanted)
        for text in (lot.settlement, lot.neighborhood, lot.region, lot.tender_name)
    )


def is_expired(lot: Lot, today: str) -> bool:
    """Лот протух, если срок подачи уже прошёл или тендер закрыт.

    Дата важнее статуса: статус на портале обновляется не сразу, а срок
    подачи — жёсткий факт.
    """
    if lot.closing_date and lot.closing_date < today:
        return True
    if lot.status in CLOSED_STATUSES:
        return True
    return False


def _attach_csv(notifier: Any, lots: list[Lot], day: str) -> None:
    """Прикладывает к сводке CSV с новыми лотами — таблицей их удобнее смотреть."""
    from .report import export_csv

    with tempfile.TemporaryDirectory() as tmp:
        path = export_csv(lots, Path(tmp) / f"landtender-{day}.csv")
        notifier.send_document(path, caption=f"Новые лоты за {day}: {len(lots)}")


def probe_sources(config: Config, only_sources: Sequence[str] | None = None) -> list[SourceReport]:
    """``landtender check`` — проверяет доступность источников без записи в БД."""
    http = build_http(config)
    reports: list[SourceReport] = []
    for name in select_sources(config, only_sources):
        started = time.time()
        report = SourceReport(name=name, ok=False)
        try:
            source = build_source(name, config, http, storage=None)
            report.note = source.probe()
            report.ok = True
        except Exception as exc:  # noqa: BLE001
            report.error = f"{type(exc).__name__}: {exc}"
        report.duration_sec = round(time.time() - started, 2)
        reports.append(report)
    return reports


def open_storage(config: Config, db_path: str | Path | None = None) -> Storage:
    return Storage(Path(db_path) if db_path else config.db_path)


def backfill_land_use(storage: Storage) -> int:
    """Проставляет назначение лотам, попавшим в базу до появления колонки.

    Иначе сельхозземля, найденная в прошлые запуски, осталась бы неопознанной
    навсегда: повторно увиденный лот с тем же содержимым в базу не переписывается.
    """
    from .landuse import classify_lot as classify_landuse

    dropped = storage.clear_land_use_on_built_land()
    if dropped:
        log.info("Назначение снято с застроенных площадок: %d лот(ов)", dropped)

    filled = 0
    for row in storage.iter_unclassified():
        land_use = classify_landuse(
            row["purpose"], row["tender_name"], renewal_kind=row["renewal_kind"]
        )
        if land_use:
            storage.set_land_use(row["uid"], land_use)
            filled += 1
    if filled:
        log.info("Назначение земли проставлено задним числом: %d лот(ов)", filled)
    if filled or dropped:
        storage.commit()
    return filled


def farmland_lots(storage: Storage, only_active: bool = True) -> list[Lot]:
    """Вся сельхозземля из базы — не только найденная сегодня.

    Дневная сводка показывает лишь новое, поэтому «покажи всю сельхозземлю»
    отдельным вопросом к базе и отвечаем.
    """
    from .landuse import AGRICULTURE

    today = date.today().isoformat()
    lots = [lot for lot in stored_lots(storage) if lot.land_use == AGRICULTURE]
    if only_active:
        lots = [lot for lot in lots if not is_expired(lot, today)]
    return lots


def backfill_settlement_codes(config: Config, http: HttpClient, storage: Storage) -> int:
    """Достаёт коды населённых пунктов из поиска рм"и для уже накопленных лотов.

    Код нужен для сравнения участков, а колонка появилась позже базы и после
    миграции пуста. Детали тендеров ради неё перезабирать не нужно: код лежит
    в ответе поиска, и это один запрос на весь архив.

    Отказ портала не фатален — вернётся ноль, и рейтинг просто останется без
    оценок до следующего раза.
    """
    from .extract import as_list, to_int
    from .sources.rmi_michrazim import SEARCH_URL, SITE_HEADERS

    try:
        data = http.post_json(
            SEARCH_URL,
            json={"ActiveQuickSearch": False, "ActiveMichraz": False},
            headers=SITE_HEADERS,
        )
    except Exception as exc:  # noqa: BLE001 - рейтинг важнее одного запроса
        log.warning("Коды населённых пунктов недоступны: %s", exc)
        return 0

    codes: dict[str, int] = {}
    for tender in as_list(data):
        if not isinstance(tender, dict):
            continue
        tender_id = to_int(tender.get("MichrazID") or tender.get("MichrazId"))
        code = to_int(tender.get("KodYeshuv") or tender.get("KodYishuv"))
        if tender_id is not None and code is not None:
            codes[str(tender_id)] = code

    updated = storage.set_settlement_codes("rmi_michrazim", codes)
    log.info("Коды населённых пунктов: проставлено %d лотам", updated)
    return updated


def top_lots(
    config: Config,
    http: HttpClient,
    storage: Storage,
    limit: int = 10,
    only_active: bool = True,
) -> list[Lot]:
    """Лучшие предложения из базы, пересчитанные на сегодняшних сравнимых.

    Балл и оценка, лежащие в базе, посчитаны в день, когда лот попал в неё, —
    а база сравнимых сделок с тех пор выросла с десяти записей до пятнадцати
    тысяч. Ранжировать по сохранённым числам значило бы ставить наверх лоты,
    которым просто повезло со днём загрузки. Поэтому оценка, показатели и
    ставка считаются заново, в памяти: сети это не требует, а рейтинг
    получается по одной мерке для всех.

    Лоты без общего балла в топ не идут: место в рейтинге без основания —
    это не «десятое место», это отсутствие ответа. Без цены и без оценки —
    тоже: рейтинг предложений отвечает на вопрос «что брать и почём», а на
    него нельзя ответить, не сравнив цену с рынком.
    """
    today = date.today().isoformat()
    lots = collapse_placeholders(stored_lots(storage))
    if only_active:
        lots = [lot for lot in lots if not is_expired(lot, today)]

    comparables = build_appraiser(config, http, storage) or []
    bidding_options = config.section("bidding")

    for lot in lots:
        if comparables:
            _apply_estimate(lot, comparables)
        _apply_scoring(lot, bidding_options)

    # Рейтинг предложений без цены бессмыслен: у такого лота нет ни скидки к
    # оценке, ни доходности, ни предельной ставки — сравнивать нечего. В
    # первом прогоне два верхних места заняли тендеры, у которых приём заявок
    # ещё не открыт и цена появится только через месяц.
    # Показатель цены обязателен. Без него балл считается по «бесплатным»
    # слагаемым — сроку подачи и плотности, — и лот, о котором известно только
    # то, что заявки принимают до декабря, получает 100 из одного показателя и
    # обходит участок с честной оценкой и баллом 60. Рейтинг предложений
    # обязан опираться на сравнение цены с рынком, иначе он ранжирует не лоты,
    # а полноту наших сведений о них.
    ranked = [
        lot for lot in lots
        if lot.score_total is not None
        and lot.score_price is not None
        and lot.price_nis
        and lot.price_nis > 0
    ]
    ranked.sort(key=lambda lot: (-(lot.score_total or 0), -(lot.price_usd or 0)))
    return ranked[:limit]


def explain_top(
    config: Config, http: HttpClient, storage: Storage, only_active: bool = True
) -> dict[str, int]:
    """Счётчики причин, по которым лоты остались без оценки.

    Оценка отвечает за главный показатель рейтинга — цену против рынка. Когда
    её нет ни у кого, топ вырождается в сортировку по сроку подачи, и важно
    знать, что именно её не пускает.
    """
    from .valuation import explain_estimates

    today = date.today().isoformat()
    lots = collapse_placeholders(stored_lots(storage))
    if only_active:
        lots = [lot for lot in lots if not is_expired(lot, today)]
    return explain_estimates(lots, build_appraiser(config, http, storage) or [])


def collapse_placeholders(lots: Sequence[Lot]) -> list[Lot]:
    """Убирает тендер-заглушку, если по тому же тендеру есть разобранные участки.

    Когда детали тендера не догружались (кеш отпечатков или исчерпанный
    ``details_budget``), источник отдаёт один лот на весь тендер — без площади,
    цены и участков. В базе он живёт рядом с настоящими участками того же
    тендера, найденными в другой день, и тендер задваивается в выдаче.
    """
    detailed: set[tuple[str, str]] = {
        (lot.source, lot.tender_id)
        for lot in lots
        if lot.tender_id and lot.source_id != lot.tender_id
    }
    return [
        lot
        for lot in lots
        if not (
            lot.tender_id
            and lot.source_id == lot.tender_id
            and (lot.source, lot.tender_id) in detailed
        )
    ]


def stored_lots(storage: Storage, tier: str | None = None, since: str | None = None) -> list[Lot]:
    """Читает лоты из БД обратно в модели — для выгрузок и повторных отчётов."""
    lots: list[Lot] = []
    known = set(Lot.__dataclass_fields__)
    for row in storage.iter_lots(tier=tier, since=since):
        data = {key: row[key] for key in row.keys() if key in known}
        lots.append(Lot(**data))
    return collapse_placeholders(lots)
