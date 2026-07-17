"""Tests for the persistent, once-initialised SQLite connection registry in db.py.

These guard the efficiency refactor that replaced per-operation connections (each
re-running the full schema) with one cached connection per (event loop, path).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import anansi.db as db
from anansi.db import close_all, crawl_db, selector_db
from anansi.spider.queue import SQLiteQueue

CRAWL_ID = "pool-crawl-0000"


async def _seed_crawl(path: Path) -> None:
    async with crawl_db(path) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO crawls (crawl_id, spider_name, state) VALUES (?, ?, ?)",
            (CRAWL_ID, "test", "running"),
        )
        await conn.commit()


async def test_same_connection_reused_within_loop(tmp_db: Path) -> None:
    """Two ``crawl_db`` context blocks on one loop yield the SAME connection."""
    async with crawl_db(tmp_db) as a:
        conn_a = a
    async with crawl_db(tmp_db) as b:
        conn_b = b
    assert conn_a is conn_b


async def test_distinct_connections_per_path(tmp_path: Path) -> None:
    async with crawl_db(tmp_path / "one.db") as a:
        async with crawl_db(tmp_path / "two.db") as b:
            assert a is not b


async def test_selector_and_crawl_dbs_are_distinct(tmp_path: Path) -> None:
    async with crawl_db(tmp_path / "c.db") as c:
        async with selector_db(tmp_path / "s.db") as s:
            assert c is not s


async def test_schema_initialised_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_init_schema`` runs exactly once per (loop, path), not per operation."""
    calls = 0
    original = db._init_schema

    async def counting(conn, schema):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return await original(conn, schema)

    monkeypatch.setattr(db, "_init_schema", counting)
    path = tmp_path / "once.db"
    for _ in range(3):
        async with crawl_db(path):
            pass
    assert calls == 1


async def test_close_all_allows_clean_reopen(tmp_db: Path) -> None:
    async with crawl_db(tmp_db) as a:
        conn_a = a
    await close_all()
    async with crawl_db(tmp_db) as b:
        # A fresh connection is created after close_all().
        assert b is not conn_a
        row = await (await b.execute("SELECT 1")).fetchone()
        assert row[0] == 1


async def test_concurrent_shared_connection_lands_all_rows(tmp_db: Path) -> None:
    """50 concurrent pushes over one shared connection: no 'database is locked',
    every row persists (guards the aiosqlite-serialisation safety verdict)."""
    await _seed_crawl(tmp_db)
    queue = SQLiteQueue(CRAWL_ID, tmp_db, canonicalize=False)
    await asyncio.gather(*(queue.push(f"https://example.com/{i}") for i in range(50)))
    assert await queue.pending_count() == 50


def test_cross_loop_isolation(tmp_path: Path) -> None:
    """A connection created on one loop is never reused on another. Two separate
    ``asyncio.run`` loops must both work and see each other's committed writes."""
    path = tmp_path / "xloop.db"

    async def writer() -> None:
        await _seed_crawl(path)
        queue = SQLiteQueue(CRAWL_ID, path, canonicalize=False)
        await queue.push("https://example.com/a")
        await close_all()

    async def reader() -> int:
        queue = SQLiteQueue(CRAWL_ID, path, canonicalize=False)
        n = await queue.pending_count()
        await close_all()
        return n

    asyncio.run(writer())
    assert asyncio.run(reader()) == 1
