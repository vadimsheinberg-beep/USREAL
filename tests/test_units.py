"""Количество единиц строений."""

from landtender.models import UNITS_INFERRED, UNITS_REPORTED
from landtender.units import resolve_units, units_from_purpose, units_from_record, units_from_text


class TestFromRecord:
    def test_reads_yechidot_diur(self):
        assert units_from_record({"YechidotDiur": 96}) == (96, UNITS_REPORTED)

    def test_reads_hebrew_column(self):
        assert units_from_record({"יחידות דיור": 140}) == (140, UNITS_REPORTED)

    def test_zero_is_not_a_count(self):
        assert units_from_record({"YechidotDiur": 0}) == (None, None)

    def test_missing_field(self):
        assert units_from_record({"Gush": "10769"}) == (None, None)


class TestFromText:
    def test_finds_units_in_hebrew_description(self):
        assert units_from_text('מגרש לבנייה, 4 יח"ד') == (4, UNITS_INFERRED)

    def test_finds_full_wording(self):
        assert units_from_text("מכרז ל־120 יחידות דיור בחיפה") == (120, UNITS_INFERRED)

    def test_no_match(self):
        assert units_from_text("מגרש למכירה") == (None, None)

    def test_none_input(self):
        assert units_from_text(None) == (None, None)


class TestFromPurpose:
    def test_single_family_plot_means_one_unit(self):
        assert units_from_purpose("מגרש לבניית בית קרקע") == (1, UNITS_INFERRED)

    def test_multi_unit_purpose_is_not_inferred(self):
        assert units_from_purpose("מגורים") == (None, None)


class TestResolveUnits:
    def test_reported_field_wins_over_text(self):
        units, basis = resolve_units({"YechidotDiur": 60}, 'מכרז ל־4 יח"ד')
        assert (units, basis) == (60, UNITS_REPORTED)

    def test_text_wins_over_purpose(self):
        units, basis = resolve_units({}, '4 יח"ד', "מגרש לבניית בית קרקע")
        assert (units, basis) == (4, UNITS_INFERRED)

    def test_purpose_is_last_resort(self):
        assert resolve_units({}, None, "מגרש לבניית בית קרקע") == (1, UNITS_INFERRED)

    def test_nothing_known_stays_none(self):
        assert resolve_units({}, None, None) == (None, None)
