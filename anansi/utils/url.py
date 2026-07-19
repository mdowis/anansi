"""URL canonicalization for deduplication in the URL queue.

``canonicalize_url`` is a pure function — safe to call on every URL before
enqueueing. It normalises scheme/host casing, removes tracking parameters,
sorts remaining query parameters, and strips fragments so that URL variants
pointing to the same content hash to the same canonical form.
"""

from __future__ import annotations

import posixpath
from urllib.parse import (
    ParseResult,
    parse_qsl,
    urlencode,
    urlparse,
    urlunparse,
)

# Parameters that identify traffic sources, ad campaigns, or analytics but
# carry no information about the page's content. Strip these unconditionally.
_TRACKING_PARAMS: frozenset[str] = frozenset({
    # Google Analytics / Google Ads
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    "_ga", "_gl", "gclid", "gclsrc", "dclid",
    # Meta / Facebook
    "fbclid", "fb_action_ids", "fb_action_types", "fb_source",
    # Microsoft / Bing
    "msclkid",
    # Mailchimp
    "mc_cid", "mc_eid",
    # Yandex
    "yclid", "_openstat",
    # Twitter / X
    "twclid",
    # Instagram
    "igshid",
    # HubSpot
    "hsa_acc", "hsa_cam", "hsa_grp", "hsa_ad", "hsa_src", "hsa_tgt",
    "hsa_kw", "hsa_mt", "hsa_net", "hsa_ver",
    # Generic referral/tracking
    "ref", "referrer", "source", "origin",
})

# Default ports that are redundant in a URL and should be stripped.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def canonicalize_url(
    url: str,
    *,
    keep_trailing_slash: bool = False,
    extra_strip_params: tuple[str, ...] | list[str] = (),
) -> str:
    """Return the canonical form of *url* for deduplication purposes.

    Operations applied (in order):
    1. Lowercase scheme and host.
    2. Strip default ports (:80 for http, :443 for https).
    3. Normalise path (collapse ``//``, resolve ``.`` and ``..``).
    4. Remove trailing slash from non-root paths (unless *keep_trailing_slash*).
    5. Strip known tracking/analytics query parameters (see ``_TRACKING_PARAMS``).
    6. Strip any additional parameters listed in *extra_strip_params*.
    7. Sort remaining query parameters alphabetically.
    8. Strip fragment (``#...``) entirely.

    Args:
        url: The raw URL string to canonicalise.
        keep_trailing_slash: If True, do not remove a trailing ``/`` from paths.
        extra_strip_params: Additional query parameter names to strip beyond the
            built-in tracking list (useful for site-specific junk params).

    Returns:
        The canonical URL string. Returns *url* unchanged if it cannot be parsed.
    """
    try:
        parsed: ParseResult = urlparse(url)
    except Exception:
        return url

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Strip default ports from netloc
    if ":" in netloc:
        host, _, port_str = netloc.rpartition(":")
        try:
            port = int(port_str)
            if _DEFAULT_PORTS.get(scheme) == port:
                netloc = host
        except ValueError:
            pass

    # Normalise path
    path = posixpath.normpath(parsed.path) if parsed.path else "/"
    # posixpath.normpath strips trailing slash; restore for root
    if not path:
        path = "/"
    # Re-add trailing slash if the original had one and keep_trailing_slash is set
    if keep_trailing_slash and parsed.path.endswith("/") and not path.endswith("/"):
        path += "/"
    # Remove trailing slash from non-root paths (default behaviour)
    if not keep_trailing_slash and path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Filter and sort query parameters
    strip = (
        _TRACKING_PARAMS
        if not extra_strip_params
        else _TRACKING_PARAMS | frozenset(extra_strip_params)
    )
    params = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k not in strip
    ]
    params.sort(key=lambda kv: kv[0])
    query = urlencode(params)

    # Reconstruct — fragment always stripped
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))
