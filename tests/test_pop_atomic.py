"""pop() is a single atomic UPDATE...RETURNING that skips visited URLs (#15)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from anansi.db import crawl_db
from anansi.spider.queue import SQLiteQueue

CRAWL_ID = "pop-crawl-0"


async def _seed(db_path: Path) -> None:
    async with crawl_db(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO crawls (crawl_id, spider_name, state) VALUES (?, ?, ?)",
            (CRAWL_ID, "t", "running"),
        )
        await db.commit()


async def test_pop_skips_visited_urls(tmp_db: Path) -> None:
    await _seed(tmp_db)
    q = SQLiteQueue(CRAWL_ID, tmp_db, canonicalize=False)
    await q.push("http://e.com/a", priority=5)
    await q.push("http://e.com/b", priority=1)
    await q.mark_visited("http://e.com/a")  # already visited → pop must skip it
    entry = await q.pop()
    assert entry is not None
    assert entry[0] == "http://e.com/b"
    # Only b was claimable; the queue is now empty of poppable rows.
    assert await q.pop() is None


async def test_concurrent_pops_claim_distinct_rows(tmp_db: Path) -> None:
    await _seed(tmp_db)
    q = SQLiteQueue(CRAWL_ID, tmp_db, canonicalize=False)
    for i in range(10):
        await q.push(f"http://e.com/{i}")
    results = await asyncio.gather(*(q.pop() for _ in range(10)))
    urls = [r[0] for r in results if r is not None]
    assert len(urls) == 10
    assert len(set(urls)) == 10  # no row claimed twice
