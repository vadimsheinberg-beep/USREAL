"""Сборка сводки и нарезка сообщений под лимит Telegram."""

import json

from landtender.models import TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN, Lot, RunResult, SourceReport
from landtender.notify.telegram import chunk_blocks
from landtender.report import (
    build_console_report,
    build_farmland_digest,
    build_telegram_digest,
    export_csv,
    export_json,
    fmt_usd,
    split_by_tier,
)


def lot(**overrides) -> Lot:
    data = dict(
        source="rmi_michrazim",
        source_id="1",
        tender_name="חי/142/2025",
        settlement="חיפה",
        url="https://apps.land.gov.il/MichrazimSite/#/michraz/20250142",
        units=60,
        units_basis="reported",
        price_nis=18_500_000.0,
        price_usd=5_080_742.0,
        price_kind="min",
        tier=TIER_PREMIUM,
    )
    data.update(overrides)
    return Lot(**data)


def result(**overrides) -> RunResult:
    data = dict(
        started_at="2026-07-27T06:00:00+00:00",
        finished_at="2026-07-27T06:04:00+00:00",
        sources=[SourceReport(name="rmi_michrazim", ok=True, lots=3)],
        new_lots=[lot()],
        total_seen=3,
        fx_rate=3.6412,
        fx_date="2026-07-27",
        fx_source="boi",
    )
    data.update(overrides)
    return RunResult(**data)


class TestFmtUsd:
    def test_millions_are_abbreviated(self):
        assert fmt_usd(5_080_742.0) == "$5.08 млн"

    def test_small_amounts_are_plain(self):
        assert fmt_usd(247_171.0) == "$247 171"

    def test_missing_price(self):
        assert fmt_usd(None) == "—"


class TestSplitByTier:
    def test_lots_land_in_their_tier(self):
        buckets = split_by_tier(
            [lot(), lot(source_id="2", tier=TIER_STANDARD, price_usd=100.0), lot(source_id="3", tier=TIER_UNKNOWN, price_usd=None)]
        )
        assert len(buckets[TIER_PREMIUM]) == 1
        assert len(buckets[TIER_STANDARD]) == 1
        assert len(buckets[TIER_UNKNOWN]) == 1

    def test_sorted_by_price_descending(self):
        buckets = split_by_tier([lot(source_id="a", price_usd=1.0), lot(source_id="b", price_usd=9.0)])
        assert [l.source_id for l in buckets[TIER_PREMIUM]] == ["b", "a"]


class TestTelegramDigest:
    def test_header_reports_threshold_and_rate(self):
        blocks = build_telegram_digest(result(), threshold_usd=1_000_000)
        assert "$1.00 млн" in blocks[0]
        assert "3.6412" in blocks[0]

    def test_premium_section_totals_units(self):
        res = result(new_lots=[lot(), lot(source_id="2", units=24)])
        blocks = build_telegram_digest(res, threshold_usd=1_000_000)
        assert "единиц строений: 84" in blocks[1]

    def test_both_tiers_are_present(self):
        res = result(new_lots=[lot(), lot(source_id="2", tier=TIER_STANDARD, price_usd=250_000.0)])
        text = "\n".join(build_telegram_digest(res, threshold_usd=1_000_000))
        assert "Дороже порога" in text
        assert "Дешевле порога" in text

    def test_standard_tier_can_be_suppressed(self):
        res = result(new_lots=[lot(), lot(source_id="2", tier=TIER_STANDARD, price_usd=250_000.0)])
        text = "\n".join(build_telegram_digest(res, threshold_usd=1_000_000, include_standard=False))
        assert "Дешевле порога" not in text

    def test_lot_line_shows_units_and_price(self):
        text = "\n".join(build_telegram_digest(result(), threshold_usd=1_000_000))
        assert "единиц: 60" in text
        assert "$5.08 млн" in text

    def test_inferred_units_are_marked(self):
        res = result(new_lots=[lot(units=4, units_basis="inferred")])
        text = "\n".join(build_telegram_digest(res, threshold_usd=1_000_000))
        assert "единиц: ≈4" in text

    def test_long_lists_are_truncated(self):
        lots = [lot(source_id=str(i)) for i in range(40)]
        text = "\n".join(build_telegram_digest(result(new_lots=lots), 1_000_000, max_per_tier=5))
        assert "и ещё 35" in text

    def test_changes_section(self):
        changed = [(lot(), {"price_usd": {"before": 5_080_742.0, "after": 4_000_000.0}})]
        text = "\n".join(build_telegram_digest(result(changed_lots=changed), 1_000_000))
        assert "Изменения по ранее найденным" in text
        assert "$5.08 млн → $4.00 млн" in text

    def test_failed_sources_are_reported(self):
        res = result(sources=[SourceReport(name="yad2", ok=False, error="HTTP 403")])
        text = "\n".join(build_telegram_digest(res, 1_000_000))
        assert "Источники с ошибкой" in text
        assert "HTTP 403" in text

    def test_html_in_source_data_is_escaped(self):
        res = result(new_lots=[lot(tender_name="<script>alert(1)</script>")])
        text = "\n".join(build_telegram_digest(res, 1_000_000))
        assert "<script>" not in text
        assert "&lt;script&gt;" in text


class TestNoPriceSplit:
    """Фильтр по цене снят: одна секция, дорогие сверху, цены на месте."""

    def res(self):
        return result(new_lots=[
            lot(source_id="a", price_usd=5_000_000.0, tier=TIER_PREMIUM),
            lot(source_id="b", price_usd=100_000.0, tier=TIER_STANDARD, units=10),
            lot(source_id="c", price_usd=None, tier=TIER_UNKNOWN, units=5),
        ])

    def blocks(self):
        return build_telegram_digest(self.res(), 1_000_000, split_by_threshold=False)

    def test_single_section_instead_of_three(self):
        text = "\n".join(self.blocks())
        assert "Все лоты" in text
        assert "Дороже порога" not in text
        assert "Дешевле порога" not in text

    def test_all_lots_are_in_it(self):
        text = "\n".join(self.blocks())
        assert "Все лоты</b> — 3 лот(ов)" in text

    def test_header_says_no_filtering(self):
        assert "Без отбора по цене и городам" in self.blocks()[0]

    def test_prices_are_still_shown(self):
        """Убран фильтр, а не цены."""
        text = "\n".join(self.blocks())
        assert "$5.00 млн" in text
        assert "$100 000" in text

    def test_expensive_lots_come_first(self):
        text = "\n".join(self.blocks())
        assert text.index("$5.00 млн") < text.index("$100 000")

    def test_priceless_lots_come_last(self):
        text = "\n".join(self.blocks())
        money = [l for l in text.split("\n") if l.startswith("  💰")]
        # У лота без цены в долларах строка начинается с прочерка
        assert money[-1].startswith("  💰 —")
        assert not money[0].startswith("  💰 —")

    def test_structures_are_counted_in_the_title(self):
        res = result(new_lots=[lot(renewal_kind="pinui_binui"), lot(source_id="2")])
        text = "\n".join(build_telegram_digest(res, 1_000_000, split_by_threshold=False))
        assert "со строениями: 1" in text

    def test_farmland_is_counted_in_the_title(self):
        res = result(new_lots=[lot(land_use="agriculture"), lot(source_id="2")])
        text = "\n".join(build_telegram_digest(res, 1_000_000, split_by_threshold=False))
        assert "сельхоз: 1" in text

    def test_farmland_gets_a_badge_and_a_header_line(self):
        res = result(new_lots=[lot(land_use="agriculture", area_sqm=145_000.0)])
        text = "\n".join(build_telegram_digest(res, 1_000_000, split_by_threshold=False))
        assert "🌾 сельхоз" in text
        assert "🌾 Сельхозземля: 1 лот(ов), всего 14.5 га" in text

    def test_farmland_header_line_absent_without_farmland(self):
        text = "\n".join(build_telegram_digest(self.res(), 1_000_000, split_by_threshold=False))
        assert "Сельхозземля" not in text

    def test_split_still_works_when_enabled(self):
        text = "\n".join(build_telegram_digest(self.res(), 1_000_000, split_by_threshold=True))
        assert "Дороже порога" in text
        assert "Все лоты" not in text

    def test_console_report_also_merges(self):
        text = build_console_report(self.res(), 1_000_000, split_by_threshold=False)
        assert "Все лоты" in text
        assert "Отбор по цене и городам отключён" in text


class TestChunkBlocks:
    def test_short_blocks_join_into_one_message(self):
        assert len(chunk_blocks(["a", "b"], limit=100)) == 1

    def test_blocks_split_when_over_limit(self):
        messages = chunk_blocks(["x" * 60, "y" * 60], limit=100)
        assert len(messages) == 2

    def test_oversized_block_is_split_on_line_boundaries(self):
        block = "\n".join(f"строка {i}" for i in range(100))
        messages = chunk_blocks([block], limit=100)
        assert len(messages) > 1
        assert all(len(m) <= 100 for m in messages)
        assert "".join(m.replace("\n", "") for m in messages).count("строка") == 100

    def test_every_message_fits_telegram_limit(self):
        res = result(new_lots=[lot(source_id=str(i)) for i in range(200)])
        blocks = build_telegram_digest(res, 1_000_000, max_per_tier=200)
        assert all(len(m) <= 4096 for m in chunk_blocks(blocks))


class TestExports:
    def test_csv_has_header_and_rows(self, tmp_path):
        path = export_csv([lot()], tmp_path / "out.csv")
        text = path.read_text("utf-8-sig")
        assert "price_usd" in text.splitlines()[0]
        assert "חיפה" in text

    def test_json_roundtrip(self, tmp_path):
        path = export_json([lot()], tmp_path / "out.json")
        rows = json.loads(path.read_text("utf-8"))
        assert rows[0]["units"] == 60
        assert rows[0]["tier"] == TIER_PREMIUM

    def test_raw_payload_is_not_exported(self, tmp_path):
        path = export_json([lot(raw={"secret": 1})], tmp_path / "out.json")
        assert "secret" not in path.read_text("utf-8")


def test_console_report_lists_sources_and_tiers():
    text = build_console_report(result(), 1_000_000)
    assert "rmi_michrazim" in text
    assert "Дороже порога" in text
    assert "Курс USD/ILS: 3.6412" in text


class TestFarmlandDigest:
    """Срез базы по сельхозземле — ответ на «покажи всю сельхозземлю»."""

    def farm(self, **overrides):
        data = dict(land_use="agriculture", area_sqm=145_000.0, purpose="חקלאות")
        data.update(overrides)
        return lot(**data)

    def test_empty_base_says_so_instead_of_a_blank_message(self):
        text = "\n".join(build_farmland_digest([]))
        assert "Ничего не найдено" in text

    def test_counts_lots_and_hectares(self):
        text = "\n".join(build_farmland_digest([self.farm(), self.farm(source_id="2")]))
        assert "Лотов: 2 · площадь: 29.0 га" in text

    def test_priced_lots_are_summed(self):
        lots = [self.farm(), self.farm(source_id="2", price_usd=None)]
        text = "\n".join(build_farmland_digest(lots))
        assert "С ценой: 1 на $5.08 млн" in text

    def test_scope_is_stated(self):
        active = "\n".join(build_farmland_digest([self.farm()], only_active=True))
        whole = "\n".join(build_farmland_digest([self.farm()], only_active=False))
        assert "действующие тендеры" in active
        assert "включая закрытые" in whole

    def test_long_lists_are_truncated(self):
        lots = [self.farm(source_id=str(i)) for i in range(10)]
        text = "\n".join(build_farmland_digest(lots, max_lots=3))
        assert "…и ещё 7" in text

    def test_expensive_first(self):
        lots = [self.farm(price_usd=2_000.0), self.farm(source_id="2", price_usd=7_000_000.0)]
        body = build_farmland_digest(lots)[1]
        assert body.index("$7.00 млн") < body.index("$2 000")
