"""Распознавание назначения земли — с упором на сельхоз."""

from __future__ import annotations

import pytest

from landtender.landuse import (
    AGRICULTURE,
    COMMERCE,
    INDUSTRY,
    PUBLIC,
    RESIDENTIAL,
    TOURISM,
    badge,
    classify,
    classify_lot,
    is_agricultural,
)


@pytest.mark.parametrize(
    "text",
    [
        "חקלאות",
        "קרקע חקלאית",
        "מכרז להשכרת קרקע חקלאית לגידולי שדה",
        "מטעים",
        "מטע זיתים",
        "שטח מרעה",
        "נחלה במושב",
        "משק עזר",
        "חממות",
        "לולים",
        "רפת",
        "בית אריזה",
    ],
)
def test_agricultural_wording(text: str) -> None:
    assert classify(text) == AGRICULTURE
    assert is_agricultural(text) is True


@pytest.mark.parametrize(
    "text, expected",
    [
        ("מגורים", RESIDENTIAL),
        ("מגרש לבניית בית קרקע", RESIDENTIAL),
        ("תעשייה ומלאכה", INDUSTRY),
        ("מסחר ומשרדים", COMMERCE),
        ("מבני ציבור", PUBLIC),
        ("תיירות ונופש", TOURISM),
    ],
)
def test_other_categories(text: str, expected: str) -> None:
    assert classify(text) == expected
    assert is_agricultural(text) is False


def test_neighborhood_named_like_a_farm_is_not_farmland() -> None:
    """«נחלת יהודה» — квартал в Ришон ле-Ционе, а не надел."""
    assert classify("מגורים", "נחלת יהודה") == RESIDENTIAL
    assert is_agricultural("נחלת יהודה") is False


def test_mixed_wording_prefers_agriculture() -> None:
    assert classify("חקלאות ותיירות") == AGRICULTURE


def test_unknown_and_empty_stay_none() -> None:
    assert classify(None) is None
    assert classify("") is None
    assert classify("מכרז 142/2025") is None


def test_several_texts_are_searched_together() -> None:
    assert classify(None, "מכרז 12/2026", "קרקע חקלאית") == AGRICULTURE


def test_badge_marks_only_farmland() -> None:
    assert badge(AGRICULTURE) == "🌾 сельхоз"
    assert badge(RESIDENTIAL) is None
    assert badge(None) is None


class TestBuiltLandIsNotFarmland:
    """Площадка под снос сельхозземлёй не бывает, что бы ни говорил текст."""

    def test_renewal_project_named_after_a_street(self):
        # «זאב חקלאי» — улица в Иерусалиме; комплекс на ней шёл как сельхоз
        assert classify_lot("זאב חקלאי", renewal_kind="pinui_binui") is None

    def test_without_renewal_the_text_still_decides(self):
        assert classify_lot("קרקע חקלאית") == AGRICULTURE

    def test_empty_renewal_kind_does_not_block(self):
        assert classify_lot("קרקע חקלאית", renewal_kind=None) == AGRICULTURE
        assert classify_lot("קרקע חקלאית", renewal_kind="") == AGRICULTURE
