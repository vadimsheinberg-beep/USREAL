"""Толерантный разбор ответов источников.

Государственные API Израиля меняют схему без предупреждения и смешивают
английские ключи с ивритскими подписями. Поэтому вместо жёсткого маппинга
поля ищутся по спискам синонимов, а значения приводятся к типам «как
получится, но честно»: если разобрать нельзя — ``None``, а не выдумка.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterator

# ---------------------------------------------------------------- значения --

_DIGITS_RE = re.compile(r"-?\d[\d\s, ']*(?:\.\d+)?")
_DOTNET_DATE_RE = re.compile(r"/Date\((-?\d+)")

#: Множители для «1.2 млн» в разных написаниях.
_MULTIPLIERS = (
    ("מיליון", 1_000_000),
    ('מלש"ח', 1_000_000),  # миллион шекелей
    ("million", 1_000_000),
    ("אלף", 1_000),
)


def to_float(value: Any) -> float | None:
    """``"1,234,567 ₪"`` → ``1234567.0``. Мусор → ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    multiplier = 1
    for token, factor in _MULTIPLIERS:
        if token in text:
            multiplier = factor
            break
    match = _DIGITS_RE.search(text)
    if not match:
        return None
    cleaned = re.sub(r"[\s, ']", "", match.group(0))
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    if number is None:
        return None
    try:
        return int(round(number))
    except (ValueError, OverflowError):
        return None


def to_iso_date(value: Any) -> str | None:
    """Приводит дату любого встреченного формата к ``YYYY-MM-DD``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        # Эпоха в миллисекундах — так отдают некоторые .NET-бэкенды
        seconds = float(value) / 1000.0 if abs(value) > 1e11 else float(value)
        try:
            return datetime.utcfromtimestamp(seconds).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    dotnet = _DOTNET_DATE_RE.search(text)
    if dotnet:
        return to_iso_date(int(dotnet.group(1)))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def clean_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str):
        value = str(value)
    text = re.sub(r"\s+", " ", value).strip()
    return text or None


# ------------------------------------------------------------------ ключи ---


def _normalize_key(key: str) -> str:
    return re.sub(r"[\s_\-'\"]+", "", key).lower()


def pick(mapping: dict[str, Any], names: tuple[str, ...] | list[str]) -> Any:
    """Первое непустое значение по любому из имён (регистр и ``_`` игнорируются)."""
    if not isinstance(mapping, dict):
        return None
    normalized = {_normalize_key(str(k)): v for k, v in mapping.items()}
    for name in names:
        value = normalized.get(_normalize_key(name))
        if value not in (None, "", [], {}):
            return value
    return None


def walk_dicts(obj: Any, _depth: int = 0) -> Iterator[dict[str, Any]]:
    """Обходит вложенный JSON и отдаёт все словари сверху вниз."""
    if _depth > 12:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_dicts(value, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_dicts(item, _depth + 1)


def as_list(obj: Any) -> list[Any]:
    """Ответ бывает списком, бывает ``{"results": [...]}`` — сводим к списку."""
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("results", "Results", "data", "Data", "items", "Items", "records", "value"):
            value = obj.get(key)
            if isinstance(value, list):
                return value
        return [obj]
    return []


# --------------------------------------------------- словари синонимов ------

# Имена подтверждены разведкой живого ответа портала (landtender inspect),
# а не догадками: MechirSaf — «цена порога», mechirShuma — оценка шамая,
# SchumZchiya — сумма выигрыша, HotzaotPituach — расходы на развитие.
PRICE_FINAL_KEYS = (
    "SchumZchiya", "FinalPrice", "WinningPrice", "מחיר זוכה", "מחיר סופי",
)
PRICE_MIN_KEYS = (
    "MechirSaf", "MechirSafMichraz", "MinPrice", "MechirMinimum",
    "MinimumPrice", "SchumMinimali", "מחיר מינימום",
)
PRICE_APPRAISAL_KEYS = (
    "mechirShuma", "MechirShuma", "ShumaPrice", "Shuma", "AppraisalPrice",
    "שומה", "מחיר שומה",
)
PRICE_ASKING_KEYS = ("price", "Price", "asking_price", "מחיר", "מחיר מבוקש")
DEVELOPMENT_KEYS = (
    "HotzaotPituach", "DevelopmentCosts", "פיתוח", "הוצאות פיתוח",
)
#: Банковская гарантия — по условиям рм"י не менее 10% от суммы заявки.
GUARANTEE_KEYS = ("SchumArvut", "SumArvutSarvan", "Arvut", "ערבות")
#: Максимальная цена — встречается в тендерах с потолком.
PRICE_MAX_KEYS = ("MechirMaximum", "MaxPrice", "מחיר מקסימום")

UNITS_KEYS = (
    "Kibolet",  # вместимость участка в единицах — так это поле зовётся у рм"י
    "YechidotDiur",
    "YechidotDiyur",
    "YehidotDiur",
    "HousingUnits",
    "NumberOfUnits",
    "UnitsCount",
    "יחידות דיור",
    "מספר יחידות דיור",
    'מספר יח"ד',
    'יח"ד',
)

AREA_KEYS = ("Shetach", "Area", "AreaSqm", "SizeInMeters", "square_meters", "שטח", 'שטח במ"ר')
#: Площадь разрешённой застройки — отдельно от площади участка.
BUILD_AREA_KEYS = ("ShetachBniya", "BuildArea")

SETTLEMENT_KEYS = ("Yishuv", "Yeshuv", "SettlementName", "ShemYeshuv", "city", "cityName", "יישוב", "ישוב")
NEIGHBORHOOD_KEYS = ("Shchuna", "Shkhuna", "Neighborhood", "neighborhood", "שכונה")
REGION_KEYS = ("MerchavName", "Merchav", "KodMerchav", "Region", "מרחב", "מחוז")
GUSH_KEYS = ("Gush", "gush", "block", "גוש")
CHELKA_KEYS = ("Chelka", "Helka", "chelka", "parcel", "חלקה")
# Порядок важен: текстовое название предпочтительнее числового кода.
PURPOSE_KEYS = (
    "YeudMichrazName", "Yeud", "YeudMichraz", "KodYeudMichraz",
    "Purpose", "LandUse", "ייעוד", "ייעוד מכרז", "שימוש",
)
TENDER_TYPE_KEYS = ("SugMichrazName", "SugMichraz", "KodSugMichraz", "TenderType", "סוג מכרז")
STATUS_KEYS = ("StatusMichraz", "Status", "StatusName", "סטטוס", "סטטוס מכרז")

TENDER_ID_KEYS = ("MichrazID", "MichrazId", "TenderID", "tender_id", "id", "מזהה מכרז")
TENDER_NAME_KEYS = ("MichrazName", "MisMichraz", "TenderName", "title", "שם מכרז", "מספר מכרז")

PUBLISHED_KEYS = ("PirsumDate", "PublicationDate", "published", "תאריך פרסום")
CLOSING_KEYS = ("SgiraDate", "CloseDate", "SubmissionDate", "תאריך סגירה", "מועד אחרון להגשה")
COMMITTEE_KEYS = ("VaadaDate", "CommitteeDate", "תאריך ועדה", "ועדת מכרזים")

#: По этим ключам определяем, что словарь описывает участок, а не служебный узел.
LOT_MARKER_KEYS = (
    PRICE_FINAL_KEYS
    + PRICE_MIN_KEYS
    + PRICE_APPRAISAL_KEYS
    + UNITS_KEYS
    + GUSH_KEYS
    + AREA_KEYS
)


#: Поля, без которых узел не является участком: цена, размер, вместимость.
#: Гуш и хелька в этот набор НЕ входят — портал держит их отдельным списком
#: ``GushHelka``, и такая запись сама по себе лишь кадастровая ссылка.
LOT_SUBSTANCE_KEYS = (
    PRICE_FINAL_KEYS + PRICE_MIN_KEYS + PRICE_APPRAISAL_KEYS + PRICE_MAX_KEYS
    + UNITS_KEYS + AREA_KEYS + BUILD_AREA_KEYS + DEVELOPMENT_KEYS
)


def looks_like_lot(mapping: dict[str, Any]) -> bool:
    """Эвристика: словарь похож на описание участка?

    Нужны два условия: хотя бы одно содержательное поле (цена, площадь или
    вместимость) и минимум два маркера всего. Одного случайного совпадения
    мало, а пары идентификаторов без содержимого — недостаточно.
    """
    if not isinstance(mapping, dict):
        return False

    has_substance = any(pick(mapping, (key,)) is not None for key in LOT_SUBSTANCE_KEYS)
    if not has_substance:
        return False

    hits = 0
    for key in LOT_MARKER_KEYS:
        if pick(mapping, (key,)) is not None:
            hits += 1
            if hits >= 2:
                return True
    return False
