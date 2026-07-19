"""Batched link enqueue (push_batch) and item persistence (_persist_items) (#14)."""

from __future__ import annotations

from pathlib import Path

from anansi.core import Item, Spider
from anansi.db import crawl_db
from anansi.spider.crawler import Crawler
from anansi.spider.queue import SQLiteQueue

CRAWL_ID = "batch-crawl-0"


async def _seed(db_path: Path, crawl_id: str = CRAWL_ID) -> None:
    async with crawl_db(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO crawls (crawl_id, spider_name, state) VALUES (?, ?, ?)",
            (crawl_id, "t", "running"),
        )
        await db.commit()


async def test_push_batch_inserts_per_row_metadata(tmp_db: Path) -> None:
    await _seed(tmp_db)
    q = SQLiteQueue(CRAWL_ID, tmp_db, canonicalize=False)
    n = await q.push_batch([
        ("http://e.com/a", "parse", 0, {"depth": 1}),
        ("http://e.com/b", "custom", 5, {"depth": 2}),
    ])
    assert n == 2
    assert await q.pending_count() == 2
    # Highest priority pops first, and per-row callback/meta survive.
    url, callback, meta = await q.pop()
    assert url == "http://e.com/b"
    assert callback == "custom"
    assert meta["depth"] == 2


async def test_push_batch_dedupes_against_existing(tmp_db: Path) -> None:
    await _seed(tmp_db)
    q = SQLiteQueue(CRAWL_ID, tmp_db, canonicalize=False)
    await q.push("http://e.com/a")
    n = await q.push_batch([
        ("http://e.com/a", "parse", 0, {}),  # duplicate → ignored
        ("http://e.com/c", "parse", 0, {}),
    ])
    assert n == 1
    assert await q.pending_count() == 2


async def test_persist_items_batches_a_page(tmp_db: Path) -> None:
    class _S(Spider):
        name = "s"

        async def parse(self, response):  # pragma: no cover
            return
            yield

    crawler = Crawler(spider_class=_S, db_path=tmp_db)
    await _seed(tmp_db, crawler._crawl_id)
    items = [
        Item(data={"i": 1}, source_url="http://e.com/1", spider_name="s"),
        Item(data={"i": 2}, source_url="http://e.com/2", spider_name="s"),
    ]
    await crawler._persist_items(items)
    rows = await Crawler.get_items(crawler._crawl_id, db_path=tmp_db)
    assert len(rows) == 2
