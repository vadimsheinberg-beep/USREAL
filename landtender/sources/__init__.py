"""Реестр источников.

Добавить новый государственный портал = написать класс-наследник
:class:`~landtender.sources.base.Source`, зарегистрировать его здесь и
включить в конфиге. Остальной конвейер трогать не нужно.
"""

from __future__ import annotations

from .base import Source, SourceContext
from .data_gov_il import DataGovIlSource
from .gov_mr import GovMrSource
from .mechir_lamishtaken import MechirLamishtakenSource
from .rmi_michrazim import RmiMichrazimSource
from .urban_renewal import UrbanRenewalSource
from .yad2 import Yad2Source

#: Порядок важен: сначала самый авторитетный источник по земле.
ALL_SOURCES: tuple[type[Source], ...] = (
    RmiMichrazimSource,
    UrbanRenewalSource,
    DataGovIlSource,
    MechirLamishtakenSource,
    GovMrSource,
    Yad2Source,
)

SOURCES_BY_NAME: dict[str, type[Source]] = {cls.name: cls for cls in ALL_SOURCES}

__all__ = [
    "ALL_SOURCES",
    "MechirLamishtakenSource",
    "SOURCES_BY_NAME",
    "Source",
    "SourceContext",
    "DataGovIlSource",
    "GovMrSource",
    "RmiMichrazimSource",
    "UrbanRenewalSource",
    "Yad2Source",
]
