"""Tests for the O(1) LRU-evicting defaultdict used by the domain throttle."""

from __future__ import annotations

from anansi.spider.crawler import _LRUDefault


def test_missing_read_inserts_default_but_get_does_not() -> None:
    d = _LRUDefault(lambda: 0, max_entries=3)
    assert d.get("absent") is None
    assert "absent" not in d
    assert d["auto"] == 0  # __missing__ inserts the factory default
    assert "auto" in d


def test_eviction_respects_recent_use() -> None:
    d = _LRUDefault(lambda: 0, max_entries=2)
    d["a"] = 1
    d["b"] = 2
    assert d["a"] == 1  # promote "a" → "b" is now least-recently-used
    d["c"] = 3  # over cap → evict the LRU entry ("b")
    assert "b" not in d
    assert list(d.keys()) == ["a", "c"]
    assert d["a"] == 1 and d["c"] == 3


def test_factory_value_is_mutable_in_place() -> None:
    d = _LRUDefault(list, max_entries=4)
    d["k"].append(1)
    d["k"].append(2)
    assert d["k"] == [1, 2]
