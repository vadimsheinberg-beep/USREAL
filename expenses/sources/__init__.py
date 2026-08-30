"""Источники транзакций: файлы выписок, биржа Bybit и произвольный REST API."""

from __future__ import annotations

from .base import FieldMap, normalize_records
from .bybit import BybitClient, BybitConfig, BybitError, fetch_bybit
from .files import load_csv, load_file, load_json
from .rest import RestClient, RestConfig, RestError, fetch_rest

__all__ = [
    "BybitClient",
    "BybitConfig",
    "BybitError",
    "FieldMap",
    "RestClient",
    "RestConfig",
    "RestError",
    "fetch_bybit",
    "fetch_rest",
    "load_csv",
    "load_file",
    "load_json",
    "normalize_records",
]
