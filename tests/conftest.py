"""Shared test fixtures for the Anansi test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import anansi.db as _db
from anansi.db import crawl_db
from anansi.spider.queue import SQLiteQueue

CRAWL_ID = "test-crawl-0000"


@pytest.fixture(autouse=True)
async def _release_shared_resources():
    """Close shared DB connections (and any pooled MCP browser fetchers) at the end
    of every test.

    ``db.py`` caches one ``aiosqlite`` connection per (event loop, path), and each
    test runs on its own function-scoped loop (``asyncio_mode="auto"``). Leaking a
    connection past its loop would let a later test whose loop reuses the same id
    receive a connection bound to a dead loop and hang, so release them here.
    """
    yield
    await _db.close_all()
    server_mod = sys.modules.get("anansi.mcp_server.server")
    if server_mod is not None:
        try:
            await server_mod._close_browser_fetchers()
        except Exception:
            pass


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Per-test ephemeral SQLite database path (never touches ~/.anansi)."""
    return tmp_path / "test_crawls.db"


@pytest.fixture
def tmp_sel_db(tmp_path: Path) -> Path:
    """Per-test ephemeral selector database path."""
    return tmp_path / "test_selectors.db"


@pytest.fixture
async def queue(tmp_db: Path) -> SQLiteQueue:
    """SQLiteQueue pre-seeded with the required crawl FK row."""
    async with crawl_db(tmp_db) as db:
        await db.execute(
            "INSERT OR IGNORE INTO crawls (crawl_id, spider_name, state) VALUES (?, ?, ?)",
            (CRAWL_ID, "test", "running"),
        )
        await db.commit()
    return SQLiteQueue(CRAWL_ID, tmp_db, canonicalize=False)
