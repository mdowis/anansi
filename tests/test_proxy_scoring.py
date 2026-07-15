"""Tests for target-aware proxy stats and scored selection."""

from __future__ import annotations

import pytest

from anansi.protection import ProtectionVendor
from anansi.proxy.manager import NoProxiesAvailable, ProxyManager, ProxyRotationStrategy


def _pm(*urls: str, strategy=ProxyRotationStrategy.ROUND_ROBIN, **kw) -> ProxyManager:
    return ProxyManager(list(urls), strategy=strategy, **kw)


# ── Task 11: target-aware stats ───────────────────────────────────────────────

def test_report_success_backward_compatible_no_args() -> None:
    pm = _pm("http://a:1")
    pm.report_success("http://a:1")  # old no-arg form must still work
    stats = {s["url"]: s for s in pm.stats()}
    assert stats["http://a:1"]["success_count"] == 1


def test_report_failure_backward_compatible_no_args() -> None:
    pm = _pm("http://a:1", max_failures=2)
    pm.report_failure("http://a:1")  # old no-arg form
    pm.report_failure("http://a:1")
    # Two plain failures still quarantine as before.
    assert pm.healthy_count == 0


def test_report_success_records_domain_and_vendor() -> None:
    pm = _pm("http://a:1")
    pm.report_success("http://a:1", domain="shop.com", vendor=ProtectionVendor.CLOUDFLARE)
    entry = pm._entries["http://a:1"]
    assert entry.domain_success["shop.com"] == 1
    assert entry.vendor_success["cloudflare"] == 1
    assert entry.success_count == 1


def test_report_failure_hard_block_and_challenge_split() -> None:
    pm = _pm("http://a:1", max_failures=10)
    pm.report_failure("http://a:1", domain="shop.com", hard_block=True)
    pm.report_failure("http://a:1", domain="shop.com", hard_block=False)
    entry = pm._entries["http://a:1"]
    assert entry.hard_block_count == 1
    assert entry.failure_count == 2
    assert entry.challenge_count == 1
    assert entry.domain_failure["shop.com"] == 2


def test_report_failure_penalize_false_does_not_quarantine() -> None:
    pm = _pm("http://a:1", max_failures=1)
    pm.report_failure("http://a:1", domain="shop.com", hard_block=True, penalize=False)
    # Recorded for scoring, but not counted toward quarantine.
    assert pm.healthy_count == 1
    assert pm._entries["http://a:1"].hard_block_count == 1


def test_vendor_accepts_enum_or_string() -> None:
    pm = _pm("http://a:1")
    pm.report_success("http://a:1", vendor="akamai")
    pm.report_success("http://a:1", vendor=ProtectionVendor.AKAMAI)
    assert pm._entries["http://a:1"].vendor_success["akamai"] == 2


# ── Task 12: scored selection ─────────────────────────────────────────────────

def test_next_no_args_is_round_robin() -> None:
    pm = _pm("http://a:1", "http://b:1")
    seen = {pm.next(), pm.next()}
    assert seen == {"http://a:1", "http://b:1"}


def test_next_prefers_proxy_with_domain_history() -> None:
    pm = _pm("http://a:1", "http://b:1")
    # Proxy B has proven itself on shop.com; A has not.
    for _ in range(5):
        pm.report_success("http://b:1", domain="shop.com", vendor=ProtectionVendor.CLOUDFLARE)
    # A generic (healthy) proxy exists, but B should win for shop.com.
    for _ in range(5):
        assert pm.next(domain="shop.com") == "http://b:1"


def test_next_prefers_proxy_with_vendor_history() -> None:
    pm = _pm("http://a:1", "http://b:1")
    for _ in range(4):
        pm.report_success("http://a:1", vendor=ProtectionVendor.DATADOME)
    for _ in range(4):
        assert pm.next(vendor=ProtectionVendor.DATADOME) == "http://a:1"


def test_next_avoids_hard_blocked_proxy_for_domain() -> None:
    pm = _pm("http://a:1", "http://b:1")
    # A got hard-blocked on shop.com; B succeeded there.
    for _ in range(3):
        pm.report_failure("http://a:1", domain="shop.com", hard_block=True, penalize=False)
        pm.report_success("http://b:1", domain="shop.com")
    assert pm.next(domain="shop.com") == "http://b:1"


def test_next_falls_back_to_round_robin_without_history() -> None:
    pm = _pm("http://a:1", "http://b:1")
    # No history for this domain → even rotation, not always the same proxy.
    picks = {pm.next(domain="fresh.com") for _ in range(6)}
    assert picks == {"http://a:1", "http://b:1"}


def test_next_raises_when_all_quarantined() -> None:
    pm = _pm("http://a:1", max_failures=1)
    pm.report_failure("http://a:1")
    with pytest.raises(NoProxiesAvailable):
        pm.next(domain="shop.com")
