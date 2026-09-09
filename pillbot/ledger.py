"""
pillbot.ledger.py - tamper-evident, idempotent dispense ledger.

Every dose decision (dispensed OR blocked) is appended as a record that binds
intent -> safety certificate -> hardware/sensor outcome, hash-chained to the
previous record (prev_hash + entry_hash, SHA-256). Editing, deleting, or
reordering any past record breaks the chain, which verify_chain() detects -
court/FDA-grade accountability.

Idempotency: each append carries a request_id (UNIQUE). Re-appending the same
request_id returns the existing entry instead of writing a duplicate, so a
replay after a crash between actuation and logging can never double-count a
dose (closes the ACK-vs-log crash window).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

GENESIS = "0" * 64


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, request_id: str, payload: dict, ts: str) -> str:
    body = _canonical({"request_id": request_id, "payload": payload, "ts": ts})
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


@dataclass
class LedgerEntry:
    seq: int
    request_id: str
    payload: dict
    prev_hash: str
    entry_hash: str
    ts: str


class HashChainedLedger:
    def __init__(self, db_path: str, table: str = "dispense_ledger",
                 clock: Optional[Callable[[], datetime.datetime]] = None):
        self.db_path = db_path
        self.table = table  # fixed, code-controlled identifier
        self._clock = clock or (lambda: datetime.datetime.now())
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.table} (
                        seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT UNIQUE NOT NULL,
                        payload    TEXT NOT NULL,
                        prev_hash  TEXT NOT NULL,
                        entry_hash TEXT NOT NULL,
                        ts         TEXT NOT NULL
                    )"""
            )

    def _row_to_entry(self, row: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(row["seq"], row["request_id"], json.loads(row["payload"]),
                           row["prev_hash"], row["entry_hash"], row["ts"])

    def get(self, request_id: str) -> Optional[LedgerEntry]:
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT * FROM {self.table} WHERE request_id=?", (request_id,)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def _last(self) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                f"SELECT * FROM {self.table} ORDER BY seq DESC LIMIT 1"
            ).fetchone()

    def append(self, request_id: str, payload: dict) -> LedgerEntry:
        """Idempotent append. Re-using a request_id returns the existing entry."""
        existing = self.get(request_id)
        if existing is not None:
            return existing

        last = self._last()
        prev_hash = last["entry_hash"] if last else GENESIS
        ts = self._clock().isoformat(timespec="seconds")
        entry_hash = compute_hash(prev_hash, request_id, payload, ts)

        with self._conn() as conn:
            try:
                cur = conn.execute(
                    f"""INSERT INTO {self.table}
                        (request_id, payload, prev_hash, entry_hash, ts)
                        VALUES (?,?,?,?,?)""",
                    (request_id, _canonical(payload), prev_hash, entry_hash, ts),
                )
                seq = cur.lastrowid
            except sqlite3.IntegrityError:
                # Lost a race on the same request_id - return the winner.
                return self.get(request_id)

        return LedgerEntry(seq, request_id, payload, prev_hash, entry_hash, ts)

    def entries(self) -> List[LedgerEntry]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT * FROM {self.table} ORDER BY seq ASC").fetchall()
        return [self._row_to_entry(r) for r in rows]

    def verify_chain(self) -> Tuple[bool, Optional[int]]:
        """Recompute the whole chain. Returns (ok, first_bad_seq)."""
        prev = GENESIS
        for e in self.entries():
            if e.prev_hash != prev:
                return False, e.seq
            if compute_hash(e.prev_hash, e.request_id, e.payload, e.ts) != e.entry_hash:
                return False, e.seq
            prev = e.entry_hash
        return True, None
