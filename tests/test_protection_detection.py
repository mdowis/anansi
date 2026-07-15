"""Tests for the shared protection classifier."""

from __future__ import annotations

from anansi.protection import (
    ProtectionKind,
    ProtectionVendor,
    detect_protection,
)


def test_cloudflare_challenge_page() -> None:
    html = (
        "<html><head><title>Just a moment...</title></head>"
        "<body><div class='cf-turnstile'></div>"
        "<script src='/cdn-cgi/challenge-platform/'></script></body></html>"
    )
    d = detect_protection(html, 403, {"server": "cloudflare"})
    assert d.vendor is ProtectionVendor.CLOUDFLARE
    assert d.kind is ProtectionKind.CHALLENGE
    assert d.needs_browser is True
    assert d.is_hard_block is False


def test_cloudflare_1020_block() -> None:
    html = (
        "<html><body><h1>Sorry, you have been blocked</h1>"
        "<p>Error 1020</p><p>Cloudflare Ray ID: abc123</p></body></html>"
    )
    d = detect_protection(html, 403, {"server": "cloudflare"})
    assert d.vendor is ProtectionVendor.CLOUDFLARE
    assert d.kind is ProtectionKind.BLOCK
    assert d.is_hard_block is True
    assert d.needs_browser is False


def test_cloudflare_bare_edge_error_is_challenge() -> None:
    """A 503 from the CF edge with no body marker is still worth a browser."""
    d = detect_protection("", 503, {"server": "cloudflare", "cf-ray": "xyz"})
    assert d.vendor is ProtectionVendor.CLOUDFLARE
    assert d.kind is ProtectionKind.CHALLENGE


def test_akamai_body_markers() -> None:
    html = "<html><body>Reference #18.abcd errors.edgesuite.net</body></html>"
    d = detect_protection(html, 403, {})
    assert d.vendor is ProtectionVendor.AKAMAI
    assert d.kind is ProtectionKind.BLOCK


def test_akamai_server_header() -> None:
    d = detect_protection("<html>blocked</html>", 403, {"Server": "AkamaiGHost"})
    assert d.vendor is ProtectionVendor.AKAMAI
    assert d.kind is ProtectionKind.BLOCK


def test_datadome_cookie_header() -> None:
    d = detect_protection(
        "<html>please verify</html>", 403,
        {"set-cookie": "datadome=xyz; Path=/"},
    )
    assert d.vendor is ProtectionVendor.DATADOME
    assert d.kind is ProtectionKind.CAPTCHA


def test_datadome_cookies_map() -> None:
    d = detect_protection(
        "<html>ok</html>", 200, {}, cookies={"datadome": "abc"},
    )
    assert d.vendor is ProtectionVendor.DATADOME


def test_datadome_body_marker() -> None:
    html = "<html><body>captcha-delivery.com challenge</body></html>"
    d = detect_protection(html, 403, {})
    assert d.vendor is ProtectionVendor.DATADOME
    assert d.kind is ProtectionKind.CAPTCHA


def test_generic_recaptcha_is_captcha() -> None:
    html = "<html><body><div class='g-recaptcha'></div></body></html>"
    d = detect_protection(html, 200, {})
    assert d.kind is ProtectionKind.CAPTCHA
    assert d.vendor is ProtectionVendor.UNKNOWN


def test_plain_html_is_none() -> None:
    html = "<html><body>" + ("Hello world. " * 200) + "</body></html>"
    d = detect_protection(html, 200, {"content-type": "text/html"})
    assert d.vendor is ProtectionVendor.NONE
    assert d.kind is ProtectionKind.NONE
    assert d.is_protected is False


def test_js_shell_detected() -> None:
    html = (
        '<html><head><title>App</title></head><body>'
        '<div id="__next"></div>'
        '<script src="/_next/static/app.js"></script></body></html>'
    )
    d = detect_protection(html, 200, {})
    assert d.vendor is ProtectionVendor.NONE
    assert d.kind is ProtectionKind.JS_SHELL
    assert d.needs_browser is True


def test_case_insensitive_headers() -> None:
    d = detect_protection("Just a moment cf-turnstile", 403, {"SeRvEr": "CloudFlare"})
    assert d.vendor is ProtectionVendor.CLOUDFLARE
