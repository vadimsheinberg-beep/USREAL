"""Базовый интерфейс источника данных."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Protocol

from ..http import HttpClient
from ..models import Lot

log = logging.getLogger(__name__)


class TenderCache(Protocol):
    """Минимум, который источнику нужен от хранилища, чтобы не качать лишнее."""

    def tender_changed(self, source: str, tender_id: str, fingerprint: str) -> bool: ...

    def remember_tender(self, source: str, tender_id: str, fingerprint: str) -> None: ...


@dataclass
class SourceContext:
    """Всё, что источник получает снаружи."""

    http: HttpClient
    options: dict[str, Any]
    lookback_days: int = 30
    cache: TenderCache | None = None
    full_refresh: bool = False

    @property
    def since(self) -> date:
        return date.today() - timedelta(days=self.lookback_days)

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)


class Source(ABC):
    """Источник тендеров/объявлений по земле."""

    #: Машинное имя, оно же ключ в ``[sources.*]`` конфига.
    name: str = ""
    #: Человекочитаемое название для отчётов.
    title: str = ""
    #: Государственный или частный.
    kind: str = "government"

    def __init__(self, ctx: SourceContext) -> None:
        self.ctx = ctx

    @abstractmethod
    def fetch(self) -> Iterable[Lot]:
        """Отдаёт нормализованные лоты. Может бросать :class:`HttpError`."""

    def probe(self) -> str:
        """Быстрая проверка доступности для команды ``landtender check``."""
        lots = list(self.fetch())
        return f"получено лотов: {len(lots)}"
