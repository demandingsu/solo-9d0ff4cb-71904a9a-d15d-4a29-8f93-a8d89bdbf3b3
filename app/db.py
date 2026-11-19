"""SQLite connection handling and schema management.

Concurrency model
-----------------
SQLite in WAL mode allows multiple readers concurrently with one writer.
Every state-changing request runs inside an explicit ``BEGIN IMMEDIATE``
transaction, which acquires the reserved (write) lock up front. Combined
with the busy timeout, uploads and withdrawals are serialized against one
another and each sees a fully committed state. Reads use their own
snapshot transaction, so a query never observes a half-written path:
paths are *derived* from committed event rows at read time, there is no
separate path table that could be updated partially.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT    NOT NULL PRIMARY KEY,
    visitor_id    TEXT    NOT NULL,
    page_view_id  TEXT    NOT NULL,
    exhibit_id    TEXT    NOT NULL,
    event_type    TEXT    NOT NULL CHECK (event_type IN ('open', 'close')),
    event_time    TEXT    NOT NULL,
    received_at   TEXT    NOT NULL
);

-- Hot lookup paths: pairing by page view, path ordering by open time,
-- and per-visitor scans / consent checks.
CREATE INDEX IF NOT EXISTS idx_events_visitor_time
    ON events (visitor_id, event_time);
CREATE INDEX IF NOT EXISTS idx_events_visitor_page
    ON events (visitor_id, page_view_id);

CREATE TABLE IF NOT EXISTS withdrawals (
    visitor_id  TEXT    NOT NULL PRIMARY KEY,
    withdrawn_at TEXT   NOT NULL
);

-- Deep-zoom image revisions. One row per uploaded original; the manifest is
-- only ever visible to clients when status = 'ready' and every tile file has
-- been verified on disk. A revision stays 'building' while its tiles are being
-- written, and becomes 'failed' if the build breaks or a restart interrupts it.
CREATE TABLE IF NOT EXISTS image_revisions (
    exhibit_id     TEXT    NOT NULL,
    revision       INTEGER NOT NULL,
    source_format  TEXT    NOT NULL CHECK (source_format IN ('png', 'jpeg')),
    width          INTEGER NOT NULL CHECK (width > 0),
    height         INTEGER NOT NULL CHECK (height > 0),
    max_level      INTEGER NOT NULL CHECK (max_level >= 0),
    manifest       TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    error_message  TEXT,
    created_at     TEXT    NOT NULL,
    -- Process/worker that owns this row while it is 'building'. Recovery
    -- only reaps a build when its owner's heartbeat has gone stale, so a
    -- second booting worker can never kill another worker's live build.
    owner_id       TEXT,
    PRIMARY KEY (exhibit_id, revision)
);

-- Liveness of build owners (one row per running process). Refreshed on a
-- short interval; rows older than the staleness threshold mark a dead owner.
CREATE TABLE IF NOT EXISTS build_owners (
    owner_id    TEXT    NOT NULL PRIMARY KEY,
    last_beat   TEXT    NOT NULL
);

-- One row per exhibit; active_revision points at the currently published,
-- fully readable revision. NULL means nothing has been published yet. The
-- pointer only moves inside the same transaction that marks a revision ready,
-- so an in-flight or failed build can never be observed as active.
CREATE TABLE IF NOT EXISTS exhibits (
    exhibit_id      TEXT    NOT NULL PRIMARY KEY,
    active_revision INTEGER,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_exhibit_status
    ON image_revisions (exhibit_id, status);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection with the pragmas required for safe concurrency."""
    conn = sqlite3.connect(
        db_path or config.DB_PATH,
        timeout=config.BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # manual transaction control
        # Sync FastAPI endpoints run in uvicorn's worker threadpool, so the
        # connection created by the dependency may be touched from a pool
        # thread. Each request gets its own connection (never shared), which
        # makes cross-thread use safe; SQLite file locking + BEGIN IMMEDIATE
        # still serialize the actual writers.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create tables/indexes if missing and ensure the tile root exists."""
    os.makedirs(config.TILE_ROOT, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_connection() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, closed afterwards.

    FastAPI consumes this generator directly: the yielded connection is
    injected into the endpoint, and the ``finally`` block runs when the
    request finishes. Do not wrap it in ``@contextmanager`` — doing so hides
    the generator signature from FastAPI's dependency analysis.
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Acquire the write lock immediately and commit/rollback atomically."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
