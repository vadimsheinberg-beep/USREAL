"""Пять показателей полезности лота и запас прочности ставки.

Показателей именно пять, и каждый отвечает на отдельный вопрос, который
инвестор задаёт по любому участку:

1. **Цена против оценки** — насколько минимальная цена ниже того, что модель
   считает рыночным уровнем. Это единственный показатель, который прямо
   говорит о деньгах.
2. **Смена назначения** — переводят ли участок под застройку и на какой это
   стадии. Для сельхозземли главный ценообразующий факт.
3. **Плотность застройки** — сколько единиц жилья приходится на дунам. Чем
   больше, тем больше выход с той же земли.
4. **Глубина рынка** — на скольких сделках построена оценка. Тонкий рынок
   означает не низкую цену, а неизвестную: продать будет некому.
5. **Срок** — сколько времени осталось до закрытия подачи. Проект, по
   которому нельзя успеть подготовить заявку, бесполезен независимо от цены.

Все пять в шкале 0–100 и складываются в общий балл с явными весами. Ни один
из них не выдумывается: если данных нет, показатель остаётся пустым и в
общий балл не входит — среднее по трём известным честнее среднего по пяти,
где два подставлены наугад.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .invest import SIGNAL_CONFIRMED, SIGNAL_EARLY, SIGNAL_LIKELY, SIGNAL_NONE
from .models import Lot
from .valuation import MIN_CREDIBLE_AREA_SQM

#: Веса показателей в общем балле. Цена весит больше всех, потому что она
#: единственная измеряется в деньгах; срок — меньше всех, потому что он
#: ограничивает, а не создаёт ценность.
WEIGHTS = {
    "price": 0.35,
    "rezoning": 0.25,
    "density": 0.15,
    "market": 0.15,
    "timing": 0.10,
}

INDICATOR_TITLES = {
    "price": "цена против оценки",
    "rezoning": "смена назначения",
    "density": "плотность застройки",
    "market": "глубина рынка",
    "timing": "срок подачи",
}

#: Балл за стадию плана. Утверждённая смена назначения — свершившийся факт,
#: поданная заявка — намерение, между ними разница принципиальная.
_REZONING_SCORE = {
    SIGNAL_CONFIRMED: 100.0,
    SIGNAL_LIKELY: 70.0,
    SIGNAL_EARLY: 40.0,
    SIGNAL_NONE: 0.0,
}

#: Плотность, которую считаем «полным баллом»: 8 квартир на дунам — плотная
#: городская застройка. Больше бывает, но выше потолка балл не растёт.
FULL_DENSITY = 8.0

#: Выборка, при которой рынок считается изученным.
DEEP_MARKET = 30

#: Меньше этого числа дней подготовить заявку с банковской гарантией
#: практически нельзя.
TIGHT_DAYS = 21
COMFORTABLE_DAYS = 90


@dataclass(frozen=True)
class Scorecard:
    """Показатели одного лота. ``None`` — данных не хватило."""

    price: float | None = None
    rezoning: float | None = None
    density: float | None = None
    market: float | None = None
    timing: float | None = None

    @property
    def known(self) -> dict[str, float]:
        return {
            name: value
            for name, value in (
                ("price", self.price),
                ("rezoning", self.rezoning),
                ("density", self.density),
                ("market", self.market),
                ("timing", self.timing),
            )
            if value is not None
        }

    @property
    def total(self) -> float | None:
        """Общий балл по известным показателям, веса перенормированы.

        Неизвестный показатель не заменяется нулём и не считается средним:
        и то и другое исказило бы вывод в конкретную сторону.
        """
        known = self.known
        if not known:
            return None
        weight = sum(WEIGHTS[name] for name in known)
        return sum(WEIGHTS[name] * value for name, value in known.items()) / weight

    @property
    def coverage(self) -> int:
        """Сколько показателей из пяти удалось посчитать."""
        return len(self.known)


def score(lot: Lot, today: date | None = None) -> Scorecard:
    """Считает пять показателей по тому, что о лоте известно."""
    return Scorecard(
        price=_price_score(lot),
        rezoning=_REZONING_SCORE.get(lot.plan_signal) if lot.plan_signal else None,
        density=_density_score(lot),
        market=_market_score(lot),
        timing=_timing_score(lot, today or date.today()),
    )


def _price_score(lot: Lot) -> float | None:
    """Насколько запрошенная цена ниже оценки.

    Ровно по оценке — 50. Вдвое дешевле оценки — 100, вдвое дороже — 0.
    Шкала линейна по отношению цены к оценке, а не по разнице в шекелях:
    скидка в миллион на участке за два миллиона и на участке за сто — разные
    события.
    """
    if not lot.estimate_nis or not lot.price_nis:
        return None
    ratio = lot.price_nis / lot.estimate_nis
    return _clamp(100.0 * (1.5 - ratio))


def _density_score(lot: Lot) -> float | None:
    """Плотность считается только по правдоподобной площади.

    Портал вместо настоящей площади иногда отдаёт «1 м²». Тендер 405/2021 с
    166 квартирами на этом квадратном метре давал плотность в двадцать тысяч
    на дунам, полный балл и первое место в рейтинге. Делить на заглушку —
    значит считать брак портала достоинством участка.
    """
    if not lot.units or not lot.area_sqm or lot.area_sqm < MIN_CREDIBLE_AREA_SQM:
        return None
    per_dunam = lot.units / (lot.area_sqm / 1000)
    return _clamp(100.0 * per_dunam / FULL_DENSITY)


def _market_score(lot: Lot) -> float | None:
    if not lot.estimate_n:
        return None
    return _clamp(100.0 * lot.estimate_n / DEEP_MARKET)


def _timing_score(lot: Lot, today: date) -> float | None:
    """Времени должно хватить на подготовку заявки и банковской гарантии."""
    days = _days_until(lot.closing_date, today)
    if days is None:
        return None
    if days <= 0:
        return 0.0
    if days >= COMFORTABLE_DAYS:
        return 100.0
    if days <= TIGHT_DAYS:
        return _clamp(50.0 * days / TIGHT_DAYS)
    span = COMFORTABLE_DAYS - TIGHT_DAYS
    return _clamp(50.0 + 50.0 * (days - TIGHT_DAYS) / span)


def _days_until(when: str | None, today: date) -> int | None:
    if not when:
        return None
    try:
        target = date.fromisoformat(when[:10])
    except (ValueError, TypeError):
        return None
    return (target - today).days


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))
