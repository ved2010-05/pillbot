"""
pillbot.store.py - durable (SQLite) persistence for magazine tag assignments.

Implements the MagazineStore protocol from pillbot.magazines using its own
SQLite connection, so a magazine's tag -> medicine assignment (and its
last-known count, lot, expiry) survives restarts and power cycles. One row per
tag; the payload is JSON so the schema can evolve without migrations here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional


class SqliteMagazineStore:
    def __init__(self, db_path: str, table: str = "magazines"):
        self.db_path = db_path
        self.table = table  # fixed, code-controlled identifier (not user input)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.table} (
                        tag_id     TEXT PRIMARY KEY,
                        data       TEXT NOT NULL,
                        updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
                    )"""
            )

    def load(self, tag_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT data FROM {self.table} WHERE tag_id=?", (tag_id,)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def save(self, tag_id: str, data: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                f"""INSERT INTO {self.table} (tag_id, data) VALUES (?, ?)
                    ON CONFLICT(tag_id) DO UPDATE SET
                        data=excluded.data,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%S','now')""",
                (tag_id, json.dumps(data)),
            )

    def all_tags(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT tag_id FROM {self.table}").fetchall()
        return [r["tag_id"] for r in rows]
