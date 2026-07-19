"""Response caches its parsed tree so css()/xpath()/follow_links parse once."""

from __future__ import annotations

from anansi.core import Response


def test_css_shares_one_cached_soup() -> None:
    r = Response(
        url="http://e.com",
        status=200,
        html='<div class="x"><h1>Hi</h1><p class="x">y</p></div>',
    )
    first = r._soup
    assert r._soup is first  # cached_property returns the same tree every time
    assert r.css("h1")[0].get_text() == "Hi"
    assert len(r.css(".x")) == 2
    assert r._soup is first  # css() did not rebuild the tree


def test_follow_links_reuses_the_response_soup() -> None:
    from anansi.core import Request, Spider, rule

    class S(Spider):
        name = "s"

        @rule(r".*", follow=True)
        def parse(self, response):  # pragma: no cover - not invoked here
            yield None

    r = Response(url="http://e.com", status=200, html='<a href="/next">n</a>')
    soup_before = r._soup
    reqs = S().follow_links(r)
    assert [req.url for req in reqs] == ["http://e.com/next"]
    assert r._soup is soup_before  # follow_links used the cached tree, no re-parse


def test_xpath_returns_correct_nodes_and_caches() -> None:
    r = Response(
        url="http://e.com",
        status=200,
        html="<html><body><a href='/x'>L</a></body></html>",
    )
    assert r.xpath("//a/@href") == ["/x"]
    assert r.xpath("//a/@href") == ["/x"]  # cached tree, same result
