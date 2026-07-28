"""Хранилище на SQLite: история лотов и дедупликация уведомлений.

Ежедневный запуск обязан отличать «увидел впервые» от «видел вчера»,
иначе Telegram каждый день получал бы одну и ту же сводку. Состояние
живёт в одном файле — его можно положить в кеш CI или на диск сервера.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .models import Lot

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    uid                    TEXT PRIMARY KEY,
    source                 TEXT NOT NULL,
    source_id              TEXT NOT NULL,
    tender_id              TEXT,
    tender_name            TEXT,
    url                    TEXT,
    settlement             TEXT,
    neighborhood           TEXT,
    region                 TEXT,
    gush                   TEXT,
    chelka                 TEXT,
    purpose                TEXT,
    tender_type            TEXT,
    status                 TEXT,
    area_sqm               REAL,
    units                  INTEGER,
    units_basis            TEXT,
    price_nis              REAL,
    price_kind             TEXT,
    development_costs_nis  REAL,
    guarantee_nis          REAL,
    price_usd              REAL,
    fx_rate                REAL,
    fx_date                TEXT,
    price_per_unit_usd     REAL,
    price_per_sqm_usd      REAL,
    tier                   TEXT,
    published_date         TEXT,
    closing_date           TEXT,
    committee_date         TEXT,
    content_hash           TEXT NOT NULL,
    first_seen             TEXT NOT NULL,
    last_seen              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lots_tier ON lots(tier);
CREATE INDEX IF NOT EXISTS idx_lots_first_seen ON lots(first_seen);
CREATE INDEX IF NOT EXISTS idx_lots_source ON lots(source);

CREATE TABLE IF NOT EXISTS tender_cache (
    source       TEXT NOT NULL,
    tender_id    TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (source, tender_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL,
    stats_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    uid      TEXT NOT NULL,
    kind     TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (uid, kind)
);
"""

#: Поля, изменение которых интересно показать в сводке отдельной строкой.
TRACKED_CHANGES = ("price_usd", "price_nis", "units", "status", "closing_date", "tier")

_LOT_COLUMNS = (
    "uid", "source", "source_id", "tender_id", "tender_name", "url",
    "settlement", "neighborhood", "region", "gush", "chelka",
    "purpose", "tender_type", "status", "area_sqm", "units", "units_basis",
    "price_nis", "price_kind", "development_costs_nis", "guarantee_nis", "price_usd",
    "fx_rate", "fx_date", "price_per_unit_usd", "price_per_sqm_usd", "tier",
    "published_date", "closing_date", "committee_date",
    "content_hash", "first_seen", "last_seen",
)


class Storage:
    """Обёртка над SQLite. Используется как контекстный менеджер."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ------------------------------------------------------------ лоты ------

    def get_lot_row(self, uid: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM lots WHERE uid = ?", (uid,))
        return cur.fetchone()

    def upsert_lot(self, lot: Lot, now: str) -> tuple[str, dict[str, Any]]:
        """Пишет лот и возвращает ``(статус, изменения)``.

        Статус: ``new`` — впервые, ``changed`` — поменялись значимые поля,
        ``same`` — повтор вчерашнего.
        """
        content_hash = lot.content_hash()
        existing = self.get_lot_row(lot.uid)

        if existing is None:
            self._insert(lot, content_hash, first_seen=now, last_seen=now)
            return "new", {}

        if existing["content_hash"] == content_hash:
            self.conn.execute("UPDATE lots SET last_seen = ? WHERE uid = ?", (now, lot.uid))
            return "same", {}

        changes = self._diff(existing, lot)
        self._insert(lot, content_hash, first_seen=existing["first_seen"], last_seen=now)
        return "changed", changes

    @staticmethod
    def _diff(existing: sqlite3.Row, lot: Lot) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        new_values = lot.to_dict()
        for field in TRACKED_CHANGES:
            before = existing[field]
            after = new_values.get(field)
            if before != after:
                changes[field] = {"before": before, "after": after}
        return changes

    def _insert(self, lot: Lot, content_hash: str, first_seen: str, last_seen: str) -> None:
        values = lot.to_dict()
        values["uid"] = lot.uid
        values["content_hash"] = content_hash
        values["first_seen"] = first_seen
        values["last_seen"] = last_seen
        placeholders = ", ".join("?" for _ in _LOT_COLUMNS)
        self.conn.execute(
            f"INSERT OR REPLACE INTO lots ({', '.join(_LOT_COLUMNS)}) VALUES ({placeholders})",
            tuple(values.get(column) for column in _LOT_COLUMNS),
        )

    def commit(self) -> None:
        self.conn.commit()

    def iter_lots(self, tier: str | None = None, since: str | None = None) -> Iterable[sqlite3.Row]:
        query = "SELECT * FROM lots"
        conditions, params = [], []
        if tier:
            conditions.append("tier = ?")
            params.append(tier)
        if since:
            conditions.append("first_seen >= ?")
            params.append(since)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        # Портируемый аналог NULLS LAST — работает и на старых сборках SQLite.
        query += " ORDER BY price_usd IS NULL, price_usd DESC"
        with closing(self.conn.execute(query, params)) as cur:
            yield from cur.fetchall()

    def count_lots(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0])

    # ----------------------------------------------------- кеш тендеров -----

    def tender_changed(self, source: str, tender_id: str, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT fingerprint FROM tender_cache WHERE source = ? AND tender_id = ?",
            (source, tender_id),
        ).fetchone()
        return row is None or row["fingerprint"] != fingerprint

    def remember_tender(self, source: str, tender_id: str, fingerprint: str) -> None:
        from .money import utcnow_iso

        self.conn.execute(
            "INSERT OR REPLACE INTO tender_cache (source, tender_id, fingerprint, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (source, tender_id, fingerprint, utcnow_iso()),
        )

    # ------------------------------------------------------ уведомления -----

    def was_notified(self, uid: str, kind: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM notifications WHERE uid = ? AND kind = ?", (uid, kind)
        ).fetchone()
        return row is not None

    def mark_notified(self, uid: str, kind: str, now: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO notifications (uid, kind, sent_at) VALUES (?, ?, ?)",
            (uid, kind, now),
        )

    # ------------------------------------------------------------ запуски ---

    def record_run(self, started_at: str, finished_at: str, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO runs (started_at, finished_at, stats_json) VALUES (?, ?, ?)",
            (started_at, finished_at, json.dumps(stats, ensure_ascii=False, default=str)),
        )
        self.conn.commit()

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
