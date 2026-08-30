"""Локальное хранилище операций — JSONL, одна транзакция на строку.

Формат выбран специально простой: файл читается глазами, кладётся в git
(если не жалко) и не требует ни сервера, ни миграций. Дедупликация — по
:attr:`Transaction.key`, поэтому повторный импорт той же выписки или
пересекающийся период выгрузки из API не удваивают расходы.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from .models import Transaction

log = logging.getLogger(__name__)


class Store:
    """Набор операций на диске."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._items: dict[str, Transaction] = {}
        self._loaded = False

    def load(self) -> list[Transaction]:
        """Читает файл. Битые строки пропускает, чтобы не терять остальное."""
        if self._loaded:
            return self.transactions
        self._items = {}
        if self.path.exists():
            broken = 0
            for line_no, line in enumerate(self.path.read_text("utf-8").splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    tx = Transaction.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    broken += 1
                    log.warning("строка %d в %s не разобралась", line_no, self.path)
                    continue
                self._items[tx.key] = tx
            if broken:
                log.warning("пропущено битых строк: %d", broken)
        self._loaded = True
        return self.transactions

    @property
    def transactions(self) -> list[Transaction]:
        """Все операции, от старых к новым."""
        return sorted(self._items.values(), key=lambda t: (t.date, t.description))

    def add(self, transactions: Iterable[Transaction]) -> tuple[int, int]:
        """Добавляет операции. Возвращает ``(новых, дубликатов)``."""
        self.load()
        added = duplicates = 0
        for tx in transactions:
            if tx.key in self._items:
                duplicates += 1
                continue
            self._items[tx.key] = tx
            added += 1
        return added, duplicates

    def replace(self, transactions: Iterable[Transaction]) -> None:
        """Полностью перезаписывает набор — нужно после рекатегоризации."""
        self._items = {tx.key: tx for tx in transactions}
        self._loaded = True

    def save(self) -> int:
        """Пишет файл целиком. Возвращает число сохранённых операций."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(tx.to_dict(), ensure_ascii=False) for tx in self.transactions
        ]
        #: Пишем через временный файл: обрыв на середине не должен убить историю.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp.replace(self.path)
        return len(lines)

    def __len__(self) -> int:
        self.load()
        return len(self._items)
