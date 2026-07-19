"""
Structured data extraction from HTML: JSON-LD, Open Graph, and Microdata.

All functions accept a BeautifulSoup object and return plain Python dicts/lists.
No external dependencies beyond what's already in the project (bs4, json).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


def extract_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Parse all <script type="application/ld+json"> blocks in the page.

    Returns a flat list of parsed objects. Each block may contain either a
    single object or a JSON array — both are normalised. Handles @graph arrays.
    Malformed blocks are silently skipped.
    """
    results: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.get_text(strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.debug("Malformed JSON-LD block skipped")
            continue
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    # Unwrap @graph if present
                    if "@graph" in item and isinstance(item["@graph"], list):
                        results.extend(i for i in item["@graph"] if isinstance(i, dict))
                    else:
                        results.append(item)
        elif isinstance(parsed, dict):
            if "@graph" in parsed and isinstance(parsed["@graph"], list):
                results.extend(i for i in parsed["@graph"] if isinstance(i, dict))
            else:
                results.append(parsed)
    return results


def extract_opengraph(soup: BeautifulSoup) -> dict[str, str]:
    """Extract <meta property="og:*"> and <meta name="twitter:*"> tags.

    Keys for og: tags have the "og:" prefix stripped (e.g. "og:title" → "title").
    Twitter card tags keep their full name (e.g. "twitter:card").
    Only the first occurrence of each key is kept.
    """
    result: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        content = meta.get("content")
        if not prop or content is None:
            continue
        if prop.startswith("og:"):
            key = prop[3:]
            result.setdefault(key, content)
        elif prop.startswith("twitter:"):
            result.setdefault(prop, content)
    return result


def extract_microdata(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Extract Microdata items (itemscope / itemprop attributes).

    Walks top-level itemscope elements and collects their itemprop children.
    Nested itemscopes become nested dicts. When the same itemprop name appears
    multiple times under one scope, values are collected into a list.
    """

    def _prop_value(el: Tag) -> Any:
        if el.has_attr("itemscope"):
            return _collect_item(el)
        if el.name in ("a", "link") and el.get("href"):
            return el["href"]
        if el.name in ("img", "audio", "video", "source") and el.get("src"):
            return el["src"]
        if el.name == "meta" and el.get("content") is not None:
            return el["content"]
        if el.name == "time" and el.get("datetime"):
            return el["datetime"]
        return el.get_text(separator=" ", strip=True)

    def _add(item: dict[str, Any], name: str, value: Any) -> None:
        if name in item:
            existing = item[name]
            if isinstance(existing, list):
                existing.append(value)
            else:
                item[name] = [existing, value]
        else:
            item[name] = value

    def _collect_item(root: Tag) -> dict[str, Any]:
        item: dict[str, Any] = {}
        item_type = root.get("itemtype", "")
        if item_type:
            item["@type"] = item_type

        # Single descent over the subtree instead of ``find_all(itemprop=True)``
        # plus an O(depth) ancestor walk per hit: recurse into a nested itemscope
        # only through its own sub-item (its props don't belong to this scope).
        def _walk(node: Tag) -> None:
            for child in node.children:
                if not isinstance(child, Tag):
                    continue
                has_prop = child.has_attr("itemprop")
                has_scope = child.has_attr("itemscope")
                if has_prop:
                    prop_name = child.get("itemprop", "").strip()
                    if prop_name:
                        _add(item, prop_name, _prop_value(child))
                    # A nested scope's inner props belong to the sub-item, so only
                    # keep descending when this itemprop is not itself a scope.
                    if not has_scope:
                        _walk(child)
                elif has_scope:
                    # Nested scope with no itemprop is a separate item; skip it.
                    continue
                else:
                    _walk(child)

        _walk(root)
        return item

    results: list[dict[str, Any]] = []

    # Collect only top-level itemscopes (those with no itemscope ancestor) by
    # descending from the root and stopping at the first scope on each branch.
    def _find_top(node: Tag) -> None:
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            if child.has_attr("itemscope"):
                results.append(_collect_item(child))
            else:
                _find_top(child)

    _find_top(soup)
    return results


_SPA_MARKERS = ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__PRELOADED_STATE__", "__REDUX_STATE__")


def extract_spa_state(soup: BeautifulSoup) -> dict[str, Any]:
    """Extract SPA framework bootstrap payloads embedded as inline JSON.

    Handles Next.js (__NEXT_DATA__), Nuxt.js (__NUXT__), and Redux-style
    (__INITIAL_STATE__, __PRELOADED_STATE__, __REDUX_STATE__) patterns.
    Returns only keys that were found; malformed JSON is silently skipped.
    """
    raw_html = str(soup)
    if not any(m in raw_html for m in _SPA_MARKERS):
        return {}

    result: dict[str, Any] = {}

    # Next.js: <script id="__NEXT_DATA__" type="application/json">...</script>
    next_tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if next_tag:
        try:
            result["next_data"] = json.loads(next_tag.get_text(strip=True))
        except (json.JSONDecodeError, ValueError):
            logger.debug("Malformed __NEXT_DATA__ JSON skipped")

    # Nuxt / Redux: window.__KEY__ = {...};
    _var_map = {
        "__NUXT__": "nuxt",
        "__INITIAL_STATE__": "initial_state",
        "__PRELOADED_STATE__": "preloaded_state",
        "__REDUX_STATE__": "redux_state",
    }
    for script in soup.find_all("script"):
        text = script.get_text()
        if not text:
            continue
        for marker, key in _var_map.items():
            if key in result or marker not in text:
                continue
            idx = text.find(marker)
            if idx == -1:
                continue
            eq_idx = text.find("=", idx)
            if eq_idx == -1:
                continue
            json_str = text[eq_idx + 1:].strip().rstrip(";").strip()
            try:
                result[key] = json.loads(json_str)
            except (json.JSONDecodeError, ValueError):
                logger.debug("Malformed %s JSON skipped", marker)

    return result


def extract_all(soup: BeautifulSoup) -> dict[str, Any]:
    """Run all three extractors and combine into a single dict.

    Returns:
        {
            "json_ld": [...],      # list of parsed JSON-LD objects
            "open_graph": {...},   # flat dict of og: / twitter: meta tags
            "microdata": [...],    # list of parsed microdata items
        }
    """
    return {
        "json_ld": extract_jsonld(soup),
        "open_graph": extract_opengraph(soup),
        "microdata": extract_microdata(soup),
        "spa_state": extract_spa_state(soup),
    }
