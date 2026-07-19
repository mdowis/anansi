"""Tests for the single-descent microdata extractor (structured.extract_microdata)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from anansi.parser.structured import extract_microdata


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def test_flat_item_props() -> None:
    items = extract_microdata(_soup(
        '<div itemscope itemtype="http://schema.org/Product">'
        '<span itemprop="name">Widget</span>'
        '<span itemprop="price">9.99</span>'
        '</div>'
    ))
    assert items == [{"@type": "http://schema.org/Product", "name": "Widget", "price": "9.99"}]


def test_nested_itemscope_becomes_nested_dict() -> None:
    items = extract_microdata(_soup(
        '<div itemscope itemtype="Product">'
        '<span itemprop="name">Widget</span>'
        '<div itemprop="brand" itemscope itemtype="Brand">'
        '<span itemprop="name">Acme</span>'
        '</div>'
        '</div>'
    ))
    assert len(items) == 1
    top = items[0]
    assert top["name"] == "Widget"
    # The nested scope's "name" belongs to the sub-item, not the parent.
    assert top["brand"] == {"@type": "Brand", "name": "Acme"}


def test_repeated_prop_collected_into_list() -> None:
    items = extract_microdata(_soup(
        '<ul itemscope>'
        '<li itemprop="tag">a</li>'
        '<li itemprop="tag">b</li>'
        '</ul>'
    ))
    assert items[0]["tag"] == ["a", "b"]


def test_top_level_only_no_duplication_of_nested() -> None:
    # A nested scope must not also appear as its own top-level item.
    items = extract_microdata(_soup(
        '<div itemscope itemtype="Outer">'
        '<div itemprop="inner" itemscope itemtype="Inner">'
        '<span itemprop="v">x</span>'
        '</div>'
        '</div>'
    ))
    assert len(items) == 1
    assert items[0]["inner"] == {"@type": "Inner", "v": "x"}


def test_typed_value_extraction() -> None:
    items = extract_microdata(_soup(
        '<div itemscope>'
        '<a itemprop="url" href="/p/1">link</a>'
        '<meta itemprop="sku" content="ABC">'
        '<time itemprop="date" datetime="2026-01-01">Jan</time>'
        '</div>'
    ))
    it = items[0]
    assert it["url"] == "/p/1"
    assert it["sku"] == "ABC"
    assert it["date"] == "2026-01-01"
