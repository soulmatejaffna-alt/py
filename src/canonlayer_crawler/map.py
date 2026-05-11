from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from crawl4ai import CacheMode, CrawlerRunConfig

from .same_origin import is_same_site
from .scrape import get_crawler
from .types import MapResult

log = logging.getLogger("crawler-py.map")


async def map_site(
    source_url: str,
    max_pages: int | None = None,
    max_depth: int | None = None,
    max_concurrency: int | None = None,
) -> MapResult:
    """BFS same-origin URL discovery using Crawl4AI.

    Equivalent to the Node variant's mapSite() — expands the frontier level
    by level, fetching pages in parallel up to max_concurrency. We only need
    links from each page, so we skip scan_full_page to keep it fast.
    """
    mp = max_pages or 100
    md = max_depth or 5
    mc = max_concurrency or int(os.environ.get("CRAWLER_MAX_CONCURRENCY", "3"))

    crawler = await get_crawler()

    seen: set[str] = {source_url}
    found: list[str] = [source_url]
    frontier: list[tuple[str, int]] = [(source_url, 0)]
    stopped_reason: str = "queue_empty"

    semaphore = asyncio.Semaphore(mc)

    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=20_000,
        scan_full_page=False,
    )

    async def fetch_links(url: str) -> list[str]:
        async with semaphore:
            try:
                result: Any = await crawler.arun(url=url, config=run_cfg)
                if not getattr(result, "success", True):
                    return []
                links_dict = getattr(result, "links", None) or {}
                internal = (
                    links_dict.get("internal", []) if isinstance(links_dict, dict) else []
                )
                return [
                    link["href"]
                    for link in internal
                    if isinstance(link, dict) and link.get("href")
                ]
            except Exception as err:
                log.warning("[map] fetch failed for %s: %s", url, err)
                return []

    while frontier and len(found) < mp:
        current = [(u, d) for (u, d) in frontier if d < md]
        frontier = []
        if not current:
            break

        results = await asyncio.gather(*(fetch_links(u) for (u, _) in current))

        reached_cap = False
        for (_, depth), links in zip(current, results):
            for link in links:
                if len(found) >= mp:
                    reached_cap = True
                    break
                if link.startswith("javascript:") or link.startswith("mailto:"):
                    continue
                clean = link.split("#")[0]
                if not clean or clean in seen:
                    continue
                if not is_same_site(clean, source_url):
                    continue
                seen.add(clean)
                found.append(clean)
                frontier.append((clean, depth + 1))
            if reached_cap:
                break

        if reached_cap:
            stopped_reason = "max_pages"
            break

    final_reason = "max_pages" if len(found) >= mp else stopped_reason

    return MapResult(
        urls=found,
        pages_visited=len(found),
        stopped_reason=final_reason,
    )
