"""Database initialisation and shared schema for selectors + crawl state."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

DATA_DIR = Path.home() / ".anansi"

_SELECTOR_SCHEMA = """
-- busy_timeout MUST be first: concurrent connections opening the same DB (e.g.
-- AdaptiveParser.extract() fanning out fields via asyncio.gather) otherwise
-- race on the WAL journal_mode switch and immediately hit "database is locked".
-- A busy handler makes contenders wait instead of erroring.
PRAGMA busy_timeout=5000;
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS selectors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url_pattern   TEXT    NOT NULL,
    field_name    TEXT    NOT NULL,
    selector      TEXT    NOT NULL,
    selector_type TEXT    NOT NULL DEFAULT 'css',
    confidence    REAL    NOT NULL DEFAULT 1.0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_used     TEXT,
    created_at    TEXT    DEFAULT (datetime('now')),
    UNIQUE(url_pattern, field_name, selector)
);

CREATE INDEX IF NOT EXISTS idx_sel_lookup
    ON selectors(url_pattern, field_name, confidence DESC);
"""

_CRAWL_SCHEMA = """
-- See _SELECTOR_SCHEMA: busy_timeout first so concurrent connections wait on
-- the WAL switch instead of raising "database is locked".
PRAGMA busy_timeout=5000;
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS crawls (
    crawl_id    TEXT PRIMARY KEY,
    spider_name TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    settings    TEXT,
    items_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS url_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_id    TEXT    NOT NULL REFERENCES crawls(crawl_id),
    url         TEXT    NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'pending',
    callback    TEXT    DEFAULT 'parse',
    meta        TEXT    DEFAULT '{}',
    created_at  TEXT    DEFAULT (datetime('now')),
    UNIQUE(crawl_id, url)
);

CREATE INDEX IF NOT EXISTS idx_queue_pop
    ON url_queue(crawl_id, status, priority DESC, id ASC);

CREATE TABLE IF NOT EXISTS visited_urls (
    crawl_id     TEXT NOT NULL REFERENCES crawls(crawl_id),
    url          TEXT NOT NULL,
    content_hash TEXT,
    PRIMARY KEY (crawl_id, url)
);

CREATE INDEX IF NOT EXISTS idx_visited_hash
    ON visited_urls(crawl_id, content_hash);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_id    TEXT    NOT NULL REFERENCES crawls(crawl_id),
    source_url  TEXT,
    spider_name TEXT,
    data        TEXT    NOT NULL,
    created_at  TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_crawl ON items(crawl_id, id);

CREATE TABLE IF NOT EXISTS url_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    content_hash  TEXT,
    last_fetched  REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_url_cache_fetched ON url_cache(last_fetched);
"""


async def _init_schema(db: aiosqlite.Connection, schema: str) -> None:
    """Run a schema script, retrying on transient SQLITE_BUSY.

    Switching a fresh database into WAL journal mode needs a brief exclusive
    lock, and SQLite returns SQLITE_BUSY *immediately* for a ``journal_mode``
    change regardless of ``busy_timeout`` — the busy handler is not consulted
    for that operation. When two connections open the same DB concurrently
    (e.g. ``AdaptiveParser.extract()`` fanning out fields), one loses that race.
    A bounded retry lets it re-run once the other connection's switch completes.
    """
    last_exc: Exception | None = None
    for attempt in range(12):
        try:
            await db.executescript(schema)
            return
        except Exception as exc:  # noqa: BLE001 - only retry the lock case
            if "locked" not in str(exc).lower():
                raise
            last_exc = exc
            await asyncio.sleep(0.02 * (attempt + 1))
    if last_exc is not None:
        raise last_exc


@asynccontextmanager
async def selector_db(path: Path | None = None) -> AsyncIterator[aiosqlite.Connection]:
    db_path = path or DATA_DIR / "selectors.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await _init_schema(db, _SELECTOR_SCHEMA)
        yield db


@asynccontextmanager
async def crawl_db(path: Path | str | None = None) -> AsyncIterator[aiosqlite.Connection]:
    # SECURITY: ``path`` is treated as trusted library input. Do NOT forward
    # untrusted strings (e.g. MCP-client-controlled values) here — confine them
    # to a sandbox via ``anansi.security.confine_to_dir`` first.
    db_path = Path(path) if path else DATA_DIR / "crawls.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await _init_schema(db, _CRAWL_SCHEMA)
        # Migrate: add retry_count to url_queue for existing databases
        try:
            await db.execute(
                "ALTER TABLE url_queue ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
            )
            await db.commit()
        except Exception:
            pass  # column already exists
        # Migrate: add content_hash to visited_urls for existing databases
        try:
            await db.execute("ALTER TABLE visited_urls ADD COLUMN content_hash TEXT")
            await db.commit()
        except Exception:
            pass  # column already exists
        # Migrate: create url_cache table for incremental crawling
        try:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS url_cache (
                    url           TEXT PRIMARY KEY,
                    etag          TEXT,
                    last_modified TEXT,
                    content_hash  TEXT,
                    last_fetched  REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            await db.commit()
        except Exception:
            pass
        yield db


async def init_all(data_dir: Path | None = None) -> None:
    """Ensure both databases are initialised."""
    base = data_dir or DATA_DIR
    async with selector_db(base / "selectors.db"):
        pass
    async with crawl_db(base / "crawls.db"):
        pass
