"""CLI entry-point for Anansi."""

from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anansi",
        description="Arachne — adaptive web scraping framework",
    )
    sub = parser.add_subparsers(dest="command")

    # fetch subcommand
    fetch_p = sub.add_parser("fetch", help="Fetch a single URL")
    fetch_p.add_argument("url")
    fetch_p.add_argument("--browser", action="store_true", help="Use headless browser")
    fetch_p.add_argument("--proxy", help="Proxy URL")
    fetch_p.add_argument("--output", choices=["html", "text", "markdown"], default="html")
    fetch_p.add_argument(
        "--bot-profile",
        help="Present as a known crawler (e.g. 'googlebot', 'googlebot-mobile')",
    )
    fetch_p.add_argument(
        "--as-googlebot",
        action="store_true",
        help="Shorthand for --bot-profile googlebot",
    )

    # mcp subcommand
    sub.add_parser("mcp", help="Start the MCP server")

    # crawls subcommand
    sub.add_parser("crawls", help="List all crawls")

    args = parser.parse_args()

    if args.command == "fetch":
        asyncio.run(_cmd_fetch(args))
    elif args.command == "mcp":
        from anansi.mcp_server.server import run
        run()
    elif args.command == "crawls":
        asyncio.run(_cmd_crawls())
    else:
        parser.print_help()
        sys.exit(1)


async def _cmd_fetch(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.syntax import Syntax

    console = Console()

    bot_profile = "googlebot" if getattr(args, "as_googlebot", False) else args.bot_profile

    from anansi.bot_profiles import UnknownBotProfileError, get_profile
    try:
        get_profile(bot_profile)  # fail fast on a bad --bot-profile name
    except UnknownBotProfileError as exc:
        console.print(f"[bold red]error:[/] {exc}")
        sys.exit(1)

    if args.browser:
        from anansi.fetchers.browser import BrowserFetcher
        async with BrowserFetcher(bot_profile=bot_profile) as fetcher:
            result = await fetcher.fetch(args.url, proxy=args.proxy)
    else:
        from anansi.fetchers.http import HTTPFetcher
        async with HTTPFetcher(bot_profile=bot_profile) as fetcher:
            result = await fetcher.fetch(args.url, proxy=args.proxy)

    console.print(f"[bold green]HTTP {result.status}[/] {result.url}  [{result.elapsed:.2f}s]")

    if args.output == "html":
        console.print(Syntax(result.html[:4000], "html", theme="monokai"))
    elif args.output == "markdown":
        import markdownify
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(result.html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        md = markdownify.markdownify(str(soup), heading_style="ATX", strip=["a"]).strip()
        console.print(md[:4000])
    else:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(result.html, "lxml").get_text(separator="\n", strip=True)
        console.print(text[:4000])


async def _cmd_crawls() -> None:
    from rich.console import Console
    from rich.table import Table
    from anansi.spider.crawler import Crawler

    crawls = await Crawler.list_crawls()
    console = Console()
    table = Table(title="Arachne Crawls")
    table.add_column("Crawl ID", style="cyan")
    table.add_column("Spider")
    table.add_column("State")
    table.add_column("Items")
    table.add_column("Updated")

    for c in crawls:
        state_color = {"running": "green", "paused": "yellow", "finished": "blue", "error": "red"}.get(c["state"], "white")
        table.add_row(
            c["crawl_id"][:16] + "…",
            c["spider_name"],
            f"[{state_color}]{c['state']}[/]",
            str(c["items_count"]),
            c["updated_at"],
        )

    console.print(table)


if __name__ == "__main__":
    main()
