"""Категоризация транзакций по правилам.

Правило — это категория плюс список подстрок/регулярок, которые ищутся в
описании операции, имени мерчанта и категории от источника. Побеждает
правило с наибольшим приоритетом; при равенстве — то, что описано раньше.

Встроенный набор рассчитан на израильские выписки (иврит + латиница) и на
привычные международные сервисы. Свои правила добавляются через конфиг,
они всегда идут раньше встроенных.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .models import CATEGORY_UNKNOWN, DIRECTION_INCOME, Transaction

log = logging.getLogger(__name__)

#: Мусор, который платёжные шлюзы дописывают к имени мерчанта.
_NOISE = re.compile(
    r"(paypal\s*\*|sq\s*\*|tel\s?aviv|תל אביב|בע\"?מ|ltd\.?|\binc\b|\bllc\b)",
    re.IGNORECASE,
)
#: Хвосты карт, номера филиалов и чеков: «AROMA 118» и «AROMA 9921» — один мерчант.
#: Числа внутри слова («10bis») не трогаем — там цифры часть названия.
_CARD_TAIL = re.compile(r"\b(?:x{2,}|\*{2,})?\d{2,}\b")
_SPACES = re.compile(r"\s+")


def normalize_merchant(text: str) -> str:
    """Приводит описание к сравнимому виду: без номеров карт, дат и мусора.

    ``RAMI LEVY 1234 TEL AVIV`` и ``Rami Levy  5678`` дают одну строку —
    это нужно, чтобы поиск регулярных списаний не считал их разными.
    """
    s = (text or "").lower()
    s = _NOISE.sub(" ", s)
    s = _CARD_TAIL.sub(" ", s)
    #: ``\w`` в Python юникодный, так что иврит и кириллица остаются на месте.
    s = re.sub(r"[^\w]+", " ", s)
    return _SPACES.sub(" ", s).strip()


@dataclass
class Rule:
    """Одно правило категоризации."""

    category: str
    patterns: Sequence[str]
    #: Имя для отладки: видно в отчёте, почему операция попала в категорию.
    name: str = ""
    #: Чем больше, тем раньше проверяется. Пользовательские правила: 100.
    priority: int = 0
    #: Ограничение по сумме — например, «переводы больше 5000 это аренда».
    min_amount: float | None = None
    max_amount: float | None = None
    #: ``expense`` / ``income``. None — правило годится для любых операций.
    direction: str | None = None
    #: Считать ли ``patterns`` регулярками. По умолчанию — подстроки.
    regex: bool = False

    _compiled: list[re.Pattern[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.category
        self._compiled = [self._compile(p) for p in self.patterns]

    def _compile(self, pattern: str) -> re.Pattern[str]:
        if self.regex:
            return re.compile(pattern, re.IGNORECASE)
        #: Подстроку оборачиваем в границы слова: иначе «bar» ловит «barber»,
        #: а «ace» — «space». Границы юникодные, для иврита работают так же.
        body = re.escape(pattern)
        prefix = r"(?<!\w)" if pattern[:1].isalnum() else ""
        suffix = r"(?!\w)" if pattern[-1:].isalnum() else ""
        return re.compile(f"{prefix}{body}{suffix}", re.IGNORECASE)

    def matches(self, haystack: str, tx: Transaction) -> bool:
        if self.direction and tx.direction != self.direction:
            return False
        if self.min_amount is not None and tx.amount < self.min_amount:
            return False
        if self.max_amount is not None and tx.amount > self.max_amount:
            return False
        return any(p.search(haystack) for p in self._compiled)


#: Встроенные правила: категория → что искать.
#:
#: Порядок важен только внутри одного приоритета, поэтому узкие категории
#: (аптека, подписки) описаны выше широких (покупки, прочее).
DEFAULT_RULES: list[tuple[str, list[str]]] = [
    (
        "Продукты",
        [
            "shufersal", "שופרסל", "rami levy", "רמי לוי", "yohananof", "יוחננוף",
            "victory", "ויקטורי", "tiv taam", "טיב טעם", "am:pm", "ampm", "am pm",
            "yeinot bitan", "יינות ביתן", "osher ad", "אושר עד", "mega", "מגה בעיר",
            "supermarket", "סופרמרקט", "מכולת", "carrefour", "קרפור", "hatzi hinam",
            "חצי חינם", "quik", "wolt market", "пятерочка", "магнит", "перекресток",
        ],
    ),
    (
        "Кафе и рестораны",
        [
            "aroma", "ארומה", "cofix", "קופיקס", "landwer", "לנדוור", "cafe", "קפה",
            "coffee", "starbucks", "mcdonald", "מקדונלד", "burger", "בורגר", "pizza",
            "פיצה", "sushi", "סושי", "restaurant", "מסעדה", "wolt", "וולט", "10bis",
            "תן ביס", "ten bis", "bar", "פאב", "falafel", "פלאפל", "hummus", "חומוס",
        ],
    ),
    (
        "Транспорт",
        [
            "rav kav", "רב קו", "ravkav", "moovit", "מוביט", "gett", "גט", "yango",
            "uber", "אובר", "taxi", "מונית", "רכבת", "israel railways", "egged",
            "אגד", "dan bus", "דן", "metropoline", "מטרופולין", "pango", "פנגו",
            "cellopark", "סלופארק", "חניון", "parking", "bird", "lime", "яндекс такси",
        ],
    ),
    (
        "Топливо",
        [
            "paz", "פז", "delek", "דלק", "sonol", "סונול", "dor alon", "דור אלון",
            "ten petrol", "תן דלק", "yellow", "ילו", "fuel", "תדלוק", "gas station",
        ],
    ),
    (
        "Жильё",
        [
            "שכר דירה", "rent", "аренда", "mashkanta", "משכנתא", "mortgage",
            "ועד בית", "vaad bait", "house committee",
        ],
    ),
    (
        "Коммуналка",
        [
            "חברת חשמל", "iec", "electric company", "מקורות", "mekorot", "water",
            "מים", "ארנונה", "arnona", "municipality", "עירייה", "supergas",
            "סופרגז", "amisragas", "אמישראגז", "pazgas", "גז",
        ],
    ),
    (
        "Связь и интернет",
        [
            "cellcom", "סלקום", "partner", "פרטנר", "pelephone", "פלאפון", "hot",
            "הוט", "bezeq", "בזק", "golan telecom", "גולן טלקום", "019", "012",
            "rami levy communication", "yes", "יס", "internet", "אינטרנט",
        ],
    ),
    (
        "Подписки",
        [
            "netflix", "spotify", "youtube premium", "google one", "icloud",
            "apple.com/bill", "apple services", "openai", "chatgpt", "anthropic",
            "claude", "github", "adobe", "dropbox", "notion", "figma", "microsoft 365",
            "office 365", "disney", "hbo", "amazon prime", "duolingo", "patreon",
        ],
    ),
    (
        "Аптека",
        [
            "superpharm", "סופר פארם", "super pharm", "be pharm", "בי פארם",
            "new pharm", "ניו פארם", "pharmacy", "בית מרקחת", "аптека",
        ],
    ),
    (
        "Здоровье",
        [
            "maccabi", "מכבי", "clalit", "כללית", "meuhedet", "מאוחדת", "leumit",
            "לאומית", "קופת חולים", "dentist", "רופא שיניים", "מרפאה", "clinic",
            "hospital", "בית חולים", "לאבורטוריה", "optic", "אופטיקה",
        ],
    ),
    (
        "Спорт",
        [
            "holmes place", "הולמס פלייס", "gym", "חדר כושר", "icon fitness",
            "great shape", "בריכה", "pool", "фитнес", "спортмастер", "decathlon",
            "דקטלון",
        ],
    ),
    (
        "Красота",
        [
            "מספרה", "barber", "hair", "salon", "סלון", "nails", "ציפורניים",
            "spa", "ספא", "cosmetic", "קוסמטיק",
        ],
    ),
    (
        "Дети",
        [
            "גן ילדים", "kindergarten", "צהרון", "tzaharon", "детский сад",
            "toys r us", "טויס אר אס", "shilav", "שילב", "מעון",
        ],
    ),
    (
        "Образование",
        [
            "university", "אוניברסיטה", "college", "מכללה", "course", "קורס",
            "udemy", "coursera", "школа", "בית ספר", "tuition", "שכר לימוד",
        ],
    ),
    (
        "Одежда",
        [
            "zara", "זארה", "h&m", "castro", "קסטרו", "fox", "פוקס", "renuar",
            "רנואר", "golf", "גולף", "terminal x", "טרמינל איקס", "shein", "asos",
            "nike", "adidas", "אדידס", "בגדים",
        ],
    ),
    (
        "Покупки",
        [
            "amazon", "אמזון", "aliexpress", "алиэкспресс", "ebay", "ikea", "איקאה",
            "ace", "אייס", "home center", "הום סנטר", "ksp", "קספ", "bug", "באג",
            "ivory", "אייבורי", "wallashops", "zap", "озон", "wildberries",
        ],
    ),
    (
        "Развлечения",
        [
            "cinema", "קולנוע", "yes planet", "יס פלאנט", "cinema city", "סינמה סיטי",
            "theatre", "תיאטרון", "steam", "playstation", "xbox", "nintendo",
            "eventim", "ticket", "כרטיסים", "מוזיאון", "museum",
        ],
    ),
    (
        "Путешествия",
        [
            "booking.com", "airbnb", "expedia", "hotel", "מלון", "el al", "אל על",
            "wizz", "ryanair", "israir", "ישראייר", "arkia", "ארקיע", "turkish airlines",
            "aeroflot", "airport", "נתב\"ג", "duty free", "דיוטי פרי", "kiwi.com",
        ],
    ),
    (
        "Питомцы",
        [
            "vet", "וטרינר", "pet", "חיות מחמד", "petzone", "פטזון", "animal",
        ],
    ),
    (
        "Страховка",
        [
            "insurance", "ביטוח", "harel", "הראל", "menora", "מנורה", "clal",
            "כלל ביטוח", "phoenix", "הפניקס", "migdal", "מגדל", "ayalon", "איילון",
        ],
    ),
    (
        "Налоги",
        [
            "מס הכנסה", "income tax", "ביטוח לאומי", "bituach leumi", "מע\"מ",
            "vat", "מס הכנסה", "налог", "רשות המסים",
        ],
    ),
    (
        "Комиссии банка",
        [
            "עמלה", "commission", "fee", "דמי כרטיס", "card fee", "bank charge",
            "ריבית", "interest", "комиссия",
        ],
    ),
    (
        "Наличные",
        [
            "atm", "כספומט", "משיכת מזומן", "cash withdrawal", "снятие наличных",
        ],
    ),
    (
        "Переводы",
        [
            "העברה", "transfer", "bit", "ביט", "paybox", "פייבוקס", "paypal",
            "העברה בנקאית", "перевод", "wire",
        ],
    ),
    (
        "Подарки",
        [
            "gift", "מתנה", "matana", "flowers", "פרחים", "подарок",
        ],
    ),
]

#: Правила для операций с биржи. Работают по машинным меткам, которые
#: проставляет :mod:`expenses.sources.bybit`, а не по тексту описания:
#: описание там наше собственное и может меняться.
DEFAULT_CRYPTO_RULES: list[tuple[str, list[str]]] = [
    ("Вывод с биржи", ["bybit:withdraw"]),
    ("Пополнение биржи", ["bybit:deposit"]),
    ("Комиссии биржи", ["bybit:fee"]),
    ("Фандинг", ["bybit:funding"]),
    ("Проценты по займу", ["bybit:interest"]),
    ("Сделки", ["bybit:trade"]),
    ("Переводы между счетами", ["bybit:transfer"]),
    ("Прочее на бирже", ["bybit:other"]),
]

#: Правила для поступлений — иначе зарплата уедет в «Переводы».
DEFAULT_INCOME_RULES: list[tuple[str, list[str]]] = [
    ("Зарплата", ["משכורת", "salary", "зарплата", "payroll", "שכר"]),
    ("Возвраты", ["refund", "זיכוי", "החזר", "возврат", "cashback", "קאשבק"]),
]


def build_default_rules() -> list[Rule]:
    """Собирает встроенный набор правил."""
    rules = [
        Rule(category=cat, patterns=pats, name=f"default:{cat}")
        for cat, pats in DEFAULT_RULES
    ]
    #: Метки биржи однозначны, поэтому проверяются раньше текстовых правил:
    #: иначе «Вывод BTC» рискует зацепиться за что-нибудь по подстроке.
    rules += [
        Rule(category=cat, patterns=pats, name=f"default-crypto:{cat}", priority=50)
        for cat, pats in DEFAULT_CRYPTO_RULES
    ]
    rules += [
        Rule(
            category=cat,
            patterns=pats,
            name=f"default-income:{cat}",
            priority=10,
            direction=DIRECTION_INCOME,
        )
        for cat, pats in DEFAULT_INCOME_RULES
    ]
    return rules


def rules_from_config(raw: Iterable[dict[str, Any]]) -> list[Rule]:
    """Читает пользовательские правила из конфига.

    Ожидает список словарей вида
    ``{"category": "Аренда", "patterns": ["landlord"], "priority": 100}``.
    Приоритет по умолчанию — 100, то есть выше любого встроенного.
    """
    rules: list[Rule] = []
    for i, item in enumerate(raw):
        category = str(item.get("category") or "").strip()
        patterns = item.get("patterns") or item.get("match") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not category or not patterns:
            log.warning("правило #%d пропущено: нужны category и patterns", i + 1)
            continue
        rules.append(
            Rule(
                category=category,
                patterns=[str(p) for p in patterns],
                name=str(item.get("name") or f"custom:{category}"),
                priority=int(item.get("priority", 100)),
                min_amount=_opt_float(item.get("min_amount")),
                max_amount=_opt_float(item.get("max_amount")),
                direction=item.get("direction"),
                regex=bool(item.get("regex", False)),
            )
        )
    return rules


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


class Categorizer:
    """Присваивает категории по набору правил."""

    def __init__(
        self,
        rules: Sequence[Rule] | None = None,
        *,
        use_defaults: bool = True,
        trust_source_category: bool = False,
    ) -> None:
        collected = list(rules or [])
        if use_defaults:
            collected += build_default_rules()
        #: Сортировка стабильная, поэтому внутри приоритета сохраняется порядок описания.
        self.rules = sorted(collected, key=lambda r: -r.priority)
        self.trust_source_category = trust_source_category

    def categorize(self, tx: Transaction) -> Transaction:
        """Проставляет ``category``/``category_rule`` и нормализует мерчанта."""
        if not tx.merchant:
            tx.merchant = normalize_merchant(tx.description)

        haystack = " ".join(
            part for part in (tx.description, tx.merchant, tx.source_category) if part
        )
        for rule in self.rules:
            if rule.matches(haystack, tx):
                tx.category = rule.category
                tx.category_rule = rule.name
                return tx

        if self.trust_source_category and tx.source_category:
            tx.category = tx.source_category
            tx.category_rule = "source"
            return tx

        tx.category = CATEGORY_UNKNOWN
        tx.category_rule = None
        return tx

    def categorize_all(self, transactions: Iterable[Transaction]) -> list[Transaction]:
        return [self.categorize(tx) for tx in transactions]

    def uncategorized(self, transactions: Iterable[Transaction]) -> list[Transaction]:
        """Операции, которые не поймало ни одно правило.

        Основной инструмент донастройки: смотришь список и дописываешь
        правила в конфиг, пока он не опустеет.
        """
        return [tx for tx in transactions if tx.category_rule is None]
