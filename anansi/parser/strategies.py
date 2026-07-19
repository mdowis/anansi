"""
Selector healing strategies.

When a known CSS selector stops matching, these strategies attempt to locate
the same element using different heuristics, ranked by confidence.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from bs4 import BeautifulSoup, Tag


def _text(tag: Tag) -> str:
    return tag.get_text(separator=" ", strip=True)


def _class_similarity(a: list[str], b: list[str]) -> float:
    """Ratio of class overlap between two elements."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(len(sa), len(sb))


def _selector_to_tag_classes(selector: str) -> tuple[str | None, list[str]]:
    """Parse a simple CSS selector into (tag, [classes])."""
    tag_match = re.match(r'^([a-zA-Z][a-zA-Z0-9]*)', selector)
    tag = tag_match.group(1).lower() if tag_match else None
    classes = re.findall(r'\.([a-zA-Z_-][a-zA-Z0-9_-]*)', selector)
    return tag, classes


# ── Strategy functions ────────────────────────────────────────────────────────

def strategy_text_match(
    soup: BeautifulSoup,
    original_selector: str,
    expected_pattern: str | None,
    **_: Any,
) -> list[tuple[Tag, float]]:
    """Find elements whose text matches an expected regex pattern."""
    if not expected_pattern:
        return []
    compiled = re.compile(expected_pattern, re.IGNORECASE)
    results: list[tuple[Tag, float]] = []
    for tag in soup.find_all(string=compiled):
        parent = tag.parent
        if parent and isinstance(parent, Tag):
            results.append((parent, 0.75))
    return results


def strategy_attribute_fuzzy(
    soup: BeautifulSoup,
    original_selector: str,
    **_: Any,
) -> list[tuple[Tag, float]]:
    """
    Find elements whose class names are similar to those in the broken selector.
    Uses difflib sequence matching with a threshold of 0.6.
    """
    _, wanted_classes = _selector_to_tag_classes(original_selector)
    if not wanted_classes:
        return []

    # Memoize similarity per (wanted, candidate) pair: a page has thousands of
    # elements but only dozens of distinct class names, so the same pair would
    # otherwise be scored thousands of times.
    ratio_cache: dict[tuple[str, str], float] = {}

    def _ratio(wc: str, tc: str) -> float:
        if wc == tc:
            return 1.0
        key = (wc, tc)
        cached = ratio_cache.get(key)
        if cached is None:
            cached = difflib.SequenceMatcher(None, wc, tc).ratio()
            ratio_cache[key] = cached
        return cached

    results: list[tuple[Tag, float]] = []
    for tag in soup.find_all(True):
        tag_classes: list[str] = tag.get("class", [])
        if not tag_classes:
            continue
        # Score each wanted class against the tag's classes
        scores = [max(_ratio(wc, tc) for tc in tag_classes) for wc in wanted_classes]
        avg_score = sum(scores) / len(scores)
        if avg_score >= 0.6:
            results.append((tag, avg_score * 0.85))  # cap below text-match

    results.sort(key=lambda x: -x[1])
    return results[:5]


def strategy_structural(
    soup: BeautifulSoup,
    original_selector: str,
    sibling_context: list[str] | None = None,
    **_: Any,
) -> list[tuple[Tag, float]]:
    """
    Try parent/sibling/child navigation around elements matched by related selectors.
    `sibling_context` is a list of selectors for known sibling fields.
    """
    if not sibling_context:
        return []
    results: list[tuple[Tag, float]] = []
    _, wanted_classes = _selector_to_tag_classes(original_selector)

    for sib_selector in sibling_context:
        anchors = soup.select(sib_selector)
        for anchor in anchors:
            parent = anchor.parent
            if not parent:
                continue
            for child in parent.children:
                if not isinstance(child, Tag):
                    continue
                if child == anchor:
                    continue
                child_classes = child.get("class", [])
                sim = _class_similarity(wanted_classes, child_classes) if wanted_classes else 0.3
                results.append((child, 0.5 + sim * 0.2))

    results.sort(key=lambda x: -x[1])
    return results[:5]


def strategy_xpath_fallback(
    soup: BeautifulSoup,
    original_selector: str,
    **_: Any,
) -> list[tuple[Tag, float]]:
    """
    Convert a CSS selector to a permissive XPath and try that.
    Handles common cases: tag.class, #id, tag[attr].
    """
    try:
        from lxml import etree

        tag, classes = _selector_to_tag_classes(original_selector)
        tag_part = tag if tag else "*"
        if classes:
            class_pred = " and ".join(
                f"contains(concat(' ',normalize-space(@class),' '),' {c} ')"
                for c in classes
            )
            xpath = f"//{tag_part}[{class_pred}]"
        else:
            id_match = re.search(r'#([a-zA-Z_-][a-zA-Z0-9_-]*)', original_selector)
            if id_match:
                xpath = f"//{tag_part}[@id='{id_match.group(1)}']"
            else:
                xpath = f"//{tag_part}"

        from anansi.parser.structured import lxml_tree
        lxml_elements = lxml_tree(soup).xpath(xpath)

        results: list[tuple[Tag, float]] = []
        for el in lxml_elements[:5]:
            el_str = etree.tostring(el, encoding="unicode", method="html")
            # Find corresponding bs4 tag
            mini_soup = BeautifulSoup(el_str, "lxml")
            found = mini_soup.find()
            if found:
                # We return the lxml repr as a tag placeholder; caller uses text
                results.append((found, 0.6))

        return results
    except Exception:
        return []
