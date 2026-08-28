"""Инвестиционный признак участка: что с ним собираются сделать.

Соединяет три источника в один вывод по лоту:

  тендер рм"י → гуш/хелка → кадастр govmap (площадь, точка)
              → реестр планов iplan (что накрывает эту точку)

Смысл один. Сельхозземля в Израиле стоит дёшево ровно до тех пор, пока её
назначение не сменили. Поэтому вопрос «какие планы накрывают участок и на
какой они стадии» важнее любой другой характеристики лота: он отделяет поле,
которое так и останется полем, от поля, которое через два года станет
кварталом.

Оценка намеренно грубая — четыре ступени, а не число с двумя знаками.
Данные позволяют сказать «плана нет» / «план подан» / «депонирован» /
«утверждён», и притворяться, что мы знаем больше, было бы враньём.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .landuse import AGRICULTURE
from .models import Lot
from .parcels import GovmapParcels, Parcel
from .plans import (
    STAGE_APPROVED,
    STAGE_DEPOSITED,
    STAGE_ORDER,
    STAGE_SUBMITTED,
    IplanRegistry,
    LandUse,
    Plan,
)

log = logging.getLogger(__name__)

#: Ступени сигнала.
SIGNAL_NONE = "none"  # планов, меняющих назначение, не видно
SIGNAL_EARLY = "early"  # план подан, до решения далеко
SIGNAL_LIKELY = "likely"  # план депонирован — опубликован для возражений
SIGNAL_CONFIRMED = "confirmed"  # план утверждён, назначение уже сменилось

SIGNAL_TITLES = {
    SIGNAL_NONE: "смены назначения не видно",
    SIGNAL_EARLY: "план подан",
    SIGNAL_LIKELY: "план депонирован",
    SIGNAL_CONFIRMED: "план утверждён",
}

SIGNAL_BADGES = {
    SIGNAL_EARLY: "📝 план подан",
    SIGNAL_LIKELY: "📈 план депонирован",
    SIGNAL_CONFIRMED: "🏗 назначение сменено",
}

#: Стадия плана → ступень сигнала.
_STAGE_TO_SIGNAL = {
    STAGE_SUBMITTED: SIGNAL_EARLY,
    STAGE_DEPOSITED: SIGNAL_LIKELY,
    STAGE_APPROVED: SIGNAL_CONFIRMED,
}

_SIGNAL_ORDER = {SIGNAL_NONE: 0, SIGNAL_EARLY: 1, SIGNAL_LIKELY: 2, SIGNAL_CONFIRMED: 3}


@dataclass
class Insight:
    """Что удалось узнать об участке сверх самого тендера."""

    parcel: Parcel | None = None
    plans: list[Plan] = field(default_factory=list)
    land_uses: list[LandUse] = field(default_factory=list)
    signal: str = SIGNAL_NONE
    #: План, который дал сигнал, — на него и ссылаемся в сводке.
    leading_plan: Plan | None = None

    @property
    def current_use(self) -> str | None:
        """Назначение по действующему плану, если реестр его знает."""
        for use in self.land_uses:
            if use.mavat_name:
                return use.mavat_name
        return None

    @property
    def units_ahead(self) -> int | None:
        """Сколько квартир добавляют накрывающие планы."""
        deltas = [p.units_delta for p in self.plans if p.units_delta]
        return sum(deltas) if deltas else None

    @property
    def badge(self) -> str | None:
        return SIGNAL_BADGES.get(self.signal)


def rank_signal(plans: list[Plan], agricultural: bool) -> tuple[str, Plan | None]:
    """Сильнейший сигнал среди планов и план, который его дал.

    Учитываются только планы, меняющие назначение: сдвиг линии застройки на
    два метра к стоимости земли отношения не имеет. Для сельхозучастка
    дополнительно ценится план, который переводит землю именно из сельхоза, —
    при равной стадии он важнее.
    """
    best_signal = SIGNAL_NONE
    best_plan: Plan | None = None
    best_key = (-1, -1, -1)

    for plan in plans:
        if not plan.rezones:
            continue
        signal = _STAGE_TO_SIGNAL.get(plan.stage)
        if signal is None:
            continue
        from_agri = plan.rezones_from_agriculture and agricultural
        key = (_SIGNAL_ORDER[signal], int(from_agri), plan.units_delta or 0)
        if key > best_key:
            best_key, best_signal, best_plan = key, signal, plan

    return best_signal, best_plan


class Enricher:
    """Дополняет лоты кадастром и планами.

    Каждый лот стоит двух-трёх запросов к чужим сервисам, поэтому работа
    ограничена бюджетом: дневная сводка не должна превращаться в получасовой
    обход порталов.
    """

    def __init__(
        self,
        parcels: GovmapParcels,
        plans: IplanRegistry,
        budget: int = 40,
        only_agricultural: bool = False,
    ) -> None:
        self.parcels = parcels
        self.plans = plans
        self.budget = budget
        self.only_agricultural = only_agricultural
        self.used = 0

    def wants(self, lot: Lot) -> bool:
        """Стоит ли тратить бюджет на этот лот."""
        if self.used >= self.budget:
            return False
        if not (lot.gush and lot.chelka):
            return False
        if self.only_agricultural and lot.land_use != AGRICULTURE:
            return False
        return True

    def enrich(self, lot: Lot) -> Insight | None:
        """Один лот: кадастр, планы, сигнал. Отказ сервиса не фатален."""
        if not self.wants(lot):
            return None
        self.used += 1

        parcel = self.parcels.find(lot.gush, lot.chelka)
        if parcel is None:
            return None

        insight = Insight(parcel=parcel)
        if parcel.center is not None:
            x, y = parcel.center
            insight.plans = self.plans.plans_at(x, y)
            insight.land_uses = self.plans.land_use_at(x, y)

        insight.signal, insight.leading_plan = rank_signal(
            insight.plans, agricultural=lot.land_use == AGRICULTURE
        )
        return insight


def apply(lot: Lot, insight: Insight) -> Lot:
    """Переносит в лот то, чего тендер не сообщил.

    Площадь из кадастра ставится, только если тендер её не дал или дал
    очевидную бессмыслицу: у 21/2020 портал отдал один квадратный метр
    сельхозполя. Всё остальное лот не трогает — источник остаётся хозяином
    своих данных.
    """
    parcel = insight.parcel
    if parcel is None:
        return lot

    if parcel.area_sqm and (lot.area_sqm or 0) < MIN_CREDIBLE_AREA_SQM:
        lot.area_sqm = parcel.area_sqm
    if not lot.settlement and parcel.settlement:
        lot.settlement = parcel.settlement
    if not lot.region and parcel.region:
        lot.region = parcel.region
    return lot


#: Ниже этого участок земли — заведомо ошибка портала, а не участок.
MIN_CREDIBLE_AREA_SQM = 10.0
