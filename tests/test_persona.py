"""Tests for the coherent Persona model."""

from __future__ import annotations

from anansi.persona import Persona, build_persona


def test_build_persona_returns_persona() -> None:
    p = build_persona()
    assert isinstance(p, Persona)


def test_screen_matches_or_exceeds_viewport() -> None:
    """A viewport can never be larger than the physical screen it lives on."""
    for seed in range(25):
        p = build_persona(seed=seed)
        assert p.screen["width"] >= p.viewport["width"]
        assert p.screen["height"] >= p.viewport["height"]


def test_locale_and_timezone_present() -> None:
    p = build_persona(seed=1)
    assert p.locale
    assert p.timezone_id
    assert p.accept_language
    # accept_language should advertise the persona's locale
    assert p.locale.split("-")[0] in p.accept_language


def test_deterministic_seed_returns_same_persona() -> None:
    a = build_persona(seed=42)
    b = build_persona(seed=42)
    assert a == b


def test_different_seeds_can_differ() -> None:
    personas = {build_persona(seed=s) for s in range(30)}
    # Not all 30 seeds should collapse to a single persona.
    assert len(personas) > 1


def test_mobile_persona_is_coherent() -> None:
    """Mobile personas must not present desktop-only fingerprint combos."""
    p = build_persona(seed=7, mobile=True)
    assert p.mobile is True
    # Real touch devices report >= 1 touch point.
    assert p.max_touch_points >= 1
    # Mobile viewports are portrait-ish and narrow, never a 2560-wide desktop.
    assert p.viewport["width"] <= 900
    # Mobile UA should not claim a desktop platform.
    assert p.platform not in ("Win32", "MacIntel")


def test_desktop_persona_is_coherent() -> None:
    p = build_persona(seed=8, mobile=False)
    assert p.mobile is False
    assert p.max_touch_points == 0
    assert p.viewport["width"] >= 1000


def test_persona_is_frozen() -> None:
    p = build_persona(seed=3)
    try:
        p.user_agent = "x"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Persona should be immutable (frozen)")


def test_persona_fields_present() -> None:
    p = build_persona(seed=5)
    for field in (
        "user_agent", "ua_family", "platform", "viewport", "screen",
        "locale", "timezone_id", "accept_language", "hardware_concurrency",
        "device_memory", "webgl_vendor", "webgl_renderer", "max_touch_points",
        "mobile",
    ):
        assert getattr(p, field) is not None


def test_ua_family_matches_user_agent() -> None:
    for seed in range(20):
        p = build_persona(seed=seed)
        ua = p.user_agent.lower()
        if p.ua_family == "chrome":
            assert "chrome" in ua
        elif p.ua_family == "firefox":
            assert "firefox" in ua
        elif p.ua_family == "safari":
            assert "safari" in ua
