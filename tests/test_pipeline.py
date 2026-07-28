"""Сквозной прогон: сбор → оценка → сравнение со вчера → уведомление."""

import pytest

from landtender import pipeline
from landtender.config import Config, DEFAULTS, _deep_merge
from landtender.models import TIER_PREMIUM, TIER_STANDARD, Lot
from landtender.money import FxRate
from landtender.sources.base import Source
from landtender.storage import Storage


class FakeSource(Source):
    name = "fake"
    title = "тестовый источник"
    #: Что источник вернёт на очередном вызове ``fetch``.
    batches: list[list[Lot]] = []
    calls = 0

    def fetch(self):
        batch = self.batches[min(FakeSource.calls, len(self.batches) - 1)]
        FakeSource.calls += 1
        return list(batch)


class BrokenSource(Source):
    name = "broken"
    title = "падающий источник"

    def fetch(self):
        raise RuntimeError("портал недоступен")


def make_config(**general) -> Config:
    data = _deep_merge(
        DEFAULTS,
        {
            "general": {"threshold_usd": 1_000_000, **general},
            "sources": {"fake": {"enabled": True}, "broken": {"enabled": True}},
            "telegram": {"enabled": False},
        },
    )
    return Config(data=data)


@pytest.fixture(autouse=True)
def wire_fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pipeline, "SOURCES_BY_NAME", {"fake": FakeSource, "broken": BrokenSource}
    )
    monkeypatch.setattr(pipeline, "get_fx", lambda *a, **k: FxRate(3.6412, "2026-07-27", "test"))
    monkeypatch.setattr(pipeline, "build_http", lambda config: object())
    FakeSource.calls = 0
    FakeSource.batches = [[]]


def lot(**overrides) -> Lot:
    data = dict(source="fake", source_id="1", tender_name="חי/142", units=60, price_nis=18_500_000.0)
    data.update(overrides)
    return Lot(**data)


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "run.sqlite3") as store:
        yield store


class TestRunOnce:
    def test_new_lots_are_priced_and_classified(self, storage):
        FakeSource.batches = [[lot(), lot(source_id="2", price_nis=2_000_000.0)]]
        result = pipeline.run_once(make_config(), storage, only_sources=["fake"])

        assert result.total_seen == 2
        assert len(result.new_lots) == 2
        tiers = {l.source_id: l.tier for l in result.new_lots}
        assert tiers == {"1": TIER_PREMIUM, "2": TIER_STANDARD}
        assert result.new_lots[0].price_usd == pytest.approx(5_080_742.6, rel=1e-4)

    def test_second_run_reports_nothing_new(self, storage):
        FakeSource.batches = [[lot()], [lot()]]
        pipeline.run_once(make_config(), storage, only_sources=["fake"])
        second = pipeline.run_once(make_config(), storage, only_sources=["fake"])
        assert second.new_lots == []
        assert second.changed_lots == []

    def test_price_change_surfaces_as_changed(self, storage):
        FakeSource.batches = [[lot()], [lot(price_nis=12_000_000.0)]]
        pipeline.run_once(make_config(), storage, only_sources=["fake"])
        second = pipeline.run_once(make_config(), storage, only_sources=["fake"])

        assert len(second.changed_lots) == 1
        _, changes = second.changed_lots[0]
        assert "price_usd" in changes

    def test_broken_source_does_not_stop_the_run(self, storage):
        FakeSource.batches = [[lot()]]
        result = pipeline.run_once(make_config(), storage, only_sources=["fake", "broken"])

        assert len(result.new_lots) == 1
        broken = next(s for s in result.sources if s.name == "broken")
        assert broken.ok is False
        assert "портал недоступен" in broken.error
        assert result.ok is True

    def test_run_fails_only_when_every_source_fails(self, storage):
        result = pipeline.run_once(make_config(), storage, only_sources=["broken"])
        assert result.ok is False

    def test_priceless_lots_can_be_dropped(self, storage):
        FakeSource.batches = [[lot(price_nis=None)]]
        config = make_config()
        config.data["valuation"]["keep_priceless"] = False
        result = pipeline.run_once(config, storage, only_sources=["fake"])
        assert result.new_lots == []

    def test_custom_threshold_moves_lots_between_tiers(self, storage):
        FakeSource.batches = [[lot()]]
        result = pipeline.run_once(make_config(threshold_usd=10_000_000), storage, only_sources=["fake"])
        assert result.new_lots[0].tier == TIER_STANDARD

    def test_run_is_recorded_in_history(self, storage):
        FakeSource.batches = [[lot()]]
        pipeline.run_once(make_config(), storage, only_sources=["fake"])
        assert storage.last_run() is not None

    def test_unknown_source_name_is_rejected(self, storage):
        with pytest.raises(ValueError, match="Неизвестные источники"):
            pipeline.run_once(make_config(), storage, only_sources=["нет-такого"])


class TestExpiredFilter:
    def test_lot_with_past_closing_date_is_expired(self):
        assert pipeline.is_expired(lot(closing_date="2026-07-26"), "2026-07-27") is True

    def test_lot_closing_today_is_still_current(self):
        assert pipeline.is_expired(lot(closing_date="2026-07-27"), "2026-07-27") is False

    def test_future_closing_date_is_current(self):
        assert pipeline.is_expired(lot(closing_date="2026-09-15"), "2026-07-27") is False

    def test_closed_status_is_expired_even_without_date(self):
        assert pipeline.is_expired(lot(status="סגור"), "2026-07-27") is True
        assert pipeline.is_expired(lot(status="בוטל"), "2026-07-27") is True

    def test_open_status_is_current(self):
        assert pipeline.is_expired(lot(status="פתוח"), "2026-07-27") is False

    def test_lot_without_dates_is_kept(self):
        assert pipeline.is_expired(lot(), "2026-07-27") is False

    def test_expired_lots_are_dropped_from_the_run(self, storage):
        FakeSource.batches = [[
            lot(source_id="старый", closing_date="2020-01-01"),
            lot(source_id="актуальный", closing_date="2099-01-01"),
        ]]
        result = pipeline.run_once(make_config(), storage, only_sources=["fake"])

        assert [l.source_id for l in result.new_lots] == ["актуальный"]
        assert result.sources[0].skipped_expired == 1

    def test_filter_can_be_switched_off(self, storage):
        FakeSource.batches = [[lot(source_id="старый", closing_date="2020-01-01")]]
        config = make_config()
        config.data["general"]["hide_expired"] = False
        result = pipeline.run_once(config, storage, only_sources=["fake"])

        assert len(result.new_lots) == 1
        assert result.sources[0].skipped_expired == 0


class TestSelectSources:
    def test_uses_enabled_sources_from_config(self):
        assert set(pipeline.select_sources(make_config())) == {"fake", "broken"}

    def test_explicit_list_wins(self):
        assert pipeline.select_sources(make_config(), ["fake"]) == ["fake"]

    def test_disabled_source_is_skipped(self):
        config = make_config()
        config.data["sources"]["broken"]["enabled"] = False
        assert pipeline.select_sources(config) == ["fake"]


class TestNotify:
    def test_disabled_telegram_sends_nothing(self, storage):
        FakeSource.batches = [[lot()]]
        result = pipeline.run_once(make_config(), storage, only_sources=["fake"])
        assert pipeline.notify(make_config(), storage, result) == 0

    def test_dry_run_prints_instead_of_sending(self, storage, capsys):
        FakeSource.batches = [[lot()]]
        config = make_config()
        config.data["telegram"]["enabled"] = True
        result = pipeline.run_once(config, storage, only_sources=["fake"])

        assert pipeline.notify(config, storage, result, dry_run=True) == 0
        assert "Земельные тендеры Израиля" in capsys.readouterr().out

    def test_csv_is_attached_when_enabled(self, storage, monkeypatch):
        documents = []

        class FakeNotifier:
            def __init__(self, **kwargs):
                pass

            def send_blocks(self, blocks):
                return 1

            def send_document(self, path, caption=None):
                documents.append((path.name, path.read_text("utf-8-sig"), caption))

        monkeypatch.setattr("landtender.notify.TelegramNotifier", FakeNotifier)

        FakeSource.batches = [[lot()]]
        config = make_config()
        config.data["telegram"].update({"enabled": True, "attach_csv": True})
        result = pipeline.run_once(config, storage, only_sources=["fake"])
        pipeline.notify(config, storage, result)

        assert len(documents) == 1
        name, content, caption = documents[0]
        assert name.endswith(".csv")
        assert "price_usd" in content
        assert "Новые лоты" in caption

    def test_csv_is_not_attached_when_disabled(self, storage, monkeypatch):
        documents = []

        class FakeNotifier:
            def __init__(self, **kwargs):
                pass

            def send_blocks(self, blocks):
                return 1

            def send_document(self, path, caption=None):
                documents.append(path)

        monkeypatch.setattr("landtender.notify.TelegramNotifier", FakeNotifier)

        FakeSource.batches = [[lot()]]
        config = make_config()
        config.data["telegram"].update({"enabled": True, "attach_csv": False})
        result = pipeline.run_once(config, storage, only_sources=["fake"])
        pipeline.notify(config, storage, result)

        assert documents == []

    def test_failed_csv_attachment_does_not_cancel_the_digest(self, storage, monkeypatch):
        from landtender.notify import TelegramError

        class FakeNotifier:
            def __init__(self, **kwargs):
                pass

            def send_blocks(self, blocks):
                return 1

            def send_document(self, path, caption=None):
                raise TelegramError("413 файл слишком большой")

        monkeypatch.setattr("landtender.notify.TelegramNotifier", FakeNotifier)

        FakeSource.batches = [[lot()]]
        config = make_config()
        config.data["telegram"].update({"enabled": True, "attach_csv": True})
        result = pipeline.run_once(config, storage, only_sources=["fake"])

        assert pipeline.notify(config, storage, result) == 1
        # Лоты помечены отправленными, значит завтра не придут повторно
        assert storage.was_notified(result.new_lots[0].uid, "new") is True

    def test_already_notified_lots_are_not_resent(self, storage, monkeypatch):
        sent_batches = []

        class FakeNotifier:
            def __init__(self, **kwargs):
                pass

            def send_blocks(self, blocks):
                sent_batches.append(blocks)
                return len(blocks)

            def send_document(self, path, caption=None):
                pass

        monkeypatch.setattr("landtender.notify.TelegramNotifier", FakeNotifier)

        FakeSource.batches = [[lot()]]
        config = make_config()
        config.data["telegram"]["enabled"] = True
        result = pipeline.run_once(config, storage, only_sources=["fake"])

        assert pipeline.notify(config, storage, result) > 0
        # Повторная отправка того же результата уже ничего не шлёт
        assert pipeline.notify(config, storage, result) == 0
        assert len(sent_batches) == 1


def test_stored_lots_roundtrip(storage):
    FakeSource.batches = [[lot()]]
    pipeline.run_once(make_config(), storage, only_sources=["fake"])
    restored = pipeline.stored_lots(storage)
    assert len(restored) == 1
    assert restored[0].units == 60
    assert restored[0].tier == TIER_PREMIUM
