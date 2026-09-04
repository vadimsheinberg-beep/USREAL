"""Запас прочности лота: какую ставку имеет смысл предложить.

Вопрос, на который отвечает модуль: до какой суммы можно поднимать заявку на
торгах, не теряя требуемой доходности, и насколько эта сумма выше
минимальной цены. Разрыв между ними и есть запас прочности — сколько места
остаётся для торга, прежде чем сделка перестанет быть выгодной.

Как считается. Победитель платит не только за землю:

    затраты(ставка) = ставка · (1 + налог на покупку)
                    + расходы на развитие
                    + прочие расходы (процентом от ставки)

Выходом считается оценка по сделкам с соседними участками (``valuation.py``).
Тогда предельная ставка — та, при которой доходность равна целевой:

    затраты(ставка) ≤ оценка / (1 + целевая доходность)

Уравнение линейно по ставке и решается явно, без подбора.

Чего здесь нет. Никакой надбавки «за то, что участок скоро переведут под
застройку». Соблазн умножить оценку на коэффициент велик, но такой
коэффициент был бы выдуман: сколько именно прибавит утверждение плана,
данные не говорят. Смена назначения остаётся отдельным показателем в
``scoring.py``, а не тайной добавкой к деньгам.

Ставки налогов вынесены в конфиг: они меняются законом, а не кодом.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Lot

#: Налог на покупку земли (מס רכישה). Для земли ставка одна, без ступеней,
#: которые действуют для жилья. Значение по умолчанию — рабочее, но проверять
#: его на дату сделки обязан покупатель, поэтому оно настраивается.
DEFAULT_PURCHASE_TAX = 0.06

#: Прочие расходы процентом от ставки: юрист, посредник, банковская гарантия,
#: регистрация. Порядок величины, а не точная смета.
DEFAULT_OVERHEAD = 0.03

#: Доходность, ниже которой сделка неинтересна.
DEFAULT_TARGET_ROI = 0.25


@dataclass(frozen=True)
class BidAdvice:
    """Совет по ставке со всеми числами, из которых он получен."""

    #: Предельная ставка, при которой доходность ещё равна целевой.
    max_bid_nis: float
    #: Минимальная цена тендера, если она опубликована.
    min_price_nis: float | None
    #: Во сколько раз предельная ставка выше минимальной цены.
    headroom_ratio: float | None
    #: Полные затраты при ставке, равной минимальной цене.
    outlay_at_min_nis: float | None
    #: Доходность при выигрыше по минимальной цене.
    roi_at_min: float | None
    #: Целевая доходность, от которой считалась предельная ставка.
    target_roi: float
    #: Оценка, послужившая выходом.
    exit_value_nis: float

    @property
    def viable(self) -> bool:
        """Есть ли вообще смысл участвовать.

        Предельная ставка ниже минимальной цены означает, что даже покупка по
        стартовой цене не даёт целевой доходности.
        """
        if self.min_price_nis is None:
            return self.max_bid_nis > 0
        return self.max_bid_nis >= self.min_price_nis

    @property
    def headroom_pct(self) -> float | None:
        """На сколько процентов можно поднять ставку над минимумом."""
        if self.headroom_ratio is None:
            return None
        return (self.headroom_ratio - 1.0) * 100.0


def outlay(bid_nis: float, lot: Lot, purchase_tax: float, overhead: float) -> float:
    """Полные затраты покупателя при данной ставке."""
    development = lot.development_costs_nis or 0.0
    return bid_nis * (1.0 + purchase_tax + overhead) + development


def advise(
    lot: Lot,
    target_roi: float = DEFAULT_TARGET_ROI,
    purchase_tax: float = DEFAULT_PURCHASE_TAX,
    overhead: float = DEFAULT_OVERHEAD,
) -> BidAdvice | None:
    """Совет по ставке. ``None``, если считать не на чем.

    Без оценки выхода говорить о доходности нельзя — ни ставки, ни ROI не
    существует, и выдать их означало бы выдумать.
    """
    exit_value = lot.estimate_nis
    if not exit_value or exit_value <= 0:
        return None

    development = lot.development_costs_nis or 0.0
    rate = 1.0 + purchase_tax + overhead

    # затраты ≤ выход / (1 + доходность)  ⇒  ставка ≤ (…) / rate
    budget = exit_value / (1.0 + target_roi) - development
    max_bid = budget / rate
    if max_bid <= 0:
        # Одни только расходы на развитие уже съедают всю доходность.
        max_bid = 0.0

    min_price = lot.price_nis if lot.price_kind == "min" else None
    headroom = (max_bid / min_price) if min_price else None
    outlay_at_min = outlay(min_price, lot, purchase_tax, overhead) if min_price else None
    roi_at_min = (
        (exit_value - outlay_at_min) / outlay_at_min
        if outlay_at_min and outlay_at_min > 0
        else None
    )

    return BidAdvice(
        max_bid_nis=max_bid,
        min_price_nis=min_price,
        headroom_ratio=headroom,
        outlay_at_min_nis=outlay_at_min,
        roi_at_min=roi_at_min,
        target_roi=target_roi,
        exit_value_nis=exit_value,
    )
