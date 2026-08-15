"""Назначение земли: сельское хозяйство и прочие категории.

Земельный тендер может быть на что угодно — от участка под многоэтажку до
поля под посадки. Для сельхозземли это принципиально: у неё другая цена за
дунам, другие условия аренды и другой покупатель. В сводке она должна быть
опознаваема.

Назначение приходит по-разному: у рм"י числовым кодом, у реестров — текстом.
Здесь разбирается текст; коды портал расшифровывает до вызова этого модуля.
"""

from __future__ import annotations

import re

AGRICULTURE = "agriculture"
RESIDENTIAL = "residential"
COMMERCE = "commerce"
INDUSTRY = "industry"
PUBLIC = "public"
TOURISM = "tourism"

LAND_USE_TITLES: dict[str, str] = {
    AGRICULTURE: "сельхозземля",
    RESIDENTIAL: "жильё",
    COMMERCE: "торговля и офисы",
    INDUSTRY: "промышленность",
    PUBLIC: "общественное",
    TOURISM: "туризм",
}

LAND_USE_BADGES: dict[str, str] = {
    AGRICULTURE: "🌾 сельхоз",
    RESIDENTIAL: "жильё",
    COMMERCE: "торговля",
    INDUSTRY: "промышленность",
    PUBLIC: "общественное",
    TOURISM: "туризм",
}

#: Сельскохозяйственные термины.
#:
#: Короткие слова огорожены границами: «נחלה» (надел) иначе совпало бы с
#: «נחלת יהודה» — это название квартала, а не хозяйство.
_AGRICULTURE = re.compile(
    r"חקלא"  # חקלאות, חקלאי, חקלאית — общий корень
    r"|קרקע\s+חקלאית"
    r"|\bמטע(?:ים)?\b"  # сад, плантация
    r"|\bמרעה\b"  # пастбище
    r"|\bנחלה\b|\bנחלות\b"  # надел
    r"|משק\s+עזר|משק\s+חקלאי"
    r"|\bחממ(?:ה|ות)\b"  # теплицы
    r"|\bלול(?:ים)?\b"  # птичник
    r"|\bרפת(?:ות)?\b"  # коровник
    r'|גד"ש|גדש\b'  # полевые культуры
    r"|בית\s+אריזה"  # упаковочный цех
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (AGRICULTURE, _AGRICULTURE),
    (TOURISM, re.compile(r"תיירות|מלונאות|נופש")),
    (INDUSTRY, re.compile(r"תעשייה|תעשיה|מלאכה")),
    (COMMERCE, re.compile(r"מסחר|משרדים|תעסוקה")),
    (PUBLIC, re.compile(r"מבני\s+ציבור|ציבורי")),
    (RESIDENTIAL, re.compile(r"מגורים|מגרש\s+לבניית\s+בית|בנייה\s+עצמית")),
)


def classify(*texts: str | None) -> str | None:
    """Назначение земли по тексту, либо ``None``, если понять нельзя.

    Сельское хозяйство проверяется первым: смешанные формулировки вроде
    «חקלאות ותיירות» интереснее отнести к сельхозземле.
    """
    haystack = " ".join(text for text in texts if text)
    if not haystack:
        return None
    for use, pattern in _PATTERNS:
        if pattern.search(haystack):
            return use
    return None


def is_agricultural(*texts: str | None) -> bool:
    """Сельхозземля ли это."""
    return classify(*texts) == AGRICULTURE


def badge(land_use: str | None) -> str | None:
    """Метка для строки сводки. Показываем только то, что выделяется."""
    if land_use == AGRICULTURE:
        return LAND_USE_BADGES[AGRICULTURE]
    return None
