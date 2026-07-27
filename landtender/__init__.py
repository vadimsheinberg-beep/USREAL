"""landtender — ежедневный трекер земельных тендеров Израиля.

Собирает лоты с государственных порталов (рм"י, data.gov.il, mr.gov.il) и
частной площадки Yad2, считает стоимость земли в долларах и количество
единиц строений, делит выдачу по порогу в 1 000 000 $ и шлёт дневную
сводку в Telegram.
"""

from .models import Lot, RunResult, SourceReport

__version__ = "0.1.0"

__all__ = ["Lot", "RunResult", "SourceReport", "__version__"]
