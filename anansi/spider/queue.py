"""
SQLite-backed persistent URL queue for pause/resume support.

Uses INSERT OR IGNORE so duplicate URLs are silently dropped. Pops the
highest-priority, lowest-id pending URL atomically via a single UPDATE…RETURNING
statement (SQLite ≥ 3.35 / Python ≥ 3.11 ships with this version).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from anansi.db import DATA_DIR, crawl_db
from anansi.utils.url import canonicalize_url


class SQLiteQueue:
    """
    Priority URL queue backed by a SQLite table.

    All mutations are safe to call concurrently. Each operation borrows the
    shared connection cached by ``anansi.db`` (one per event loop + database
    path, opened and schema-initialised once); aiosqlite serialises operations
    on that connection's worker thread, and WAL mode allows concurrent readers.
    """

    def __init__(
        self,
        crawl_id: str,
        db_path: Path | None = None,
        canonicalize: bool = True,
    ) -> None:
        self.crawl_id = crawl_id
        self._db_path = db_path or DATA_DIR / "crawls.db"
        self._canonicalize = canonicalize

    def _norm(self, url: str) -> str:
        """Return the canonical form of *url* when canonicalization is enabled."""
        return canonicalize_url(url) if self._canonicalize else url

    # ── Enqueue ───────────────────────────────────────────────────────────────

    async def push(
        self,
        url: str,
        *,
        priority: int = 0,
        callback: str = "parse",
        meta: dict[str, Any] | None = None,
    ) -> bool:
        """Add *url* to the queue. Returns True if inserted, False if duplicate."""
        url = self._norm(url)
        async with crawl_db(self._db_path) as db:
            cur = await db.execute(
                """
                INSERT OR IGNORE INTO url_queue
                    (crawl_id, url, priority, callback, meta)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.crawl_id, url, priority, callback, json.dumps(meta or {})),
            )
            await db.commit()
            return cur.rowcount > 0

    async def push_many(self, urls: list[str], **kwargs: Any) -> int:
        """Batch-insert URLs sharing the same priority/callback/meta.

        Returns the number of newly inserted rows (best effort).
        """
        if not urls:
            return 0
        priority = kwargs.get("priority", 0)
        callback = kwargs.get("callback", "parse")
        meta_json = json.dumps(kwargs.get("meta") or {})
        params = [
            (self.crawl_id, self._norm(url), priority, callback, meta_json)
            for url in urls
        ]
        return await self._insert_many(params)

    async def push_batch(
        self, requests: list[tuple[str, str, int, dict[str, Any] | None]]
    ) -> int:
        """Batch-insert ``(url, callback, priority, meta)`` rows in one
        transaction — one ``executemany`` + one commit instead of a
        connection/round-trip per URL. Returns the number of newly inserted rows.
        """
        if not requests:
            return 0
        params = [
            (self.crawl_id, self._norm(url), priority, callback, json.dumps(meta or {}))
            for (url, callback, priority, meta) in requests
        ]
        return await self._insert_many(params)

    async def _insert_many(self, params: list[tuple[Any, ...]]) -> int:
        async with crawl_db(self._db_path) as db:
            cur = await db.executemany(
                """
                INSERT OR IGNORE INTO url_queue
                    (crawl_id, url, priority, callback, meta)
                VALUES (?, ?, ?, ?, ?)
                """,
                params,
            )
            await db.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # ── Dequeue ───────────────────────────────────────────────────────────────

    async def pop(self) -> tuple[str, str, dict[str, Any]] | None:
        """
        Atomically claim and return (url, callback, meta) for the next pending URL.
        Returns None when the queue is empty.
        """
        async with crawl_db(self._db_path) as db:
            # Find the best candidate
            row = await db.execute_fetchall(
                """
                SELECT id, url, callback, meta
                FROM url_queue
                WHERE crawl_id = ? AND status = 'pending'
                ORDER BY priority DESC, id ASC
                LIMIT 1
                """,
                (self.crawl_id,),
            )
            if not row:
                return None
            rid, url, callback, meta_json = row[0]["id"], row[0]["url"], row[0]["callback"], row[0]["meta"]
            await db.execute(
                "UPDATE url_queue SET status = 'processing' WHERE id = ?",
                (rid,),
            )
            await db.commit()
        return url, callback, json.loads(meta_json or "{}")

    async def mark_done(self, url: str) -> None:
        async with crawl_db(self._db_path) as db:
            await db.execute(
                "UPDATE url_queue SET status='done' WHERE crawl_id=? AND url=?",
                (self.crawl_id, self._norm(url)),
            )
            await db.commit()

    async def mark_failed(self, url: str) -> None:
        async with crawl_db(self._db_path) as db:
            await db.execute(
                "UPDATE url_queue SET status='failed' WHERE crawl_id=? AND url=?",
                (self.crawl_id, self._norm(url)),
            )
            await db.commit()

    async def increment_retry(self, url: str, max_retries: int = 3) -> None:
        """Increment retry count; requeue if under budget, permanently fail if over."""
        async with crawl_db(self._db_path) as db:
            await db.execute(
                """
                UPDATE url_queue SET
                    retry_count = retry_count + 1,
                    status = CASE WHEN retry_count + 1 < ? THEN 'pending' ELSE 'failed' END
                WHERE crawl_id = ? AND url = ?
                """,
                (max_retries, self.crawl_id, self._norm(url)),
            )
            await db.commit()

    async def requeue_stale(self) -> int:
        """Reset 'processing' URLs back to 'pending' (after crash recovery)."""
        async with crawl_db(self._db_path) as db:
            cur = await db.execute(
                "UPDATE url_queue SET status='pending' WHERE crawl_id=? AND status='processing'",
                (self.crawl_id,),
            )
            await db.commit()
            return cur.rowcount

    # ── Visited set ───────────────────────────────────────────────────────────

    async def mark_visited(self, url: str) -> None:
        async with crawl_db(self._db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO visited_urls (crawl_id, url) VALUES (?, ?)",
                (self.crawl_id, self._norm(url)),
            )
            await db.commit()

    async def is_visited(self, url: str) -> bool:
        async with crawl_db(self._db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT 1 FROM visited_urls WHERE crawl_id=? AND url=? LIMIT 1",
                (self.crawl_id, self._norm(url)),
            )
        return len(rows) > 0

    async def is_content_seen(self, content_hash: str) -> bool:
        """Return True if this content hash has already been scraped in this crawl."""
        async with crawl_db(self._db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT 1 FROM visited_urls WHERE crawl_id=? AND content_hash=? LIMIT 1",
                (self.crawl_id, content_hash),
            )
        return len(rows) > 0

    async def mark_content_seen(self, url: str, content_hash: str) -> None:
        """Record content_hash for a visited URL (upsert — URL must already be in visited_urls)."""
        async with crawl_db(self._db_path) as db:
            await db.execute(
                "UPDATE visited_urls SET content_hash=? WHERE crawl_id=? AND url=?",
                (content_hash, self.crawl_id, url),
            )
            await db.commit()

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def failed_count(self) -> int:
        async with crawl_db(self._db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT COUNT(*) AS n FROM url_queue WHERE crawl_id=? AND status='failed'",
                (self.crawl_id,),
            )
        return rows[0]["n"] if rows else 0

    async def pending_count(self) -> int:
        async with crawl_db(self._db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT COUNT(*) AS n FROM url_queue WHERE crawl_id=? AND status='pending'",
                (self.crawl_id,),
            )
        return rows[0]["n"] if rows else 0

    async def visited_count(self) -> int:
        async with crawl_db(self._db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT COUNT(*) AS n FROM visited_urls WHERE crawl_id=?",
                (self.crawl_id,),
            )
        return rows[0]["n"] if rows else 0
