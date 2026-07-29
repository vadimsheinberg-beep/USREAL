"""Загрузка конфигурации из TOML + переменных окружения."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_NAMES = ("landtender.toml", "config.toml")
ENV_FILE_NAME = ".env"

#: Значения по умолчанию. Файл конфигурации накладывается поверх них.
DEFAULTS: dict[str, Any] = {
    "general": {
        "threshold_usd": 1_000_000.0,
        "hide_expired": True,
        # Пустой список — города не ограничены. Нужные города задаются
        # в конфиге; в умолчаниях фильтра нет, иначе он включён незаметно.
        "settlements": [],
        "lookback_days": 30,
        "db_path": "data/landtender.sqlite3",
        "user_agent": "landtender/0.1 (+https://github.com/vadimsheinberg-beep/usreal)",
        "request_timeout": 45,
        "rate_limit_delay": 1.0,
    },
    "fx": {
        # boi = Банк Израиля (официальный представительный курс), static = из конфига
        "provider": "boi",
        "static_usd_ils": 3.70,
        "cache_hours": 12,
    },
    "valuation": {
        # Какую цену считать «стоимостью земли», по убыванию приоритета
        "price_preference": ["final", "min", "appraisal", "asking"],
        # Прибавлять ли расходы на развитие (הוצאות פיתוח) к стоимости земли
        "include_development_costs": False,
        # Считать ли лоты без цены — их нельзя отнести ни к одной группе
        "keep_priceless": True,
    },
    "sources": {
        "rmi_michrazim": {
            "enabled": True,
            "details_budget": 500,
            "active_only": True,
            "settlements": [],
        },
        "urban_renewal": {
            "enabled": True,
            "max_rows": 2000,
            "queries": ["התחדשות עירונית", "פינוי בינוי", "מתחמי התחדשות עירונית"],
        },
        "data_gov_il": {"enabled": True, "max_rows": 2000, "queries": ["מכרזי מקרקעין", "מכרזים רשות מקרקעי ישראל"]},
        "gov_mr": {"enabled": True, "max_pages": 5},
        "yad2": {"enabled": True, "max_pages": 5, "property_types": [39]},
    },
    "telegram": {
        "enabled": True,
        "bot_token": "env:TELEGRAM_BOT_TOKEN",
        "chat_id": "env:TELEGRAM_CHAT_ID",
        "send_standard_tier": True,
        "max_lots_per_tier": 25,
        "notify_changes": True,
        # Прикладывать к сводке CSV с новыми лотами отдельным файлом
        "attach_csv": True,
    },
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
    """Разбирает содержимое ``.env``: ``КЛЮЧ=значение``, по паре на строку.

    Понимает комментарии, префикс ``export`` и кавычки вокруг значения.
    """
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


def load_env_file(directory: Path) -> dict[str, str]:
    """Подмешивает ``.env`` из каталога в окружение процесса.

    Уже заданные переменные окружения приоритетнее файла: в CI секреты
    приходят из хранилища секретов, и файл не должен их перебивать.
    """
    path = directory / ENV_FILE_NAME
    if not path.exists():
        return {}
    try:
        values = parse_env_file(path.read_text("utf-8"))
    except OSError:
        return {}
    applied = {}
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def resolve_secret(value: Any) -> Any:
    """``"env:NAME"`` превращает в значение переменной окружения ``NAME``.

    Это позволяет держать конфиг в git, а токены — в окружении.
    """
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:].strip()) or None
    return value


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: _deep_merge(DEFAULTS, {}))
    path: Path | None = None

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return resolve_secret(self.section(section).get(key, default))

    def source_config(self, name: str) -> dict[str, Any]:
        sources = self.section("sources")
        cfg = sources.get(name, {})
        return cfg if isinstance(cfg, dict) else {}

    def source_enabled(self, name: str) -> bool:
        return bool(self.source_config(name).get("enabled", False))

    @property
    def threshold_usd(self) -> float:
        return float(self.get("general", "threshold_usd", 1_000_000.0))

    @property
    def db_path(self) -> Path:
        raw = str(self.get("general", "db_path", "data/landtender.sqlite3"))
        path = Path(raw)
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
    """Читает конфиг; при отсутствии файла возвращает значения по умолчанию.

    Заодно подхватывает ``.env`` рядом с конфигом (или в текущем каталоге) —
    чтобы токен бота можно было положить в файл, а не экспортировать руками.
    """
    path = find_config(explicit)
    load_env_file(path.parent if path else Path.cwd())
    if path is None:
        return Config()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return Config(data=_deep_merge(DEFAULTS, raw), path=path)
