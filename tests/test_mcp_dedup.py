"""Single-parse extract + single-row crawl helpers (#22)."""

from __future__ import annotations

from pathlib import Path

from anansi.db import crawl_db
from anansi.parser.adaptive import AdaptiveParser
from anansi.spider.crawler import Crawler


async def test_extract_with_structured_returns_fields_and_full_structured(tmp_sel_db: Path) -> None:
    html = (
        '<html><head><meta property="og:title" content="T"></head>'
        '<body><h1 class="t">Hi</h1></body></html>'
    )
    parser = AdaptiveParser(db_path=tmp_sel_db)
    # "heading" is not an Open Graph alias, so it comes from the CSS selector.
    fields, structured = await parser.extract_with_structured(
        html, {"heading": "h1.t"}, url="http://e.com/p"
    )
    assert fields["heading"] == "Hi"
    # Full structured payload (extract_all shape), computed from one parse.
    assert set(structured) >= {"json_ld", "open_graph", "microdata", "spa_state"}
    assert structured["open_graph"].get("title") == "T"


async def test_get_crawl_and_count_items(tmp_db: Path) -> None:
    async with crawl_db(tmp_db) as db:
        await db.execute(
            "INSERT INTO crawls (crawl_id, spider_name, state) VALUES (?, ?, ?)",
            ("c1", "s", "finished"),
        )
        await db.executemany(
            "INSERT INTO items (crawl_id, source_url, spider_name, data) VALUES (?,?,?,?)",
            [("c1", "u", "s", '{"a": 1}'), ("c1", "u2", "s", '{"a": 2}')],
        )
        await db.commit()

    row = await Crawler.get_crawl("c1", db_path=tmp_db)
    assert row is not None and row["state"] == "finished"
    assert await Crawler.get_crawl("nope", db_path=tmp_db) is None
    assert await Crawler.count_items("c1", db_path=tmp_db) == 2
    assert await Crawler.count_items("nope", db_path=tmp_db) == 0
