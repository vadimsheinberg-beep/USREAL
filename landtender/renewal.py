"""Распознавание участков со строениями: реконструкция и городское обновление.

Земельный тендер бывает двух разных сортов. Один — пустой участок под
застройку. Другой — площадка, на которой уже что-то стоит: старый дом под
снос, здание под усиление, целый квартал под расселение. Экономика у них
разная, и в сводке их надо различать.

Ивритские термины, которые за этим стоят:
  * פינוי בינוי — расселение жильцов, снос, новая застройка;
  * התחדשות עירונית — городское обновление, зонтичный термин;
  * תמ"א 38 — усиление существующего дома против землетрясений;
  * הריסה ובנייה — снос и строительство заново;
  * מבנה לשימור — здание под охраной, сносить нельзя, только реконструкция.
"""

from __future__ import annotations

import re

#: Вид работ на участке. Порядок = приоритет распознавания: более конкретный
#: термин важнее общего, поэтому פינוי בינוי проверяется раньше התחדשות.
PINUI_BINUI = "pinui_binui"
TAMA_38 = "tama_38"
DEMOLITION = "demolition"
PRESERVATION = "preservation"
URBAN_RENEWAL = "urban_renewal"
EXISTING_STRUCTURE = "existing_structure"

#: Человекочитаемые названия для сводки.
RENEWAL_TITLES: dict[str, str] = {
    PINUI_BINUI: "פינוי בינוי (расселение и застройка)",
    TAMA_38: 'תמ"א 38 (усиление здания)',
    DEMOLITION: "снос и новое строительство",
    PRESERVATION: "здание под охраной",
    URBAN_RENEWAL: "городское обновление",
    EXISTING_STRUCTURE: "на участке есть строение",
}

#: Короткие метки для строки лота.
RENEWAL_BADGES: dict[str, str] = {
    PINUI_BINUI: "פינוי בינוי",
    TAMA_38: 'תמ"א 38',
    DEMOLITION: "снос",
    PRESERVATION: "שימור",
    URBAN_RENEWAL: "התחדשות",
    EXISTING_STRUCTURE: "строение",
}

#: Шаблоны по видам. Иврит пишут по-разному: гершаим бывает ", ״ или ',
#: поэтому в תמ"א 38 символ между буквами задан классом.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (PINUI_BINUI, re.compile(r"פינוי\s*[-–]?\s*בינוי|פינוי\s+ובינוי")),
    (TAMA_38, re.compile(r"תמ[\"״'’]?\s*א[\s\"״'’]*38|תמא\s*38|tama\s*38", re.IGNORECASE)),
    (PRESERVATION, re.compile(r"לשימור|מבנה\s+שימור|שימור\s+מבנים")),
    (DEMOLITION, re.compile(r"הריסה|להריסה|הריסת\s+מבנ")),
    (URBAN_RENEWAL, re.compile(r"התחדשות\s+עירונית|התחדשות\s+העירונית")),
    (
        EXISTING_STRUCTURE,
        re.compile(r"מבנה\s+קיים|מבנים\s+קיימים|מבנה\s+ישן|מבנה\s+נטוש|בנוי\s+קיים"),
    ),
)


def classify_text(*texts: str | None) -> str | None:
    """Вид работ по тексту тендера, либо ``None``, если это пустой участок."""
    haystack = " ".join(text for text in texts if text)
    if not haystack:
        return None
    for kind, pattern in _PATTERNS:
        if pattern.search(haystack):
            return kind
    return None


def has_existing_structure(built_area: float | None, kind: str | None) -> bool | None:
    """Есть ли на участке строение.

    Известная ненулевая площадь застройки — прямое свидетельство. Термины
    вроде פינוי בינוי означают то же самое, только словами. Если нет ни того,
    ни другого, ответ неизвестен: ноль в поле площади у рм"י встречается и
    там, где данных просто нет.
    """
    if built_area is not None and built_area > 0:
        return True
    if kind is not None:
        return True
    return None


def classify(
    *,
    purpose: str | None = None,
    tender_name: str | None = None,
    comments: str | None = None,
    built_area: float | None = None,
) -> tuple[str | None, bool | None]:
    """Сводит признаки в пару ``(вид работ, есть ли строение)``."""
    kind = classify_text(purpose, tender_name, comments)
    if kind is None and built_area is not None and built_area > 0:
        # Площадь застройки есть, а слов нет — строение есть, вид работ неясен
        kind = EXISTING_STRUCTURE
    return kind, has_existing_structure(built_area, kind)


def badge(kind: str | None) -> str | None:
    """Короткая метка для строки в сводке."""
    return RENEWAL_BADGES.get(kind) if kind else None
