"""Ежедневный конвейер: собрать → оценить → сравнить со вчера → уведомить."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Sequence

from .config import Config
from .http import HttpClient
from .models import Lot, RunResult, SourceReport
from .money import FxProvider, FxRate, enrich_lot, utcnow_iso
from .sources import SOURCES_BY_NAME, Source, SourceContext
from .storage import Storage

log = logging.getLogger(__name__)


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
    http = build_http(config)

    fx = get_fx(config, http)
    log.info("Курс USD/ILS = %.4f (%s, %s)", fx.rate, fx.source, fx.as_of)

    threshold = config.threshold_usd
    include_dev = bool(config.get("valuation", "include_development_costs", False))
    keep_priceless = bool(config.get("valuation", "keep_priceless", True))

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
                enrich_lot(lot, fx, threshold, include_dev)
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
    )

    if dry_run:
        print("\n\n----- сообщение -----\n\n".join(blocks))
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

    now = utcnow_iso()
    for lot in fresh:
        storage.mark_notified(lot.uid, "new", now)
    for lot, _ in changed:
        storage.mark_notified(f"{lot.uid}:{lot.content_hash()[:12]}", "changed", now)
    storage.commit()
    return sent


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


def stored_lots(storage: Storage, tier: str | None = None, since: str | None = None) -> list[Lot]:
    """Читает лоты из БД обратно в модели — для выгрузок и повторных отчётов."""
    lots: list[Lot] = []
    known = set(Lot.__dataclass_fields__)
    for row in storage.iter_lots(tier=tier, since=since):
        data = {key: row[key] for key in row.keys() if key in known}
        lots.append(Lot(**data))
    return lots
