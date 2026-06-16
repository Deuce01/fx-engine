"""SQLite persistence layer with ACID transaction support.

Uses WAL mode for concurrent read access and IMMEDIATE transactions
for serialized writes. All monetary values stored as TEXT to prevent
silent numeric coercion by the database engine.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "fx.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balances (
    customer_id TEXT NOT NULL REFERENCES customers(id),
    currency TEXT NOT NULL CHECK(currency IN ('USD','EUR','KES','NGN')),
    amount TEXT NOT NULL DEFAULT '0.00',
    PRIMARY KEY (customer_id, currency)
);

CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    from_currency TEXT NOT NULL,
    to_currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    rate TEXT NOT NULL,
    final_amount TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    executed INTEGER NOT NULL DEFAULT 0,
    executed_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL UNIQUE REFERENCES quotes(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    from_currency TEXT NOT NULL,
    to_currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    final_amount TEXT NOT NULL,
    rate TEXT NOT NULL,
    executed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db(db_path: Path | None = None) -> None:
    """Initialize database schema. Enables WAL mode and foreign keys."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.close()
    logger.info("database initialized at %s", path)


def reset_db(db_path: Path | None = None) -> None:
    """Drop and recreate the database. Used by tests."""
    path = db_path or DB_PATH
    if path.exists():
        path.unlink()
    init_db(path)


@contextmanager
def get_connection(
    db_path: Path | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Yield a database connection with explicit IMMEDIATE transaction.

    Uses BEGIN IMMEDIATE to serialize writes at the SQLite level,
    preventing TOCTOU race conditions without relying on application-
    level locks (SPEC.md §7).

    On success the transaction is committed; on any exception it is
    rolled back before re-raising.
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.isolation_level = None  # Manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def get_read_connection(
    db_path: Path | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Yield a read-only database connection (no write-transaction overhead)."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()
