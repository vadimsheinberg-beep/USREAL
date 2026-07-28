"""Населённые пункты: коды рм"י и разные написания названий.

Портал отдаёт населённый пункт числовым кодом ЦСБ (``KodYeshuv``), а текстом
показывает только квартал. Поэтому фильтр по городу работает в две стороны:
по коду — надёжно, по названию — как запасной путь, если код не пришёл.

Коды — стандартные коды Центрального статистического бюро Израиля. Если
какой-то окажется неверным, его можно переопределить в конфиге, не трогая код.
"""

from __future__ import annotations

#: Канонические ивритские названия и их коды ЦСБ.
SETTLEMENT_CODES: dict[str, int] = {
    "ירושלים": 3000,
    "נתניה": 7400,
    "תל אביב": 5000,
    "חיפה": 4000,
    "באר שבע": 9000,
    "ראשון לציון": 8300,
    "אשדוד": 70,
    "פתח תקווה": 7900,
    "נתיבות": 246,
    "מודיעין": 1200,
}

#: Как один и тот же город пишут на разных языках.
ALIASES: dict[str, str] = {
    # Иерусалим
    "ירושלים": "ירושלים",
    "иерусалим": "ירושלים",
    "jerusalem": "ירושלים",
    "yerushalayim": "ירושלים",
    # Нетания
    "נתניה": "נתניה",
    "нетания": "נתניה",
    "натания": "נתניה",
    "netanya": "נתניה",
    "natanya": "נתניה",
    # Прочие — на случай, если список городов расширят
    "тель-авив": "תל אביב",
    "tel aviv": "תל אביב",
    "хайфа": "חיפה",
    "haifa": "חיפה",
    "беэр-шева": "באר שבע",
    "beer sheva": "באר שבע",
    "модиин": "מודיעין",
    "modiin": "מודיעין",
}


def canonical(name: str) -> str:
    """Приводит написание города к каноническому ивритскому."""
    key = " ".join(str(name).strip().lower().split())
    return ALIASES.get(key, str(name).strip())


def code_for(name: str) -> int | None:
    """Код ЦСБ по названию в любом написании."""
    return SETTLEMENT_CODES.get(canonical(name))


def resolve(names: list[str] | tuple[str, ...]) -> tuple[list[str], list[int]]:
    """``["Иерусалим", "נתניה"]`` → канонические названия и известные коды.

    Названия без кода не теряются: по ним остаётся текстовый поиск.
    """
    canonical_names: list[str] = []
    codes: list[int] = []
    for name in names:
        canon = canonical(name)
        if canon not in canonical_names:
            canonical_names.append(canon)
        code = SETTLEMENT_CODES.get(canon)
        if code is not None and code not in codes:
            codes.append(code)
    return canonical_names, codes


def matches(text: str | None, wanted: list[str]) -> bool:
    """Содержит ли текст название одного из нужных городов."""
    if not text:
        return False
    lowered = text.strip().lower()
    return any(name.lower() in lowered for name in wanted)
