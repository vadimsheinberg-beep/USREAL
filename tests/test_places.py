"""Отбор по городам: коды ЦСБ и разные написания названий."""

import pytest

from landtender import pipeline
from landtender.models import Lot
from landtender.places import canonical, code_for, matches, resolve


class TestCanonical:
    def test_hebrew_name_stays_as_is(self):
        assert canonical("ירושלים") == "ירושלים"

    def test_russian_name_maps_to_hebrew(self):
        assert canonical("Иерусалим") == "ירושלים"
        assert canonical("Нетания") == "נתניה"

    def test_english_name_maps_to_hebrew(self):
        assert canonical("Jerusalem") == "ירושלים"
        assert canonical("netanya") == "נתניה"

    def test_case_and_spacing_are_ignored(self):
        assert canonical("  TEL   AVIV ") == "תל אביב"

    def test_unknown_name_is_left_alone(self):
        assert canonical("Урюпинск") == "Урюпинск"


class TestCodes:
    def test_jerusalem_and_netanya_have_codes(self):
        assert code_for("Иерусалим") == 3000
        assert code_for("נתניה") == 7400

    def test_unknown_city_has_no_code(self):
        assert code_for("Урюпинск") is None

    def test_resolve_returns_names_and_codes(self):
        names, codes = resolve(["Иерусалим", "נתניה"])
        assert names == ["ירושלים", "נתניה"]
        assert codes == [3000, 7400]

    def test_resolve_keeps_names_without_codes(self):
        names, codes = resolve(["Урюпинск"])
        assert names == ["Урюпинск"]
        assert codes == []

    def test_duplicates_collapse(self):
        names, codes = resolve(["Иерусалим", "Jerusalem", "ירושלים"])
        assert names == ["ירושלים"]
        assert codes == [3000]


class TestMatches:
    def test_substring_match(self):
        assert matches("שכונה בירושלים", ["ירושלים"]) is True

    def test_no_match(self):
        assert matches("חיפה", ["ירושלים", "נתניה"]) is False

    def test_empty_text(self):
        assert matches(None, ["ירושלים"]) is False


class TestPipelineFilter:
    def lot(self, **kw):
        return Lot(source="s", source_id="1", **kw)

    def test_empty_list_keeps_everything(self):
        assert pipeline.in_settlements(self.lot(settlement="חיפה"), []) is True

    def test_matching_settlement_is_kept(self):
        assert pipeline.in_settlements(self.lot(settlement="ירושלים"), ["ירושלים"]) is True

    def test_matching_neighborhood_is_kept(self):
        lot = self.lot(settlement=None, neighborhood="פסגת זאב ירושלים")
        assert pipeline.in_settlements(lot, ["ירושלים"]) is True

    def test_other_city_is_dropped(self):
        assert pipeline.in_settlements(self.lot(settlement="חיפה"), ["ירושלים"]) is False

    def test_lot_without_any_place_is_dropped(self):
        assert pipeline.in_settlements(self.lot(), ["ירושלים"]) is False
