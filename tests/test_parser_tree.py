"""Shared lxml tree + spa-state gate optimizations (#20)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from anansi.parser.structured import extract_spa_state, lxml_tree


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def test_lxml_tree_cached_per_soup() -> None:
    soup = _soup("<html><body><a href='/x'>y</a></body></html>")
    t1 = lxml_tree(soup)
    assert lxml_tree(soup) is t1  # same soup → same tree, parsed once
    other = _soup("<html><body><p>z</p></body></html>")
    assert lxml_tree(other) is not t1
    assert lxml_tree(soup).xpath("//a/@href") == ["/x"]


def test_spa_state_detects_next_data_by_id() -> None:
    # __NEXT_DATA__ appears only as the script id, not in the JSON text — the
    # optimized gate must still catch it.
    soup = _soup('<script id="__NEXT_DATA__" type="application/json">{"a": 1}</script>')
    assert extract_spa_state(soup) == {"next_data": {"a": 1}}


def test_spa_state_detects_marker_in_text() -> None:
    soup = _soup('<script>window.__NUXT__ = {"b": 2};</script>')
    assert extract_spa_state(soup).get("nuxt") == {"b": 2}


def test_spa_state_empty_when_no_markers() -> None:
    assert extract_spa_state(_soup("<div>plain page</div>")) == {}
