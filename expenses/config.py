"""Конфигурация анализатора: TOML + переменные окружения.

Пакет ``expenses`` намеренно не зависит от остального репозитория —
его можно скопировать в отдельный проект целиком, поэтому небольшой
разбор ``.env`` живёт здесь, а не переиспользуется из соседнего пакета.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CONFIG_NAMES = ("expenses.toml", "expenses.local.toml")
ENV_FILE_NAME = ".env"

DEFAULTS: dict[str, Any] = {
    "general": {
        #: Валюта отчёта. Операции в других валютах пересчитываются по [fx].
        "currency": "ILS",
        #: Куда складывать импортированные операции.
        "data_path": "data/expenses.jsonl",
        #: Глубина отчёта по умолчанию, в месяцах.
        "months": 6,
        #: Сколько строк показывать в топах.
        "top": 10,
    },
    "categories": {
        #: Использовать встроенный набор правил (иврит + латиница + русский).
        "use_defaults": True,
        #: Брать категорию из источника, если ни одно правило не сработало.
        "trust_source_category": False,
        #: Регулярное списание: минимум месяцев подряд и допустимый разброс сумм.
        "recurring_min_months": 3,
        "recurring_tolerance": 0.25,
    },
    "fx": {
        #: Фиксированные курсы «сколько единиц валюты отчёта за 1 единицу».
        #: Пример: rates = { USD = 3.7, EUR = 4.0 } при currency = "ILS".
        "rates": {},
    },
    "sources": {
        "babit": {
            "enabled": False,
            "base_url": "",
            "transactions_path": "/transactions",
            "auth": "bearer",
            "token_env": "BABIT_API_TOKEN",
            "pagination": "page",
            "page_size": 200,
            "fields": {},
        }
    },
    #: Пользовательские правила категоризации, приоритет выше встроенных.
    "rules": [],
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def parse_env_file(text: str) -> dict[str, str]:
    """Разбирает ``.env``: ``КЛЮЧ=значение``, комментарии, ``export``, кавычки."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(directory: Path) -> None:
    """Подмешивает ``.env`` в окружение, не перебивая уже заданные переменные."""
    path = directory / ENV_FILE_NAME
    if not path.exists():
        return
    try:
        values = parse_env_file(path.read_text("utf-8"))
    except OSError as exc:
        log.warning("не прочитал %s: %s", path, exc)
        return
    for key, value in values.items():
        os.environ.setdefault(key, value)


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: _deep_merge(DEFAULTS, {}))
    path: Path | None = None

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    def source_config(self, name: str) -> dict[str, Any]:
        cfg = self.section("sources").get(name, {})
        return cfg if isinstance(cfg, dict) else {}

    def source_enabled(self, name: str) -> bool:
        return bool(self.source_config(name).get("enabled", False))

    @property
    def currency(self) -> str:
        return str(self.get("general", "currency", "ILS")).upper()

    @property
    def rules(self) -> list[dict[str, Any]]:
        raw = self.data.get("rules", [])
        return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []

    @property
    def fx_rates(self) -> dict[str, float]:
        raw = self.section("fx").get("rates", {})
        if not isinstance(raw, dict):
            return {}
        rates: dict[str, float] = {}
        for code, value in raw.items():
            try:
                rates[str(code).upper()] = float(value)
            except (TypeError, ValueError):
                log.warning("плохой курс для %s: %r", code, value)
        return rates

    @property
    def data_path(self) -> Path:
        """Путь к хранилищу; относительный — считается от файла конфига."""
        raw = str(self.get("general", "data_path", "data/expenses.jsonl"))
        path = Path(raw).expanduser()
        if not path.is_absolute() and self.path is not None:
            path = self.path.parent / path
        return path


def find_config(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise FileNotFoundError(f"Файл конфигурации не найден: {path}")
        return path
    for name in DEFAULT_CONFIG_NAMES:
        candidate = Path.cwd() / name
        if candidate.exists():
            return candidate
    return None


def load_config(explicit: str | os.PathLike[str] | None = None) -> Config:
    """Читает конфиг; без файла возвращает значения по умолчанию."""
    path = find_config(explicit)
    load_env_file(path.parent if path else Path.cwd())
    if path is None:
        return Config()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return Config(data=_deep_merge(DEFAULTS, raw), path=path)
