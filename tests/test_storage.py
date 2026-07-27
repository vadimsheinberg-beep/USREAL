"""История лотов и дедупликация уведомлений между запусками."""

import pytest

from landtender.models import TIER_PREMIUM, TIER_STANDARD, Lot
from landtender.storage import Storage

NOW = "2026-07-27T06:00:00+00:00"
LATER = "2026-07-28T06:00:00+00:00"


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "test.sqlite3") as store:
        yield store


def make_lot(**overrides) -> Lot:
    data = dict(
        source="rmi_michrazim",
        source_id="20250142:10769/42",
        tender_name="חי/142/2025",
        settlement="חיפה",
        units=60,
        price_nis=18_500_000.0,
        price_usd=5_080_742.61,
        tier=TIER_PREMIUM,
    )
    data.update(overrides)
    return Lot(**data)


class TestUpsert:
    def test_first_sighting_is_new(self, storage):
        status, changes = storage.upsert_lot(make_lot(), NOW)
        assert status == "new"
        assert changes == {}

    def test_second_identical_sighting_is_same(self, storage):
        storage.upsert_lot(make_lot(), NOW)
        status, _ = storage.upsert_lot(make_lot(), LATER)
        assert status == "same"

    def test_price_change_is_detected(self, storage):
        storage.upsert_lot(make_lot(), NOW)
        status, changes = storage.upsert_lot(
            make_lot(price_nis=17_000_000.0, price_usd=4_668_791.6), LATER
        )
        assert status == "changed"
        assert changes["price_usd"]["before"] == pytest.approx(5_080_742.61)
        assert changes["price_usd"]["after"] == pytest.approx(4_668_791.6)

    def test_tier_change_is_detected(self, storage):
        storage.upsert_lot(make_lot(), NOW)
        _, changes = storage.upsert_lot(
            make_lot(price_nis=900_000, price_usd=247_171, tier=TIER_STANDARD), LATER
        )
        assert changes["tier"] == {"before": TIER_PREMIUM, "after": TIER_STANDARD}

    def test_first_seen_is_preserved_across_updates(self, storage):
        storage.upsert_lot(make_lot(), NOW)
        storage.upsert_lot(make_lot(units=61), LATER)
        row = storage.get_lot_row("rmi_michrazim:20250142:10769/42")
        assert row["first_seen"] == NOW
        assert row["last_seen"] == LATER

    def test_last_seen_updates_even_when_unchanged(self, storage):
        storage.upsert_lot(make_lot(), NOW)
        storage.upsert_lot(make_lot(), LATER)
        row = storage.get_lot_row("rmi_michrazim:20250142:10769/42")
        assert row["last_seen"] == LATER

    def test_lots_from_different_sources_do_not_collide(self, storage):
        storage.upsert_lot(make_lot(), NOW)
        status, _ = storage.upsert_lot(make_lot(source="yad2"), NOW)
        assert status == "new"
        assert storage.count_lots() == 2


class TestQueries:
    def test_filters_by_tier(self, storage):
        storage.upsert_lot(make_lot(), NOW)
        storage.upsert_lot(make_lot(source_id="cheap", tier=TIER_STANDARD, price_usd=100_000), NOW)
        premium = list(storage.iter_lots(tier=TIER_PREMIUM))
        assert len(premium) == 1
        assert premium[0]["tier"] == TIER_PREMIUM

    def test_sorts_by_price_desc_with_nulls_last(self, storage):
        storage.upsert_lot(make_lot(source_id="a", price_usd=1_000), NOW)
        storage.upsert_lot(make_lot(source_id="b", price_usd=None), NOW)
        storage.upsert_lot(make_lot(source_id="c", price_usd=9_000), NOW)
        order = [row["source_id"] for row in storage.iter_lots()]
        assert order == ["c", "a", "b"]

    def test_filters_by_first_seen(self, storage):
        storage.upsert_lot(make_lot(source_id="old"), NOW)
        storage.upsert_lot(make_lot(source_id="new"), LATER)
        recent = [row["source_id"] for row in storage.iter_lots(since=LATER)]
        assert recent == ["new"]


class TestTenderCache:
    def test_unknown_tender_counts_as_changed(self, storage):
        assert storage.tender_changed("rmi_michrazim", "1", "fp1") is True

    def test_same_fingerprint_is_unchanged(self, storage):
        storage.remember_tender("rmi_michrazim", "1", "fp1")
        assert storage.tender_changed("rmi_michrazim", "1", "fp1") is False

    def test_new_fingerprint_is_changed(self, storage):
        storage.remember_tender("rmi_michrazim", "1", "fp1")
        assert storage.tender_changed("rmi_michrazim", "1", "fp2") is True


class TestNotifications:
    def test_not_notified_by_default(self, storage):
        assert storage.was_notified("uid", "new") is False

    def test_marking_prevents_repeat(self, storage):
        storage.mark_notified("uid", "new", NOW)
        assert storage.was_notified("uid", "new") is True
        assert storage.was_notified("uid", "changed") is False


def test_run_history_is_recorded(storage):
    storage.record_run(NOW, LATER, {"new": 3})
    row = storage.last_run()
    assert row["started_at"] == NOW
    assert '"new": 3' in row["stats_json"]


def test_reopening_database_keeps_data(tmp_path):
    path = tmp_path / "persist.sqlite3"
    with Storage(path) as store:
        store.upsert_lot(make_lot(), NOW)
    with Storage(path) as store:
        assert store.count_lots() == 1
        assert store.upsert_lot(make_lot(), LATER)[0] == "same"
