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


class TestMigration:
    """Новая колонка не должна требовать стирания накопленной базы."""

    OLD_SCHEMA = """
    CREATE TABLE lots (
        uid TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
        content_hash TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
    );
    """

    def old_database(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute("INSERT INTO lots VALUES ('rmi_michrazim:old', 'rmi_michrazim', 'old', 'h', ?, ?)", (NOW, NOW))
        conn.commit()
        conn.close()
        return path

    def test_missing_columns_are_added(self, tmp_path):
        from landtender.storage import _LOT_COLUMNS

        with Storage(self.old_database(tmp_path)) as store:
            present = {row[1] for row in store.conn.execute("PRAGMA table_info(lots)")}
        assert set(_LOT_COLUMNS) <= present

    def test_existing_rows_survive(self, tmp_path):
        with Storage(self.old_database(tmp_path)) as store:
            assert store.count_lots() == 1

    def test_writes_work_after_migration(self, tmp_path):
        with Storage(self.old_database(tmp_path)) as store:
            assert store.upsert_lot(make_lot(land_use="agriculture"), LATER)[0] == "new"
            row = store.get_lot_row("rmi_michrazim:20250142:10769/42")
            assert row["land_use"] == "agriculture"

    def test_migration_is_idempotent(self, tmp_path):
        path = self.old_database(tmp_path)
        for _ in range(3):
            with Storage(path) as store:
                store.upsert_lot(make_lot(), LATER)
        with Storage(path) as store:
            assert store.count_lots() == 2


class TestSettlementCodeBackfill:
    """Колонка появилась позже базы — заполнить её надо, не перезабирая архив.

    Код населённого пункта нужен для сравнения участков между собой. Миграция
    добавляет колонку пустой, а архив закрытых торгов заново не забирается:
    это шесть часов запросов. Код при этом лежит в ответе поиска — один
    запрос на все десять тысяч тендеров.
    """

    def lot(self, source_id, tender_id, **kw):
        return Lot(source="rmi_michrazim", source_id=source_id,
                   tender_id=tender_id, tender_name="тест", **kw)

    def test_codes_land_on_lots_by_tender(self, tmp_path):
        with Storage(tmp_path / "s.sqlite3") as store:
            store.upsert_lot(self.lot("1", "20250142"), NOW)
            store.upsert_lot(self.lot("2", "20250142"), NOW)
            assert store.set_settlement_codes("rmi_michrazim", {"20250142": 4000}) == 2
            rows = list(store.iter_lots())
            assert all(row["settlement_code"] == 4000 for row in rows)

    def test_existing_codes_are_left_alone(self, tmp_path):
        """Шаг должен быть повторяемым и не затирать то, что пришло от портала."""
        with Storage(tmp_path / "s.sqlite3") as store:
            store.upsert_lot(self.lot("1", "20250142", settlement_code=9000), NOW)
            store.set_settlement_codes("rmi_michrazim", {"20250142": 4000})
            assert list(store.iter_lots())[0]["settlement_code"] == 9000

    def test_other_sources_are_untouched(self, tmp_path):
        with Storage(tmp_path / "s.sqlite3") as store:
            store.upsert_lot(
                Lot(source="yad2", source_id="1", tender_id="20250142", tender_name="т"),
                NOW,
            )
            assert store.set_settlement_codes("rmi_michrazim", {"20250142": 4000}) == 0

    def test_nothing_to_do_is_not_an_error(self, tmp_path):
        with Storage(tmp_path / "s.sqlite3") as store:
            assert store.set_settlement_codes("rmi_michrazim", {}) == 0


class TestSettlementCodeGap:
    """Где рвётся связь «тендер → код населённого пункта».

    Бэкфилл доложил «проставлено 0» при десяти тысячах тендеров с кодом в
    выдаче. За этим одним числом стоят три разные причины и три разные
    починки — значит, считать надо все три.
    """

    def rows(self, storage, now="2026-09-03T00:00:00+00:00"):
        from landtender.models import Lot

        lots = [
            Lot(source="rmi_michrazim", source_id="1", tender_id="20240349",
                settlement_code=4000),
            Lot(source="rmi_michrazim", source_id="2", tender_id="20240349"),
            Lot(source="rmi_michrazim", source_id="3", tender_id="19990001"),
            Lot(source="rmi_michrazim", source_id="4", tender_id=None),
        ]
        for lot in lots:
            storage.upsert_lot(lot, now)
        return storage.settlement_code_gap("rmi_michrazim", ["20240349"])

    def test_it_separates_the_three_causes(self, tmp_path):
        from landtender.storage import Storage

        with Storage(tmp_path / "db.sqlite3") as storage:
            gap = self.rows(storage)

        assert gap["лотов источника"] == 4
        assert gap["код уже стоит"] == 1
        assert gap["без кода, тендер в выдаче есть"] == 1
        assert gap["без кода, тендера нет в выдаче"] == 1
        assert gap["без кода, номер тендера пуст"] == 1


class TestMigrationOfAnOlderDatabase:
    """Новая колонка не должна стоить накопленной базы.

    ``CREATE TABLE IF NOT EXISTS`` старую таблицу не трогает, поэтому без
    миграции первый же INSERT после добавления поля падал бы, а «починка»
    сводилась бы к удалению базы — вместе с историей уведомлений: все
    пятьдесят с лишним тысяч лотов уехали бы в канал повторно.
    """

    NEW_COLUMNS = ("reserve_price_nis", "expected_price_nis")

    def old_database(self, path):
        """База, созданная до появления новых колонок."""
        import sqlite3

        from landtender.storage import SCHEMA

        schema = "\n".join(
            line for line in SCHEMA.splitlines()
            if not any(line.strip().startswith(name) for name in self.NEW_COLUMNS)
        )
        conn = sqlite3.connect(path)
        conn.executescript(schema)
        conn.execute(
            "INSERT INTO lots (uid, source, source_id, price_nis, content_hash,"
            " first_seen, last_seen) VALUES ('u1', 'rmi_michrazim', '1', 100.0,"
            " 'hash', ?, ?)",
            (NOW, NOW),
        )
        conn.commit()
        conn.close()

    def test_missing_columns_are_added_on_open(self, tmp_path):
        path = tmp_path / "old.sqlite3"
        self.old_database(path)
        with Storage(path) as store:
            columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(lots)")}
        assert set(self.NEW_COLUMNS) <= columns

    def test_rows_collected_earlier_survive(self, tmp_path):
        path = tmp_path / "old.sqlite3"
        self.old_database(path)
        with Storage(path) as store:
            row = store.get_lot_row("u1")
            assert row["price_nis"] == 100.0
            assert row["reserve_price_nis"] is None

    def test_writing_works_after_the_migration(self, tmp_path):
        """Ради этого миграция и нужна: старая база принимает новый лот."""
        path = tmp_path / "old.sqlite3"
        self.old_database(path)
        with Storage(path) as store:
            store.upsert_lot(make_lot(reserve_price_nis=2_900_000.0), LATER)
            store.commit()
            saved = store.get_lot_row(make_lot().uid)
        assert saved["reserve_price_nis"] == 2_900_000.0
