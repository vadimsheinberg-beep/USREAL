"""Импорт выписок из файлов: CSV, TSV, JSON, JSONL.

Полезно и само по себе (банк отдаёт выгрузку файлом), и как резервный
путь: если API временно недоступен, отчёт всё равно можно построить.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any, Sequence

from ..models import Transaction
from .base import FieldMap, SourceError, extract_records, normalize_records

log = logging.getLogger(__name__)

#: Как называются нужные колонки в выписках, которые попадаются на практике.
_HEADER_HINTS: dict[str, tuple[str, ...]] = {
    "date": ("date", "תאריך", "дата", "transaction date", "date_time", "datetime", "value date", "תאריך עסקה"),
    "amount": ("amount", "sum", "סכום", "сумма", "debit", "חיוב", "total", "amount_ils", "סכום חיוב"),
    "description": ("description", "details", "merchant", "business", "שם בית עסק", "תיאור", "описание", "name", "פרטים", "назначение"),
    "currency": ("currency", "מטבע", "валюта", "curr"),
    "source_id": ("id", "transaction_id", "reference", "אסמכתא", "номер"),
    "category": ("category", "קטגוריה", "категория"),
    "account": ("account", "card", "חשבון", "כרטיס", "счет", "счёт"),
}


def guess_mapping(headers: Sequence[str]) -> FieldMap:
    """Угадывает соответствие колонок по заголовкам файла.

    Сравнение нестрогое: заголовок подходит, если подсказка встречается
    в нём как подстрока. Обязательные поля — дата, сумма, описание; если
    их не видно, лучше упасть сразу, чем импортировать мусор.
    """
    lowered = {h: (h or "").strip().lower() for h in headers}
    guessed: dict[str, Any] = {}
    for field_name, hints in _HEADER_HINTS.items():
        for header, low in lowered.items():
            if not low:
                continue
            if any(hint in low for hint in hints):
                guessed[field_name] = header
                break

    missing = [f for f in ("date", "amount", "description") if f not in guessed]
    if missing:
        raise SourceError(
            "не понял колонки "
            + ", ".join(missing)
            + f" (заголовки: {', '.join(h for h in headers if h)}). "
            "Задайте маппинг вручную в expenses.toml"
        )
    log.info("колонки распознаны: %s", guessed)
    return FieldMap(**guessed)


def load_csv(
    path: str | Path,
    mapping: FieldMap | None = None,
    *,
    source: str = "csv",
    encoding: str = "utf-8-sig",
) -> list[Transaction]:
    """Читает CSV/TSV. Разделитель определяется автоматически."""
    path = Path(path)
    text = path.read_text(encoding=encoding, errors="replace")
    if not text.strip():
        return []

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
        log.debug("разделитель не определился, беру запятую")

    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    rows = [row for row in reader]
    if not rows:
        return []
    if mapping is None:
        mapping = guess_mapping(reader.fieldnames or [])
    return normalize_records(rows, mapping, source)


def load_json(
    path: str | Path,
    mapping: FieldMap | None = None,
    *,
    source: str = "json",
    encoding: str = "utf-8",
) -> list[Transaction]:
    """Читает JSON (объект или массив) либо JSONL — по одной записи на строку."""
    path = Path(path)
    text = path.read_text(encoding=encoding)
    mapping = mapping or FieldMap()

    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = _load_jsonl(text)
    else:
        payload = _load_jsonl(text)

    records = extract_records(payload, mapping.records_path)
    return normalize_records(records, mapping, source)


def _load_jsonl(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            log.warning("строка %d не разобралась как JSON, пропускаю", line_no)
            continue
        if isinstance(item, dict):
            records.append(item)
    if not records:
        raise SourceError("файл не похож ни на JSON, ни на JSONL")
    return records


def load_file(
    path: str | Path, mapping: FieldMap | None = None, *, source: str | None = None
) -> list[Transaction]:
    """Выбирает парсер по расширению файла."""
    path = Path(path)
    if not path.exists():
        raise SourceError(f"файл не найден: {path}")
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv", ".txt"}:
        return load_csv(path, mapping, source=source or "csv")
    if suffix in {".json", ".jsonl", ".ndjson"}:
        return load_json(path, mapping, source=source or "json")
    raise SourceError(f"не знаю, как читать {suffix or 'файл без расширения'}")
