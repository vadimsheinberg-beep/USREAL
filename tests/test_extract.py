from landtender.extract import (
    UNITS_KEYS,
    as_list,
    clean_text,
    looks_like_lot,
    pick,
    to_float,
    to_int,
    to_iso_date,
    walk_dicts,
)


class TestToFloat:
    def test_parses_shekel_amount_with_separators(self):
        assert to_float("18,500,000 ₪") == 18_500_000.0

    def test_parses_hebrew_millions(self):
        assert to_float("1.2 מיליון") == 1_200_000.0

    def test_returns_none_for_text_without_digits(self):
        assert to_float("לא ידוע") is None

    def test_ignores_booleans(self):
        assert to_float(True) is None

    def test_passes_numbers_through(self):
        assert to_float(42) == 42.0


class TestToIsoDate:
    def test_israeli_short_format(self):
        assert to_iso_date("15/09/26") == "2026-09-15"

    def test_iso_datetime(self):
        assert to_iso_date("2026-09-15T00:00:00") == "2026-09-15"

    def test_dotnet_epoch(self):
        assert to_iso_date("/Date(1789430400000)/") is not None

    def test_garbage_is_none(self):
        assert to_iso_date("בקרוב") is None


class TestPick:
    def test_matches_ignoring_case_and_underscores(self):
        assert pick({"yechidot_diur": 12}, UNITS_KEYS) == 12

    def test_matches_hebrew_label(self):
        assert pick({"יחידות דיור": 30}, UNITS_KEYS) == 30

    def test_skips_empty_values_and_falls_through(self):
        assert pick({"YechidotDiur": "", "HousingUnits": 5}, UNITS_KEYS) == 5

    def test_missing_returns_none(self):
        assert pick({"foo": 1}, UNITS_KEYS) is None


def test_as_list_unwraps_results_key():
    assert as_list({"results": [1, 2]}) == [1, 2]
    assert as_list([3]) == [3]
    assert as_list(None) == []


def test_walk_dicts_finds_nested_nodes():
    tree = {"a": {"b": [{"c": 1}]}}
    found = list(walk_dicts(tree))
    assert {"c": 1} in found


def test_walk_dicts_stops_on_deep_recursion():
    node: dict = {}
    deep = node
    for _ in range(50):
        deep["next"] = {}
        deep = deep["next"]
    assert len(list(walk_dicts(node))) <= 13


class TestLooksLikeLot:
    def test_lot_with_price_and_units(self):
        assert looks_like_lot({"MinPrice": 100, "YechidotDiur": 4}) is True

    def test_single_marker_is_not_enough(self):
        assert looks_like_lot({"Area": 500}) is False

    def test_unrelated_node(self):
        assert looks_like_lot({"DocType": "חוברת", "Title": "x"}) is False


def test_clean_text_collapses_whitespace():
    assert clean_text("  חיפה\n  נווה שאנן ") == "חיפה נווה שאנן"
    assert clean_text("   ") is None


def test_to_int_rounds():
    assert to_int("12.6") == 13
