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
from .landuse import RESIDENTIAL
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

    return [lot for lot in active_lots(storage, only_active) if lot.land_use == AGRICULTURE]


def enrich_stored_lots(
    config: Config,
    http: HttpClient,
    storage: Storage,
    minutes: float = 165.0,
    only_missing_area: bool = True,
) -> dict[str, int]:
    """Прогоняет кадастр govmap по уже накопленной базе.

    Ежедневный обход дополняет несколько десятков лотов за раз — это верно
    для новинок, но не закрывает накопленное: у 1497 лотов площадь пришла
    заглушкой, и без настоящей площади их нельзя ни оценить, ни сравнить.

    Как и сбор архива, проход ограничен часами, а не числом лотов: сколько
    ответит чужой сервис, заранее неизвестно, и по истечении срока обход
    прекращается штатно, сохранив всё добытое.
    """
    from .invest import MIN_CREDIBLE_AREA_SQM, apply as apply_insight

    enricher = build_enricher(config, http)
    if enricher is None:
        log.info("Дополнение выключено — включите [enrichment] enabled")
        return {"выключено": 1}

    lots = stored_lots(storage)
    if only_missing_area:
        lots = [
            lot for lot in lots
            if (lot.area_sqm or 0) < MIN_CREDIBLE_AREA_SQM and lot.gush and lot.chelka
        ]

    # Кадастр ищет участок по гуш/хелка. Лот без них govmap найти не может,
    # и его площадь заглушкой и останется: из 1497 лотов с площадью-заглушкой
    # кадастровые номера есть у 181. Разница видна в счётчиках, чтобы её не
    # приходилось выводить из пустого результата.
    without_cadastre = sum(
        1 for lot in stored_lots(storage)
        if (lot.area_sqm or 0) < MIN_CREDIBLE_AREA_SQM and not (lot.gush and lot.chelka)
    ) if only_missing_area else 0

    counts = {
        "кандидатов": len(lots),
        "без гуш/хелка": without_cadastre,
        "просмотрено": 0,
        "площадь добыта": 0,
        "не найдено": 0,
    }
    deadline = time.monotonic() + minutes * 60 if minutes > 0 else None
    now = utcnow_iso()

    for lot in lots:
        if deadline is not None and time.monotonic() >= deadline:
            log.warning(
                "Кадастр: вышло время (%.0f мин), просмотрено %d из %d",
                minutes, counts["просмотрено"], counts["кандидатов"],
            )
            break
        counts["просмотрено"] += 1
        before = lot.area_sqm
        _apply_insight(lot, enricher)
        if (lot.area_sqm or 0) != (before or 0):
            counts["площадь добыта"] += 1
            storage.upsert_lot(lot, now)
        else:
            counts["не найдено"] += 1
        # Бюджет счётчика у дополнителя свой; здесь правит время, поэтому
        # счётчик сбрасывается, иначе проход остановился бы на сороковом лоте.
        enricher.used = 0

    return counts


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

    tenders = sum(1 for item in as_list(data) if isinstance(item, dict))
    updated = storage.set_settlement_codes("rmi_michrazim", codes)
    # Одно число «проставлено 0» неотличимо от четырёх разных причин: портал
    # не ответил, в выдаче нет кода, тендеры в базе другие, или всё уже
    # проставлено. У 946 действующих лотов рм"и места нет до сих пор, и
    # выбирать между этими причинами на глаз больше нельзя.
    log.info(
        "Коды населённых пунктов: тендеров в выдаче %d, из них с кодом %d, "
        "проставлено лотам %d",
        tenders, len(codes), updated,
    )
    if not updated:
        # «Проставлено 0» при десяти тысячах тендеров с кодом — это не ответ,
        # а вопрос. Три возможные причины требуют трёх разных починок, и
        # различать их на глаз я уже пробовал: дважды подряд ошибся.
        gap = storage.settlement_code_gap("rmi_michrazim", list(codes))
        log.info(
            "Коды населённых пунктов, разбор: %s",
            ", ".join(f"{name} {value}" for name, value in gap.items()),
        )
    return updated


def active_lots(storage: Storage, only_active: bool = True) -> list[Lot]:
    """Лоты базы, годные к показу: без тендеров-заглушек и без просроченных.

    Один и тот же отбор нужен всем витринам — топу, срезу по городу,
    сельхозземле и полной выгрузке. Пока он был скопирован в каждую из них,
    витрины незаметно расходились: топ пересчитывал показатели, а срез по
    городу показывал прочерки, потому что читал те же лоты, но иначе.
    """
    today = date.today().isoformat()
    lots = collapse_placeholders(stored_lots(storage))
    if only_active:
        lots = [lot for lot in lots if not is_expired(lot, today)]
    return lots


def evaluate_lots(
    config: Config, http: HttpClient, storage: Storage, lots: Sequence[Lot]
) -> list[Lot]:
    """Пересчитывает оценку, показатели и предельную ставку в памяти.

    Числа, лежащие в базе, посчитаны в день загрузки лота, когда сравнимых
    сделок было на порядок меньше. Показывать их рядом с сегодняшними значило
    бы мерить лоты разными мерками, поэтому перед любой выгрузкой показатели
    считаются заново — по одной и той же базе сравнимых, для всех сразу.
    """
    # Оценка опирается на чужой ряд индексов ЦСБ. Когда он недоступен,
    # выгрузка обязана выйти — с прочерками на месте оценки и с внятной
    # записью в журнале, — а не упасть целиком: витрина без одного показателя
    # всё ещё витрина, а витрины нет вовсе.
    try:
        comparables = build_appraiser(config, http, storage) or []
    except Exception as exc:  # noqa: BLE001 - выгрузка важнее одного показателя
        log.warning("Оценка недоступна, показатели цены будут пустыми: %s", exc)
        comparables = []
    bidding_options = config.section("bidding")
    for lot in lots:
        if comparables:
            _apply_estimate(lot, comparables)
        _apply_scoring(lot, bidding_options)
    return list(lots)


def rank_key(lot: Lot) -> tuple[int, float, float, float]:
    """Порядок показа: сперва лоты с ценой и баллом, потом всё остальное.

    Сортировать одним лишь баллом здесь нельзя, и это не мелочь: балл
    складывается из показателей, часть которых считается без цены — срок
    подачи, плотность. Лот, о котором известно только то, что заявки
    принимают до декабря, набирает их полностью и обходит участок с честной
    оценкой. В рейтинге такие лоты просто не участвуют, но полная выгрузка
    обязана показать и их — значит, разделять надо порядком, а не отсевом.
    """
    known = 0 if (lot.price_nis and lot.score_price is not None) else 1
    return (known, -(lot.score_total or -1.0), -(lot.price_usd or -1.0), -(lot.area_sqm or -1.0))


def all_lots(
    config: Config,
    http: HttpClient,
    storage: Storage,
    only_active: bool = True,
) -> list[Lot]:
    """Все предложения базы со всеми показателями — полная дневная выгрузка.

    В отличие от топа здесь ничего не отсеивается: лот без цены или без
    оценки остаётся в списке с прочерками на месте неизвестного. Отсутствие
    числа — это сведение о лоте, а не причина его спрятать.
    """
    lots = evaluate_lots(config, http, storage, active_lots(storage, only_active))
    lots.sort(key=rank_key)
    return lots


def summary_stats(
    config: Config,
    http: HttpClient,
    storage: Storage,
    days: int = 1,
    farmland_max_usd: float | None = None,
    city: str | None = None,
    city_max_usd: float | None = None,
) -> dict[str, Any]:
    """Числа для сводки одним сообщением: что изменилось и что в базе.

    Дневная ценность здесь — дельта, а не остаток: база меняется медленно, и
    человеку, читающему сводку каждое утро, важно «что нового», а не «что
    есть». «Что есть» лежит в CSV и не требует чтения глазами.
    """
    from datetime import timedelta

    from .landuse import AGRICULTURE

    since = (date.today() - timedelta(days=days)).isoformat()
    lots = evaluate_lots(config, http, storage, active_lots(storage))
    fresh = {lot.uid for lot in stored_lots(storage, since=since)}

    priced = [lot for lot in lots if lot.price_nis]
    estimated = [lot for lot in lots if lot.estimate_nis]
    farmland = [lot for lot in lots if lot.land_use == AGRICULTURE]
    if farmland_max_usd is not None:
        farmland = [lot for lot in farmland if lot.price_usd and lot.price_usd <= farmland_max_usd]

    city_lots_found: list[Lot] = []
    city_counts: dict[str, int] = {}
    if city:
        city_lots_found, city_counts = select_city(storage, city=city, max_usd=city_max_usd)

    return {
        "новых за сутки": len([lot for lot in lots if lot.uid in fresh]),
        "действующих лотов": len(lots),
        "с ценой": len(priced),
        "с оценкой": len(estimated),
        "сельхоз под порогом": len(farmland),
        "город": city or "",
        "город: лотов": len(city_lots_found),
        "город: воронка": city_counts,
        "лоты": lots,
    }


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
    которым просто повезло со днём загрузки.

    Лоты без общего балла в топ не идут: место в рейтинге без основания —
    это не «десятое место», это отсутствие ответа. Без цены и без оценки —
    тоже: рейтинг предложений отвечает на вопрос «что брать и почём», а на
    него нельзя ответить, не сравнив цену с рынком.
    """
    lots = evaluate_lots(config, http, storage, active_lots(storage, only_active))

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
        # Цена сделки — это то, что победитель уже заплатил. Такой лот не
        # предложение, а история: предлагать его к покупке нечестно.
        and lot.price_kind != "final"
    ]
    ranked.sort(key=lambda lot: (-(lot.score_total or 0), -(lot.price_usd or 0)))
    return _one_per_tender(ranked)[:limit]


def _one_per_tender(lots: list[Lot]) -> list[Lot]:
    """Оставляет лучший участок каждого тендера.

    В первом рабочем рейтинге восемь мест из десяти занял тендер 386/2018:
    его участки почти одинаковы — 379-403 м², по одной единице, оценка около
    двух миллионов у каждого. Десять строк про одно предложение — это не
    десять предложений, а одно, показанное десять раз. Порядок уже по баллу,
    поэтому первым встреченным и оказывается лучший участок тендера.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Lot] = []
    for lot in lots:
        key = (lot.source, lot.tender_id or lot.source_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(lot)
    return out


def explain_top(
    config: Config, http: HttpClient, storage: Storage, only_active: bool = True
) -> dict[str, int]:
    """Счётчики причин, по которым лоты остались без оценки.

    Оценка отвечает за главный показатель рейтинга — цену против рынка. Когда
    её нет ни у кого, топ вырождается в сортировку по сроку подачи, и важно
    знать, что именно её не пускает.
    """
    from .valuation import explain_estimates

    lots = active_lots(storage, only_active)
    return explain_estimates(lots, build_appraiser(config, http, storage) or [])


def city_kinds(storage: Storage, city: str, only_active: bool = True) -> dict[str, int]:
    """Какие назначения вообще встречаются у лотов города.

    Срез по Иерусалиму дважды вышел пустым, и оба раза причина оказалась не
    та, на которую я думал. Догадки о содержимом чужого поля стоят по
    прогону каждая; перечень значений стоит одного и отвечает окончательно.
    """
    from .places import resolve as resolve_places

    names, _ = resolve_places([city])
    counts: dict[str, int] = {}
    for lot in active_lots(storage, only_active):
        if not place_matches(lot.settlement, names or [city]):
            continue
        key = f"{lot.purpose or '—'} / {lot.land_use or '—'}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def select_city(
    storage: Storage,
    city: str,
    purpose: str | None = None,
    max_usd: float | None = None,
    only_active: bool = True,
    land_use: str | None = None,
) -> tuple[list[Lot], dict[str, int]]:
    """Лоты одного города плюс счётчики отсева на каждом шаге.

    Счётчики здесь не диагностика на чёрный день, а часть ответа. Пустой срез
    по Иерусалиму может означать четыре разных вещи: город не встречается в
    базе, все его лоты просрочены, ни у одного нет цены или все дороже
    порога. Без разбивки они неотличимы, и на пустое сообщение нечего
    возразить — а первый же рабочий прогон дал ровно ноль лотов.

    Город сверяется через ``places``: у израильских городов десяток
    написаний, и «Иерусалим», «ירושלים» и «Jerusalem» обязаны означать одно.
    Назначение сверяется вхождением строки — портал пишет его свободным
    текстом («מגורים», «מגורים ומסחר»), и точное равенство отсекало бы лишнее.
    """
    # Название приводится к каноническому до сравнения: matches() сверяет
    # строки как есть, а «Иерусалим» в ивритском поле не встретится никогда.
    from .places import resolve as resolve_places

    counts: dict[str, int] = {}
    lots = active_lots(storage, only_active)
    counts["активных лотов"] = len(lots)

    names, _ = resolve_places([city])
    lots = [lot for lot in lots if place_matches(lot.settlement, names or [city])]
    counts["город совпал"] = len(lots)

    if land_use:
        # Разобранная категория надёжнее текста портала: «жильё» он пишет
        # десятком способов, а то и не пишет вовсе.
        lots = [lot for lot in lots if lot.land_use == land_use]
        counts["категория совпала"] = len(lots)

    if purpose:
        # Назначение портал заполняет далеко не всегда: из 213 иерусалимских
        # лотов текст «מגורים» не стоял ни у одного, и срез по городу
        # схлопывался в ноль. Отсутствие сведения — не сведение об обратном,
        # поэтому лот с пустым назначением остаётся, если разбор назначения
        # не отнёс его к другой категории (сельхоз, промышленность и т.п.).
        # Отбрасываются только лоты с известным и другим назначением.
        matched = [lot for lot in lots if purpose in (lot.purpose or "")]
        unknown = [
            lot for lot in lots
            if not (lot.purpose or "") and lot.land_use in (None, RESIDENTIAL)
        ]
        counts["назначение совпало"] = len(matched)
        counts["назначение не указано"] = len(unknown)
        lots = matched + unknown

    priceless = [lot for lot in lots if not lot.price_usd]
    lots = [lot for lot in lots if lot.price_usd]
    counts["с объявленной ценой"] = len(lots)
    counts["без цены"] = len(priceless)

    if max_usd is not None:
        lots = [lot for lot in lots if (lot.price_usd or 0) <= max_usd]
        counts["под порогом цены"] = len(lots)
    return lots, counts


def city_lots(
    storage: Storage,
    city: str,
    purpose: str | None = None,
    max_usd: float | None = None,
    only_active: bool = True,
) -> list[Lot]:
    """Лоты одного города: по назначению и потолку цены."""
    lots, _ = select_city(
        storage, city=city, purpose=purpose, max_usd=max_usd, only_active=only_active
    )
    return lots


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
