"""Количество единиц строений на участке.

Первый и главный источник — поле ``YechidotDiur`` (יחידות דיור) рм"י.
Если его нет, разрешаем ровно одну эвристику: назначение участка прямо
говорит «один дом на участке». Всё остальное остаётся ``None`` — пустое
значение честнее выдуманного.
"""

from __future__ import annotations

import re

from .extract import UNITS_KEYS, pick, to_int
from .models import UNITS_INFERRED, UNITS_REPORTED

#: Назначения, которые по определению означают одну единицу строения.
_SINGLE_UNIT_PATTERNS = (
    "בית קרקע",
    "בנייה עצמית",
    "מגרש לבניית בית",
    "בית צמוד קרקע",
    "מגרש בודד",
)

#: «12 יח"ד» / «12 יחידות דיור» внутри свободного текста.
_UNITS_IN_TEXT = re.compile(r"(\d[\d,]{0,6})\s*(?:יח[\"״']?ד|יחידות\s+דיור|יח\'\'ד)")


def units_from_record(record: dict) -> tuple[int | None, str | None]:
    """Возвращает ``(количество, способ получения)`` для одной записи."""
    reported = to_int(pick(record, UNITS_KEYS))
    if reported is not None and reported > 0:
        return reported, UNITS_REPORTED
    return None, None


def units_from_text(text: str | None) -> tuple[int | None, str | None]:
    """Достаёт число единиц из описания тендера, если поля не было."""
    if not text:
        return None, None
    match = _UNITS_IN_TEXT.search(text)
    if match:
        value = to_int(match.group(1))
        if value and value > 0:
            return value, UNITS_INFERRED
    return None, None


def units_from_purpose(purpose: str | None) -> tuple[int | None, str | None]:
    """Назначение «участок под индивидуальный дом» → одна единица."""
    if not purpose:
        return None, None
    for pattern in _SINGLE_UNIT_PATTERNS:
        if pattern in purpose:
            return 1, UNITS_INFERRED
    return None, None


def resolve_units(
    record: dict | None = None,
    text: str | None = None,
    purpose: str | None = None,
) -> tuple[int | None, str | None]:
    """Сводит все способы в один ответ, от самого надёжного к самому слабому."""
    for value, basis in (
        units_from_record(record or {}),
        units_from_text(text),
        units_from_purpose(purpose),
    ):
        if value is not None:
            return value, basis
    return None, None
