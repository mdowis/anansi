"""
AdaptiveParser — self-healing CSS selector engine.

Stores selector → confidence scores in SQLite. When a selector fails it runs
four healing strategies (text-match, attribute-fuzzy, structural, xpath-fallback),
picks the winner by confidence, and persists the new selector for future use.
Confidence decays 1% per day for unused selectors to prevent stale knowledge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

import aiosqlite
from bs4 import BeautifulSoup, Tag

from anansi.db import DATA_DIR, selector_db
from anansi.parser.strategies import (
    strategy_attribute_fuzzy,
    strategy_structural,
    strategy_text_match,
    strategy_xpath_fallback,
)
from anansi.parser.structured import extract_all as _extract_structured

# JSON-LD property names that map directly to common scraping field names
_JSONLD_FIELDS = frozenset({
    "name", "price", "description", "url", "image", "author",
    "datePublished", "headline", "sku", "brand", "identifier",
    "articleBody", "contentUrl", "embedUrl", "duration",
})

# Open Graph keys that alias to common field names (og: prefix already stripped)
_OG_ALIASES: dict[str, str] = {
    "title": "title",
    "description": "description",
    "url": "url",
    "image": "image",
}


@dataclass
class SelectorConfig:
    """Flexible selector definition accepted by AdaptiveParser."""

    selector: str
    type: str = "css"                  # "css" | "xpath" | "text"
    attribute: str | None = None       # if set, extract attr instead of text
    multiple: bool = False             # return a list instead of first match
    expected_pattern: str | None = None  # regex hint for healing text-match
    siblings: list[str] = field(default_factory=list)  # sibling selector hints


def _url_to_pattern(url: str) -> str:
    """Normalise a URL to a reusable pattern (host + path, no query/fragment).

    Collapses dynamic segments so selectors learned for one page apply to
    structurally similar pages:
      - UUIDs              → {uuid}
      - Long hex hashes    → {hash}
      - Date triplets      → {date}
      - Year/month pairs   → {year}/{month}
      - Numeric IDs ≥ 3d   → {id}  (1-2 digit segments are kept as-is)
    """
    try:
        p = urlparse(url)
        path = p.path
        # 1. UUIDs (8-4-4-4-12 hex groups)
        path = re.sub(
            r'/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            '/{uuid}', path, flags=re.IGNORECASE,
        )
        # 2. Hex hashes — 32+ contiguous hex chars (MD5 / SHA1 / SHA256)
        path = re.sub(r'/[0-9a-f]{32,}(?=/|$)', '/{hash}', path, flags=re.IGNORECASE)
        # 3. Date triplets  /YYYY/MM/DD
        path = re.sub(r'/\d{4}/\d{2}/\d{2}(?=/|$)', '/{date}', path)
        # 4. Year/month pairs  /YYYY/MM
        path = re.sub(r'/\d{4}/\d{1,2}(?=/|$)', '/{year}/{month}', path)
        # 5. Remaining numerics of 3+ digits (IDs, article numbers, etc.)
        #    1-2 digit segments left intact so /v1, /v2, /en, /5 are preserved
        path = re.sub(r'/\d{3,}(?=/|$)', '/{id}', path)
        return f"{p.netloc}{path}"
    except Exception:
        return url


def _extract_from_tag(tag: Tag, attribute: str | None) -> str | None:
    if attribute:
        return tag.get(attribute)
    text = tag.get_text(separator=" ", strip=True)
    return text if text else None


class AdaptiveParser:
    """
    Parse HTML using CSS selectors that heal themselves when pages change.

    Usage::

        parser = AdaptiveParser()
        data = await parser.extract(html, {
            "title": ".article-title",
            "price": SelectorConfig(".prod-price", expected_pattern=r"\\$[\\d,.]+"),
        }, url="https://example.com/product/123")
    """

    def __init__(self, db_path: Path | None = None, decay_rate: float = 0.99) -> None:
        self._db_path = db_path or DATA_DIR / "selectors.db"
        self._decay_rate = decay_rate  # applied per day of non-use
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    async def extract(
        self,
        html: str,
        selectors: dict[str, str | SelectorConfig],
        url: str = "",
        use_structured: bool = True,
    ) -> dict[str, Any]:
        """Extract fields from *html* using adaptive selectors.

        When *use_structured* is True (the default), JSON-LD and Open Graph
        metadata are checked first. Fields matched there (confidence 0.95) skip
        CSS selector evaluation entirely, which is faster and more reliable for
        sites that embed schema.org markup.

        Returns a dict mapping each field name to its extracted value (or None).
        """
        soup = BeautifulSoup(html, "lxml")
        url_pattern = _url_to_pattern(url)

        # Structured data pre-pass — run once, not per field
        structured_values: dict[str, Any] = {}
        if use_structured:
            sd = _extract_structured(soup)
            for obj in sd["json_ld"]:
                for k, v in obj.items():
                    if not k.startswith("@") and k in _JSONLD_FIELDS and k not in structured_values:
                        structured_values[k] = v
            for field_name, og_key in _OG_ALIASES.items():
                if field_name not in structured_values and og_key in sd["open_graph"]:
                    structured_values[field_name] = sd["open_graph"][og_key]

        tasks = {
            f: self._extract_field(soup, f, cfg, url_pattern)
            for f, cfg in selectors.items()
            if f not in structured_values
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        css_results: dict[str, Any] = {}
        for field_name, result in zip(tasks.keys(), results):
            if isinstance(result, BaseException):
                logger.debug("Field extraction failed for %r: %s", field_name, result)
                css_results[field_name] = None
            else:
                css_results[field_name] = result

        return {f: structured_values.get(f, css_results.get(f)) for f in selectors}

    async def extract_structured(self, html: str) -> dict[str, Any]:
        """Extract JSON-LD, Open Graph, and Microdata from *html*.

        Returns the same shape as ``anansi.parser.structured.extract_all``:
        ``{"json_ld": [...], "open_graph": {...}, "microdata": [...]}``.
        """
        soup = BeautifulSoup(html, "lxml")
        return _extract_structured(soup)

    async def train(
        self,
        url_pattern: str,
        field_name: str,
        selector: str,
        selector_type: str = "css",
    ) -> dict[str, Any]:
        """Manually teach the parser a correct selector, pinned at confidence 1.0.

        Use this to pre-seed knowledge or correct a wrong selector without
        waiting for the self-healing cycle to discover it naturally.
        """
        async with self._lock:
            async with self._write_db() as db:
                await db.execute(
                    """
                    INSERT INTO selectors
                        (url_pattern, field_name, selector, selector_type,
                         confidence, success_count, last_used)
                    VALUES (?, ?, ?, ?, 1.0, 1, datetime('now'))
                    ON CONFLICT(url_pattern, field_name, selector) DO UPDATE SET
                        confidence    = 1.0,
                        success_count = success_count + 1,
                        last_used     = datetime('now')
                    """,
                    (url_pattern, field_name, selector, selector_type),
                )
                await db.commit()
        return {
            "url_pattern": url_pattern,
            "field_name": field_name,
            "selector": selector,
            "selector_type": selector_type,
            "confidence": 1.0,
        }

    async def record_success(
        self,
        url_pattern: str,
        field: str,
        selector: str,
        selector_type: str = "css",
    ) -> None:
        async with self._lock:
            async with self._write_db() as db:
                await self._bump(db, url_pattern, field, selector, selector_type, success=True)

    async def record_failure(
        self,
        url_pattern: str,
        field: str,
        selector: str,
        selector_type: str = "css",
    ) -> None:
        async with self._lock:
            async with self._write_db() as db:
                await self._bump(db, url_pattern, field, selector, selector_type, success=False)

    async def known_selectors(
        self, url_pattern: str, field: str
    ) -> list[dict[str, Any]]:
        """Return all stored selectors for a field, ordered by confidence."""
        async with self._read_db() as db:
            rows = await db.execute_fetchall(
                """
                SELECT selector, selector_type, confidence, success_count, failure_count
                FROM selectors
                WHERE url_pattern = ? AND field_name = ?
                ORDER BY confidence DESC
                """,
                (url_pattern, field),
            )
        return [dict(r) for r in rows]

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _extract_field(
        self,
        soup: BeautifulSoup,
        field: str,
        config: str | SelectorConfig,
        url_pattern: str,
    ) -> Any:
        cfg = config if isinstance(config, SelectorConfig) else SelectorConfig(selector=config)

        # Load historically successful selectors (highest confidence first)
        stored = await self._load_selectors(url_pattern, field)

        # Merge: primary config selector first, then stored alternatives
        candidates: list[tuple[str, str, float]] = [
            (cfg.selector, cfg.type, 1.1)  # slightly above max stored to prefer primary
        ]
        for row in stored:
            if row["selector"] != cfg.selector:
                candidates.append((row["selector"], row["selector_type"], row["confidence"]))

        candidates.sort(key=lambda x: -x[2])

        for selector, stype, _ in candidates:
            result = self._try_selector(soup, selector, stype, cfg)
            if result is not None:
                async with self._lock:
                    async with self._write_db() as db:
                        await self._bump(db, url_pattern, field, selector, stype, success=True)
                return result

        # All known selectors failed → run healing strategies
        healed = await self._heal(soup, cfg, url_pattern, field)
        if healed is not None:
            return healed

        # Record failure on primary selector
        async with self._lock:
            async with self._write_db() as db:
                await self._bump(db, url_pattern, field, cfg.selector, cfg.type, success=False)
        return None

    def _try_selector(
        self,
        soup: BeautifulSoup,
        selector: str,
        stype: str,
        cfg: SelectorConfig,
    ) -> Any:
        try:
            if stype == "css":
                tags = soup.select(selector)
            elif stype == "xpath":
                from lxml import etree
                tree = etree.fromstring(str(soup).encode(), etree.HTMLParser())
                lxml_els = tree.xpath(selector)
                # Convert lxml elements back to text
                from lxml import etree as le
                if cfg.multiple:
                    return [le.tostring(el, encoding="unicode", method="text").strip()
                            for el in lxml_els] or None
                return le.tostring(lxml_els[0], encoding="unicode", method="text").strip() if lxml_els else None
            elif stype == "text":
                # selector is a regex matched against element text
                tags = [
                    t.parent for t in soup.find_all(string=re.compile(selector, re.I))
                    if t.parent
                ]
            else:
                return None

            if not tags:
                return None

            if cfg.multiple:
                return [v for t in tags if (v := _extract_from_tag(t, cfg.attribute))]

            return _extract_from_tag(tags[0], cfg.attribute)
        except Exception:
            return None

    async def _heal(
        self,
        soup: BeautifulSoup,
        cfg: SelectorConfig,
        url_pattern: str,
        field: str,
    ) -> Any:
        """
        Run all healing strategies and pick the highest-confidence candidate.
        Persist the winning selector to the database.
        """
        all_candidates: list[tuple[Tag, float]] = []

        all_candidates += strategy_text_match(
            soup, cfg.selector, cfg.expected_pattern
        )
        all_candidates += strategy_attribute_fuzzy(soup, cfg.selector)
        all_candidates += strategy_structural(
            soup, cfg.selector, sibling_context=cfg.siblings
        )
        all_candidates += strategy_xpath_fallback(soup, cfg.selector)

        if not all_candidates:
            return None

        all_candidates.sort(key=lambda x: -x[1])
        best_tag, best_score = all_candidates[0]

        if best_score < 0.5:
            return None

        value = _extract_from_tag(best_tag, cfg.attribute)
        if value is None:
            return None

        # Derive a new CSS selector from the winning element
        new_selector = self._tag_to_selector(best_tag)
        async with self._lock:
            async with self._write_db() as db:
                await db.execute(
                    """
                    INSERT INTO selectors
                        (url_pattern, field_name, selector, selector_type, confidence,
                         success_count, last_used)
                    VALUES (?, ?, ?, 'css', ?, 1, datetime('now'))
                    ON CONFLICT(url_pattern, field_name, selector) DO UPDATE SET
                        confidence    = excluded.confidence,
                        success_count = success_count + 1,
                        last_used     = datetime('now')
                    """,
                    (url_pattern, field, new_selector, round(best_score, 4)),
                )
                await db.commit()

        return value

    def _tag_to_selector(self, tag: Tag) -> str:
        """Build a best-effort CSS selector from a bs4 Tag."""
        parts: list[str] = []
        node = tag
        for _ in range(4):  # walk up at most 4 levels
            if not isinstance(node, Tag) or node.name in ("html", "body", "[document]"):
                break
            part = node.name
            if node.get("id"):
                part += f"#{node['id']}"
                parts.append(part)
                break
            classes = node.get("class", [])
            if classes:
                part += "." + ".".join(classes[:2])  # first 2 classes only
            parts.append(part)
            node = node.parent
        return " > ".join(reversed(parts)) if parts else tag.name

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _read_db(self):
        return selector_db(self._db_path)

    def _write_db(self):
        return selector_db(self._db_path)

    async def _load_selectors(
        self, url_pattern: str, field: str
    ) -> list[dict[str, Any]]:
        async with self._read_db() as db:
            rows = await db.execute_fetchall(
                """
                SELECT selector, selector_type, confidence
                FROM selectors
                WHERE url_pattern = ? AND field_name = ?
                ORDER BY confidence DESC
                LIMIT 10
                """,
                (url_pattern, field),
            )
        return [dict(r) for r in rows]

    async def _bump(
        self,
        db: aiosqlite.Connection,
        url_pattern: str,
        field: str,
        selector: str,
        selector_type: str,
        success: bool,
    ) -> None:
        if success:
            await db.execute(
                """
                INSERT INTO selectors
                    (url_pattern, field_name, selector, selector_type,
                     confidence, success_count, last_used)
                VALUES (?, ?, ?, ?, 1.0, 1, datetime('now'))
                ON CONFLICT(url_pattern, field_name, selector) DO UPDATE SET
                    confidence    = MIN(1.0, confidence * 1.05 + 0.02),
                    success_count = success_count + 1,
                    last_used     = datetime('now')
                """,
                (url_pattern, field, selector, selector_type),
            )
        else:
            await db.execute(
                """
                INSERT INTO selectors
                    (url_pattern, field_name, selector, selector_type,
                     confidence, failure_count, last_used)
                VALUES (?, ?, ?, ?, 0.5, 1, datetime('now'))
                ON CONFLICT(url_pattern, field_name, selector) DO UPDATE SET
                    confidence    = MAX(0.0, confidence * 0.85 - 0.05),
                    failure_count = failure_count + 1,
                    last_used     = datetime('now')
                """,
                (url_pattern, field, selector, selector_type),
            )
        await db.commit()

    async def decay_stale_selectors(self, days_threshold: int = 7) -> int:
        """Reduce confidence of selectors unused for more than *days_threshold* days.

        Returns the number of rows updated.
        """
        async with self._write_db() as db:
            cur = await db.execute(
                """
                UPDATE selectors
                SET confidence = MAX(0.0, confidence * ?)
                WHERE last_used < datetime('now', ? || ' days')
                  AND confidence > 0
                """,
                (self._decay_rate, f"-{days_threshold}"),
            )
            await db.commit()
            return cur.rowcount
