"""Нормализованные модели данных.

Каждый источник (рм"י, data.gov.il, mr.gov.il, Yad2) приводит свои записи
к одному виду — :class:`Lot`. Лот — это отдельный участок/миграш внутри
тендера; у одного тендера их может быть много.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

TIER_PREMIUM = "premium"
TIER_STANDARD = "standard"
TIER_UNKNOWN = "unknown"

#: Откуда взята цена участка. Порядок = приоритет по умолчанию.
PRICE_KIND_FINAL = "final"  # цена победившей заявки (תוצאות מכרז)
PRICE_KIND_MIN = "min"  # минимальная цена / מחיר מינימום
PRICE_KIND_APPRAISAL = "appraisal"  # оценка шамая / שומה
PRICE_KIND_ASKING = "asking"  # запрашиваемая цена (частные площадки)

#: Как получено количество единиц строений.
UNITS_REPORTED = "reported"  # пришло из источника (YechidotDiur и т.п.)
UNITS_INFERRED = "inferred"  # выведено эвристикой (см. units.py)


@dataclass
class Lot:
    """Один участок земли, выставленный на торги или в продажу."""

    source: str
    source_id: str

    tender_id: str | None = None
    tender_name: str | None = None
    url: str | None = None

    # География
    settlement: str | None = None
    neighborhood: str | None = None
    region: str | None = None
    gush: str | None = None
    chelka: str | None = None

    # Классификация
    purpose: str | None = None
    tender_type: str | None = None
    status: str | None = None

    # Физика участка
    area_sqm: float | None = None
    #: Площадь существующей застройки — признак того, что участок не пустой.
    built_area_sqm: float | None = None
    #: Вид работ: снос, усиление, расселение (см. renewal.py). None — пустой участок.
    renewal_kind: str | None = None
    #: Есть ли на участке строение. None — неизвестно.
    has_structure: bool | None = None
    #: Назначение земли: сельхоз, жильё, промышленность… (см. landuse.py).
    land_use: str | None = None
    #: Смена назначения по реестру планов (см. invest.py): none / early /
    #: likely / confirmed. Именно она превращает дешёвую землю в дорогую.
    plan_signal: str | None = None
    #: Номер и ссылка плана, давшего сигнал, — чтобы можно было проверить.
    plan_number: str | None = None
    plan_url: str | None = None
    #: Назначение по действующему плану («קרקע חקלאית», «מגורים א'»).
    zoning: str | None = None
    #: Оценка по сделкам с соседними участками (см. valuation.py).
    estimate_nis: float | None = None
    estimate_low_nis: float | None = None
    estimate_high_nis: float | None = None
    #: На скольких сделках построена оценка и насколько модель объясняет
    #: разброс. Без этих двух чисел саму оценку показывать нельзя.
    estimate_n: int | None = None
    estimate_r2: float | None = None
    estimate_method: str | None = None
    units: int | None = None
    units_basis: str | None = None

    # Деньги (в шекелях — как отдают источники)
    price_nis: float | None = None
    price_kind: str | None = None
    development_costs_nis: float | None = None
    #: Банковская гарантия, которую требует тендер (не менее 10% от заявки).
    guarantee_nis: float | None = None

    # Деньги (пересчёт в доллары — заполняет money.py)
    price_usd: float | None = None
    fx_rate: float | None = None
    fx_date: str | None = None
    price_per_unit_usd: float | None = None
    price_per_sqm_usd: float | None = None
    tier: str = TIER_UNKNOWN

    # Даты (ISO-строки, чтобы одинаково жить в SQLite и в JSON)
    published_date: str | None = None
    #: Когда открывается приём заявок. Раньше этой даты цены у тендера нет.
    opening_date: str | None = None
    closing_date: str | None = None
    committee_date: str | None = None

    # Сырьё источника — для отладки и восстановления полей задним числом
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        """Стабильный ключ записи между запусками."""
        return f"{self.source}:{self.source_id}"

    @property
    def label(self) -> str:
        """Человекочитаемое имя лота для отчёта."""
        bits = [b for b in (self.settlement, self.neighborhood) if b]
        where = ", ".join(bits) if bits else "—"
        name = self.tender_name or self.tender_id or self.source_id
        return f"{name} · {where}"

    def content_hash(self) -> str:
        """Хеш значимых полей: по нему ловим изменения лота между запусками."""
        significant = (
            self.tender_name,
            self.settlement,
            self.neighborhood,
            self.purpose,
            self.status,
            self.area_sqm,
            self.units,
            self.plan_signal,
            self.price_nis,
            self.price_kind,
            self.development_costs_nis,
            self.published_date,
            self.opening_date,
            self.closing_date,
            self.committee_date,
        )
        blob = json.dumps(significant, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["uid"] = self.uid
        if not include_raw:
            data.pop("raw", None)
        return data


@dataclass
class SourceReport:
    """Итог работы одного источника за запуск."""

    name: str
    ok: bool
    lots: int = 0
    error: str | None = None
    duration_sec: float = 0.0
    note: str | None = None
    #: Сколько лотов отброшено как просроченные (срок подачи прошёл).
    skipped_expired: int = 0
    #: Сколько лотов отброшено как относящиеся к другим городам.
    skipped_elsewhere: int = 0


@dataclass
class RunResult:
    """Итог всего ежедневного запуска."""

    started_at: str
    finished_at: str
    sources: list[SourceReport] = field(default_factory=list)
    new_lots: list[Lot] = field(default_factory=list)
    changed_lots: list[tuple[Lot, dict[str, Any]]] = field(default_factory=list)
    total_seen: int = 0
    fx_rate: float | None = None
    fx_date: str | None = None
    fx_source: str | None = None

    @property
    def ok(self) -> bool:
        return any(s.ok for s in self.sources)

    def stats(self) -> dict[str, Any]:
        premium = [lot for lot in self.new_lots if lot.tier == TIER_PREMIUM]
        standard = [lot for lot in self.new_lots if lot.tier == TIER_STANDARD]
        return {
            "total_seen": self.total_seen,
            "new": len(self.new_lots),
            "changed": len(self.changed_lots),
            "new_premium": len(premium),
            "new_standard": len(standard),
            "fx_rate": self.fx_rate,
            "fx_source": self.fx_source,
            "sources": {s.name: {"ok": s.ok, "lots": s.lots, "error": s.error} for s in self.sources},
        }
