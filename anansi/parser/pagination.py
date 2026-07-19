"""Heuristic next-page URL detection for paginated sites."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

_NEXT_TEXT_RE = re.compile(
    r"^(next(\s*page)?|»|›|→|>>|next\s*›|>\s*next)$",
    re.IGNORECASE,
)
_NEXT_CLASS_RE = re.compile(r"next", re.IGNORECASE)

_PAGE_PARAMS = frozenset({"page", "p", "pg"})


def detect_next_page_url(html: str, base_url: str) -> str | None:
    """
    Heuristically find the next-page URL from a paginated HTML page.

    Detection priority:
    1. <link rel="next"> — semantic standard
    2. <a rel="next"> — anchor with explicit rel
    3. Anchor text matching common "next" patterns
    4. Anchor class/id containing "next"
    5. Query-string ?page=N → ?page=N+1 (also checks "p" and "pg")

    Returns an absolute URL or None. Never returns base_url itself.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    # Scan all anchors once — heuristics 3, 4, and 5 below reuse this list
    # instead of re-walking the tree with find_all("a", href=True) each time.
    anchors = soup.find_all("a", href=True)

    # 1. <link rel="next">
    link_tag = soup.find(
        "link",
        rel=lambda r: r and "next" in (r if isinstance(r, list) else [r]),
    )
    if link_tag and link_tag.get("href"):
        url = urljoin(base_url, str(link_tag["href"]))
        if url != base_url:
            return url

    # 2. <a rel="next">
    a_rel = soup.find(
        "a",
        rel=lambda r: r and "next" in (r if isinstance(r, list) else [r]),
        href=True,
    )
    if a_rel:
        url = urljoin(base_url, str(a_rel["href"]))
        if url != base_url:
            return url

    # 3. Anchor text matches next-page patterns
    for a in anchors:
        text = a.get_text(strip=True)
        if _NEXT_TEXT_RE.match(text):
            url = urljoin(base_url, str(a["href"]))
            if url != base_url:
                return url

    # 4. Anchor class or id contains "next"
    for a in anchors:
        classes = " ".join(a.get("class", []))
        aid = a.get("id", "")
        if _NEXT_CLASS_RE.search(classes) or _NEXT_CLASS_RE.search(aid):
            url = urljoin(base_url, str(a["href"]))
            if url != base_url:
                return url

    # 5. ?page=N → ?page=N+1 confirmed by an anchor in the page
    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    page_param = next((k for k in qs if k.lower() in _PAGE_PARAMS), None)
    if page_param:
        try:
            current_page = int(qs[page_param][0])
        except (ValueError, IndexError):
            current_page = None
        if current_page is not None:
            next_str = str(current_page + 1)
            for a in anchors:
                candidate = urljoin(base_url, str(a["href"]))
                cqs = parse_qs(urlparse(candidate).query)
                if cqs.get(page_param, [None])[0] == next_str:
                    return candidate

    return None
