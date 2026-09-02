"""Сборка сводки и нарезка сообщений под лимит Telegram."""

import json

from landtender.models import TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN, Lot, RunResult, SourceReport
from landtender.notify.telegram import chunk_blocks
from landtender.report import (
    FULL_CARDS,
    build_console_report,
    build_city_digest,
    build_farmland_digest,
    build_telegram_digest,
    build_top_digest,
    export_csv,
    export_json,
    fmt_area,
    fmt_nis_short,
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
        assert "Общая цена: $5.08 млн" in text

    def test_priceless_lots_are_counted_not_dropped(self):
        """Про лот без цены неизвестно, дёшев он или дорог, — но и молчать о нём нельзя."""
        lots = [self.farm(), self.farm(source_id="2", price_usd=None)]
        text = "\n".join(build_farmland_digest(lots, max_usd=100_000))
        assert "Без объявленной цены: 1" in text

    def test_the_price_ceiling_filters(self):
        lots = [
            self.farm(source_id="дешёвый", price_usd=90_000.0),
            self.farm(source_id="дорогой", price_usd=900_000.0),
        ]
        text = "\n".join(build_farmland_digest(lots, max_usd=100_000))
        assert "Порог цены: до $100 000" in text
        assert "Лотов: 1" in text

    def test_nothing_under_the_ceiling_says_so(self):
        text = "\n".join(build_farmland_digest([self.farm()], max_usd=1_000.0))
        assert "Под порог цены пока ничего не подходит" in text

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
        lots = [
            self.farm(price_usd=2_000.0, price_nis=7_280.0),
            self.farm(source_id="2", price_usd=7_000_000.0, price_nis=25_480_000.0),
        ]
        body = build_farmland_digest(lots)[1]
        assert body.index("25 480 000") < body.index("7 280")


class TestFmtArea:
    """«0.0 га» ничего не сообщает — мелкие участки показываем в метрах."""

    def test_fields_are_in_hectares(self):
        assert fmt_area(145_000) == "14.5 га"

    def test_large_numbers_are_spaced(self):
        assert fmt_area(120_000_000) == "12 000.0 га"

    def test_small_plots_stay_in_metres(self):
        assert fmt_area(1) == "1 м²"
        assert fmt_area(520) == "520 м²"

    def test_digest_header_uses_it(self):
        res = result(new_lots=[lot(land_use="agriculture", area_sqm=1.0)])
        text = "\n".join(build_telegram_digest(res, 1_000_000, split_by_threshold=False))
        assert "всего 1 м²" in text
        assert "0.0 га" not in text

    def test_farmland_header_omits_area_when_unknown(self):
        farm = lot(land_use="agriculture", area_sqm=None)
        header = build_farmland_digest([farm])[0]
        assert "Лотов: 1" in header
        assert "площадь" not in header


class TestChangeLineNeverEmpty:
    """Лот меняется по более широкому набору полей, чем расписывается построчно."""

    def test_untracked_change_still_says_something(self):
        changed = [(lot(), {})]
        text = "\n".join(build_telegram_digest(result(changed_lots=changed), 1_000_000))
        assert "♻️ обновлены данные тендера" in text

    def test_shekel_only_change_is_not_a_blank_line(self):
        changed = [(lot(), {"price_nis": {"before": 1.0, "after": 2.0}})]
        text = "\n".join(build_telegram_digest(result(changed_lots=changed), 1_000_000))
        assert "♻️ обновлены данные тендера" in text

    def test_tracked_change_is_still_spelled_out(self):
        changed = [(lot(), {"price_usd": {"before": 5_080_742.0, "after": 4_000_000.0}})]
        text = "\n".join(build_telegram_digest(result(changed_lots=changed), 1_000_000))
        assert "цена $5.08 млн → $4.00 млн" in text
        assert "обновлены данные" not in text


class TestOpeningDate:
    """Цены нет не по нашей вине: тендер объявлен, но приём заявок не начат."""

    def unopened(self):
        return lot(price_nis=None, price_usd=None, price_kind=None,
                   opening_date="2026-10-26", closing_date="2026-12-28")

    def test_empty_price_explains_itself(self):
        text = "\n".join(build_telegram_digest(result(new_lots=[self.unopened()]), 1_000_000))
        assert "💰 — · цена будет с 2026-10-26" in text

    def test_dates_are_shown_as_a_window(self):
        text = "\n".join(build_telegram_digest(result(new_lots=[self.unopened()]), 1_000_000))
        assert "заявки 2026-10-26 — 2026-12-28" in text

    def test_priced_lot_keeps_its_price_kind(self):
        priced = lot(opening_date="2026-10-26", closing_date="2026-12-28")
        text = "\n".join(build_telegram_digest(result(new_lots=[priced]), 1_000_000))
        assert "$5.08 млн (18 500 000 ₪) · min" in text
        assert "цена будет с" not in text

    def test_without_opening_date_nothing_changes(self):
        plain = lot(price_nis=None, price_usd=None, price_kind=None, closing_date="2026-12-28")
        text = "\n".join(build_telegram_digest(result(new_lots=[plain]), 1_000_000))
        assert "💰 —" in text
        assert "цена будет" not in text
        assert "до 2026-12-28" in text

    def test_opening_date_is_exported(self, tmp_path):
        path = export_csv([self.unopened()], tmp_path / "out.csv")
        assert "2026-10-26" in path.read_text("utf-8")


class TestEstimateLine:
    """Оценка показывается только вместе с тем, на чём построена."""

    def valued(self, **kw):
        data = dict(
            estimate_nis=4_100_000.0,
            estimate_low_nis=3_400_000.0,
            estimate_high_nis=4_900_000.0,
            estimate_n=23,
            estimate_r2=0.58,
            estimate_method="regression",
        )
        data.update(kw)
        return lot(**data)

    def text(self, lot_obj):
        return "\n".join(build_telegram_digest(result(new_lots=[lot_obj]), 1_000_000))

    def test_shows_value_interval_sample_and_r2(self):
        text = self.text(self.valued(price_nis=None, price_usd=None))
        assert "📊 оценка 4 100 000 ₪ (3 400 000 ₪ — 4 900 000 ₪)" in text
        assert "по 23 сделк(ам), R²=0.58" in text

    def test_median_says_so_instead_of_faking_r2(self):
        text = self.text(self.valued(estimate_r2=None, estimate_method="median",
                                     estimate_low_nis=None, estimate_high_nis=None))
        assert "медиана" in text
        assert "R²" not in text

    def test_bargain_is_flagged(self):
        text = self.text(self.valued(price_nis=2_000_000.0))
        assert "🟢 запрошено на 51% ниже оценки" in text

    def test_overpriced_is_flagged(self):
        text = self.text(self.valued(price_nis=9_000_000.0))
        assert "🔴 запрошено на 120% выше оценки" in text

    def test_price_near_the_estimate_is_not_flagged(self):
        text = self.text(self.valued(price_nis=4_000_000.0))
        assert "🟢" not in text and "🔴" not in text

    def test_no_estimate_no_line(self):
        assert "оценка" not in self.text(lot())

    def test_estimate_is_exported(self, tmp_path):
        path = export_csv([self.valued()], tmp_path / "e.csv")
        assert "4100000" in path.read_text("utf-8").replace(".0", "")


class TestScoreAndBid:
    """Балл и запас прочности в строке лота."""

    def scored(self, **kw):
        data = dict(
            score_total=84.0, score_price=95.0, score_rezoning=70.0,
            score_market=77.0, score_timing=100.0, score_coverage=4,
            max_bid_nis=3_390_000.0, bid_headroom_pct=16.9, roi_at_min=0.32,
            price_nis=2_900_000.0, price_kind="min",
        )
        data.update(kw)
        return lot(**data)

    def text(self, lot_obj):
        return "\n".join(build_telegram_digest(result(new_lots=[lot_obj]), 1_000_000))

    def test_badge_shows_score_and_coverage(self):
        """3 из 5 показателей — не то же самое, что 5 из 5."""
        assert "[84/4п]" in self.text(self.scored())

    def test_breakdown_lists_the_known_indicators(self):
        text = self.text(self.scored())
        assert "🧭 цена 95 · назначение 70 · рынок 77 · срок 100" in text
        assert "плотность" not in text

    def test_bid_line(self):
        text = self.text(self.scored())
        assert "🎯 предельная ставка 3 390 000 ₪ (+17% к минимуму)" in text
        assert "💼 ROI при выигрыше по минимуму: 32%" in text

    def test_unviable_lot_is_warned_about(self):
        text = self.text(self.scored(max_bid_nis=1_000_000.0, bid_headroom_pct=-65.0))
        assert "⚠️ минимальная цена уже выше предельной ставки" in text

    def test_lot_without_score_has_no_badge(self):
        assert "[" not in self.text(lot()).split("\n")[4]

    def test_higher_score_comes_first(self):
        res = result(new_lots=[
            lot(source_id="low", score_total=20.0, price_usd=9_000_000.0),
            lot(source_id="high", score_total=90.0, price_usd=100.0),
        ])
        text = "\n".join(build_telegram_digest(res, 1_000_000, split_by_threshold=False))
        assert text.index("[90") < text.index("[20")

    def test_unscored_lots_sink_below_scored_ones(self):
        """Даже дешёвый лот с баллом важнее дорогого без него."""
        res = result(new_lots=[
            lot(source_id="none", price_usd=9_000_000.0),
            lot(source_id="scored", score_total=10.0, price_usd=100.0),
        ])
        body = build_telegram_digest(res, 1_000_000, split_by_threshold=False)[1]
        assert body.index("$100") < body.index("$9.00 млн")

    def test_scores_are_exported(self, tmp_path):
        path = export_csv([self.scored()], tmp_path / "s.csv")
        text = path.read_text("utf-8")
        assert "score_total" in text and "max_bid_nis" in text


class TestCompactCards:
    """Две формы карточки: подробно — верхние, строкой — хвост.

    Пока оценка была редкостью, все лоты помещались в подробную форму. С
    накопленной базой сравнимых сделок оценку, ставку и балл получает почти
    каждый, и шестьдесят десятистрочных карточек превращают сводку в стену,
    где выгодный лот неотличим от проходного.
    """

    def many(self, count):
        return result(new_lots=[
            lot(source_id=str(i), tender_name=f"{100 + i}/2026",
                score_total=float(100 - i), price_nis=1_000_000.0 * (i + 1),
                price_usd=300_000.0 * (i + 1))
            for i in range(count)
        ])

    def text(self, count):
        return "\n".join(build_telegram_digest(
            self.many(count), 1_000_000, max_per_tier=count, split_by_threshold=False
        ))

    def test_top_lots_keep_the_full_card(self):
        """У верхних лотов подробная карточка узнаётся по строке единиц."""
        assert self.text(20).count("🏘 единиц:") == FULL_CARDS

    def test_the_tail_is_one_line_each(self):
        text = self.text(20)
        assert "• <a" in text
        # Хвост не потерян: лотов по-прежнему двадцать, просто короче.
        assert text.count("• <a") == 20

    def test_short_digest_shows_everything_in_full(self):
        assert self.text(FULL_CARDS).count("🏘 единиц:") == FULL_CARDS

    def test_reader_is_told_about_the_split(self):
        assert f"Подробно — первые {FULL_CARDS} по баллу" in self.text(20)
        assert "Подробно — первые" not in self.text(3)

    def test_compact_card_carries_price_and_score(self):
        line = [ln for ln in self.text(20).split("\n") if "119/2026" in ln][0]
        assert "[81" in line and "млн ₪" in line

    def test_a_long_digest_fits_in_far_fewer_messages(self):
        """Шестьдесят лотов должны читаться, а не листаться девятью экранами."""
        blocks = build_telegram_digest(
            self.many(60), 1_000_000, max_per_tier=60, split_by_threshold=False
        )
        assert len(chunk_blocks(blocks)) <= 5


class TestPriceVerdict:
    """Вердикт по цене — единственный вывод, который виден и в короткой форме."""

    def compact(self, **kw):
        data = dict(estimate_nis=10_000_000.0, score_total=10.0)
        data.update(kw)
        lots = [lot(source_id=str(i), score_total=float(90 - i)) for i in range(FULL_CARDS)]
        lots.append(lot(source_id="tail", tender_name="хвост/2026", **data))
        text = "\n".join(build_telegram_digest(
            result(new_lots=lots), 1_000_000, max_per_tier=99, split_by_threshold=False
        ))
        # Вердикт уезжает на вторую строку карточки, поэтому ловим и её.
        return [ln for ln in text.split("\n") if "хвост" in ln or "оценк" in ln]

    def test_bargain_shows_in_the_compact_line(self):
        lines = self.compact(price_nis=5_500_000.0, price_kind="final")
        assert any("🟢 −45% к оценке" in ln for ln in lines)

    def test_overpriced_shows_in_the_compact_line(self):
        lines = self.compact(price_nis=15_000_000.0, price_kind="final")
        assert any("🔴 +50% к оценке" in ln for ln in lines)

    def test_price_near_the_estimate_says_nothing(self):
        """Строка «примерно по оценке» стояла бы почти всюду и стала бы фоном."""
        lines = self.compact(price_nis=9_500_000.0, price_kind="final")
        assert not any("к оценке" in ln for ln in lines)

    def test_a_reserve_price_is_not_called_a_bargain(self):
        """מחיר מינימום ниже рыночной оценки по построению, а не по удаче.

        Оценка строится на суммах, которые победители реально заплатили, а
        цена действующего тендера — это порог, ниже которого заявку не
        примут. В первом полном прогоне 🟢 получили 127 лотов из 159
        оценённых: витрина выдавала устройство торгов за находки.
        """
        lines = self.compact(price_nis=5_500_000.0, price_kind="min")
        assert any("старт на 45% ниже оценки" in ln for ln in lines)
        assert not any("🟢" in ln for ln in lines)


class TestFmtNisShort:
    def test_millions(self):
        assert fmt_nis_short(18_500_000.0) == "18.5 млн ₪"

    def test_thousands(self):
        assert fmt_nis_short(840_000.0) == "840 тыс ₪"

    def test_small_sums_stay_exact(self):
        assert fmt_nis_short(4_200.0) == "4 200 ₪"

    def test_missing(self):
        assert fmt_nis_short(None) == "—"


class TestTopDigest:
    """Топ лучших предложений: рейтинг из всей базы, а не новинки за день."""

    def ranked(self, n=10):
        return [
            lot(source_id=str(i), tender_name=f"{100 + i}/2026",
                score_total=float(95 - i * 7), score_coverage=4,
                price_nis=1_000_000.0 * (i + 1), price_usd=300_000.0 * (i + 1))
            for i in range(n)
        ]

    def text(self, lots, **kw):
        return "\n".join(build_top_digest(lots, **kw))

    def test_places_are_numbered_in_order(self):
        text = self.text(self.ranked(3))
        assert text.index("<b>1.</b>") < text.index("<b>2.</b>") < text.index("<b>3.</b>")

    def test_every_place_gets_a_row(self):
        """Показатели идут строкой на лот, имена — один раз в начале."""
        text = self.text(self.ranked(10))
        assert text.count("<b>10.</b>") == 1
        assert text.count("тендер, город, назначение") == 1

    def test_limit_cuts_the_tail(self):
        text = self.text(self.ranked(10), limit=3)
        assert "<b>3.</b>" in text and "<b>4.</b>" not in text

    def test_header_counts_bargains(self):
        cheap = lot(source_id="дешёвый", score_total=90.0, price_kind="final",
                    price_nis=5_000_000.0, estimate_nis=10_000_000.0)
        assert "🟢 Дешевле оценки более чем на 20%: 1" in self.text([cheap])

    def test_reserve_prices_do_not_inflate_the_bargain_count(self):
        cheap = lot(source_id="стартовый", score_total=90.0, price_kind="min",
                    price_nis=5_000_000.0, estimate_nis=10_000_000.0)
        assert "Дешевле оценки" not in self.text([cheap])

    def test_header_omits_bargains_when_there_are_none(self):
        assert "Дешевле оценки" not in self.text(self.ranked(3))

    def test_empty_top_explains_what_is_missing(self):
        """Пустой топ должен объяснить, чего не хватает, а не молчать."""
        text = self.text([])
        assert "harvest" in text and "балл" in text

    def test_scope_is_stated(self):
        assert "вся база, включая закрытые" in self.text(self.ranked(2), only_active=False)

    def test_messages_fit_the_telegram_limit(self):
        assert all(len(m) <= 4096 for m in chunk_blocks(build_top_digest(self.ranked(10))))


class TestTableFormat:
    """Показатели строкой через запятую, имена — один раз в начале.

    Так строка остаётся короткой, а прочесть её можно, не помня порядок
    столбцов наизусть.
    """

    def valued(self, **kw):
        data = dict(
            settlement="ירושלים", purpose="מגורים", price_nis=3_000_000.0,
            price_usd=800_000.0, area_sqm=500.0, units=4, estimate_nis=4_000_000.0,
            score_total=72.0, score_price=87.0, score_density=100.0,
            max_bid_nis=2_900_000.0, roi_at_min=0.31, closing_date="2026-12-01",
        )
        data.update(kw)
        return lot(**data)

    def test_header_names_every_column(self):
        from landtender.report import TABLE_COLUMNS, table_lines

        header = table_lines([self.valued()])[0]
        for name in TABLE_COLUMNS:
            assert name in header

    def test_a_row_has_one_value_per_column(self):
        from landtender.report import TABLE_COLUMNS, table_lines

        row = table_lines([self.valued()])[1]
        assert row.count(",") == len(TABLE_COLUMNS) - 1

    def test_missing_values_are_dashes_not_zeros(self):
        """Ноль и «неизвестно» — разные утверждения о лоте."""
        from landtender.report import table_lines

        row = table_lines([self.valued(estimate_nis=None, score_market=None)])[1]
        assert "—" in row

    def test_deviation_from_the_estimate_is_signed(self):
        from landtender.report import table_lines

        cheap = table_lines([self.valued(price_nis=2_000_000.0)])[1]
        dear = table_lines([self.valued(price_nis=8_000_000.0)])[1]
        assert "-50" in cheap
        assert "+100" in dear

    def test_the_name_stays_a_link(self):
        """Из строки нужно попасть на сам тендер: проверить важнее краткости."""
        from landtender.report import table_lines

        assert "<a href=" in table_lines([self.valued()])[1]


class TestCityDigest:
    """Срез по городу называет вещи своими именами."""

    def resid(self, **kw):
        data = dict(settlement="ירושלים", purpose="מגורים",
                    price_usd=800_000.0, price_nis=3_000_000.0)
        data.update(kw)
        return lot(**data)

    def text(self, lots, **kw):
        return "\n".join(build_city_digest(lots, city="Иерусалим", **kw))

    def test_it_does_not_call_plots_apartments(self):
        """В базе земельные торги, а не вторичный рынок жилья."""
        text = self.text([self.resid()])
        assert "участки земельных торгов, а не квартиры" in text
        assert "Участки под жильё" in text

    def test_the_ceiling_is_stated(self):
        assert "Порог цены: до $1.00 млн" in self.text([self.resid()], max_usd=1_000_000)

    def test_empty_result_says_so(self):
        assert "Под условия ничего не подошло" in self.text([])

    def test_rows_use_the_table_format(self):
        from landtender.report import TABLE_COLUMNS

        assert ", ".join(TABLE_COLUMNS) in self.text([self.resid()])


class TestExportColumns:
    """CSV — то, по чему считают, и он обязан нести всё, что видно в сообщении."""

    def test_it_carries_every_indicator_shown_in_the_message(self, tmp_path):
        from landtender.report import EXPORT_FIELDS, export_csv
        from landtender.models import Lot

        lot = Lot(source="rmi", source_id="1", settlement="חיפה", settlement_code=4000,
                  area_sqm=500.0, price_nis=1_000_000.0)
        lot.price_per_sqm_nis = 2000.0
        path = export_csv([lot], tmp_path / "out.csv")
        header = path.read_text("utf-8-sig").splitlines()[0]
        # Цена за метр стоит в строке сообщения — значит, и в выгрузке.
        assert "price_per_sqm_nis" in header
        # Код населённого пункта — то, по чему участки сравниваются между
        # собой; без него из CSV нельзя воспроизвести подбор сравнимых.
        assert "settlement_code" in header
        assert "estimate_nis" in EXPORT_FIELDS


class TestReserveStartersInTheFullExport:
    """Полная выгрузка называет старт стартом, а не скидкой."""

    def make(self, kind):
        return lot(source_id="1", price_nis=5_000_000.0,
                   estimate_nis=10_000_000.0, price_kind=kind)

    def test_reserve_prices_are_counted_separately(self):
        from landtender.report import build_all_digest

        text = "\n".join(build_all_digest([self.make("min")]))
        assert "Стартуют ниже оценки: 1" in text
        assert "Дешевле оценки" not in text

    def test_real_deals_still_count_as_bargains(self):
        from landtender.report import build_all_digest

        text = "\n".join(build_all_digest([self.make("final")]))
        assert "🟢 Дешевле оценки более чем на 20%: 1" in text
        assert "Стартуют ниже оценки" not in text
