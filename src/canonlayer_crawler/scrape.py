from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from .same_origin import is_same_site
from .types import ScrapeMetadata, ScrapeResult

log = logging.getLogger("crawler-py.scrape")

# Desktop Chrome UA — Crawl4AI's built-in anti-bot tricks handle most cases,
# so we don't need the UA-rotation retry the Node variant does.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_shared_crawler: AsyncWebCrawler | None = None
_crawler_lock = asyncio.Lock()


# JavaScript we inject after page-load to make counters, fades, and scroll-triggered
# animations settle deterministically. Three moves:
#   1. Read `data-target` / `data-count` / `data-value` / `data-to` attrs — most
#      count-up libraries (CountUp.js, odometer, jQuery counter) store the final
#      value there. Copy it into textContent so Claude sees the real number.
#   2. Kill CSS animation/transition duration — fade-ins, slide-ins complete
#      instantly, revealing any hidden-until-visible content.
#   3. Dispatch scroll + intersection events so IntersectionObserver-based
#      animations fire and finish.
_ANIMATION_SETTLE_JS = [
    """
    (() => {
      // 1. Force-complete counters — pull final values from common data attrs.
      //    Includes Elementor's `data-to-value` (widely used on WP sites) and
      //    Elementor's delimiter attr so "90000" stays formatted as "90,000".
      const counterAttrs = ['data-to-value', 'data-target', 'data-count',
                            'data-value', 'data-to', 'data-number'];
      document.querySelectorAll(counterAttrs.map(a => `[${a}]`).join(',')).forEach(el => {
        for (const attr of counterAttrs) {
          const v = el.getAttribute(attr);
          if (!v || !v.trim()) continue;
          const delimiter = el.getAttribute('data-delimiter');
          // Re-insert thousands separator if the widget defines one.
          if (delimiter && /^\d+$/.test(v.trim())) {
            const n = Number(v.trim());
            if (Number.isFinite(n)) {
              el.textContent = n.toLocaleString('en-US').replace(/,/g, delimiter);
              break;
            }
          }
          el.textContent = v;
          break;
        }
      });

      // 1b. Collapse Elementor counter wrappers — combine prefix+number+suffix
      //     into one textContent so markdown sees "25+" not "25 +" (the three
      //     spans render visually adjacent but the generator inserts whitespace
      //     between them). Same for CountUp / odometer / generic counter libs.
      const wrappers = document.querySelectorAll(
        '.elementor-counter-number-wrapper, .counter-wrapper, .count-up-wrapper, .odometer'
      );
      wrappers.forEach(w => {
        const combined = Array.from(w.children)
          .map(c => (c.textContent || '').trim())
          .filter(Boolean)
          .join('');
        if (combined) {
          // Replace the wrapper's inner nodes with a single text node so the
          // markdown generator doesn't re-insert whitespace between children.
          while (w.firstChild) w.removeChild(w.firstChild);
          w.textContent = combined;
        }
      });

      // 2. Kill CSS animations + transitions for a clean snapshot.
      const css = document.createElement('style');
      css.textContent = '*, *::before, *::after {' +
        'animation-duration: 0.01s !important;' +
        'animation-delay: 0s !important;' +
        'transition-duration: 0.01s !important;' +
        'transition-delay: 0s !important;' +
        '}';
      document.head.appendChild(css);

      // 3. Nudge any IntersectionObserver-based animations to fire.
      window.dispatchEvent(new Event('scroll'));
      window.dispatchEvent(new Event('resize'));
    })();
    """
]


async def get_crawler() -> AsyncWebCrawler:
    """Lazy-init a single Chromium instance and reuse it across requests.

    Saves ~1-2s per call vs relaunching the browser each time — same trick as
    the Node variant's sharedBrowser.
    """
    global _shared_crawler
    if _shared_crawler is not None:
        return _shared_crawler

    async with _crawler_lock:
        if _shared_crawler is not None:
            return _shared_crawler

        headless = os.environ.get("CRAWLER_HEADLESS", "true").lower() != "false"
        browser_cfg = BrowserConfig(
            headless=headless,
            user_agent=DEFAULT_USER_AGENT,
            # Hide common bot-detection signals: spoofs navigator.webdriver, plugin
            # list, languages, platform string. Helps with Cloudflare-style challenges
            # without resorting to a paid proxy.
            user_agent_mode="random",
            extra_args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            viewport_width=1280,
            viewport_height=900,
        )
        crawler = AsyncWebCrawler(config=browser_cfg)
        await crawler.start()
        _shared_crawler = crawler
        return crawler


async def close_crawler() -> None:
    global _shared_crawler
    if _shared_crawler is not None:
        try:
            await _shared_crawler.close()
        except Exception:
            pass
        _shared_crawler = None


async def scrape_page(url: str, wait_for: int | None = None) -> ScrapeResult:
    """Scrape a single URL with two attempts + backoff."""
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            return await _scrape_once(url, wait_for)
        except Exception as err:
            last_err = err
            log.warning("[scrape] attempt %d failed for %s: %s", attempt + 1, url, err)
            await asyncio.sleep(1.5)
    raise last_err if last_err else RuntimeError("Scrape failed after all retries")


async def _scrape_once(url: str, wait_for: int | None) -> ScrapeResult:
    crawler = await get_crawler()

    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=60_000,
        # Stealth/anti-detection — Crawl4AI's "magic" mode flips the standard set
        # of bot-evasion tricks (mouse-movement simulation, webdriver flag override,
        # randomized fingerprint). Free, often gets through Cloudflare's "checking
        # your browser" interstitials that block plain Playwright.
        magic=True,
        simulate_user=True,
        override_navigator=True,
        # Auto-scroll to trigger lazy-loaded content (cards, images, feeds).
        scan_full_page=True,
        scroll_delay=0.3,
        # Settle delay AFTER scrolling + JS injection. 4s covers most JS counters,
        # fade-ins, and IntersectionObserver callbacks. Tune via waitFor on a
        # site-specific basis if needed.
        delay_before_return_html=(wait_for / 1000) if wait_for else 4.0,
        # Force-complete common UI animations so counters and lazy widgets
        # settle at their final values BEFORE we capture the DOM.
        js_code=_ANIMATION_SETTLE_JS,
        # Populate `fit_markdown` by stripping nav/footer/sidebar boilerplate.
        # Without a content_filter the generator leaves fit_markdown empty and
        # _extract_markdown falls back to raw_markdown (full page incl. nav trees),
        # which bloats downstream AI extraction and produces thin results.
        #
        # Threshold 0.30 (was 0.48) keeps short but important nodes like
        # "25+" stat blocks and "+94 11 4777888" hotline pills that were
        # getting dropped as "low content value" at 0.48.
        markdown_generator=DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(threshold=0.30, threshold_type="fixed"),
        ),
        # Keep `header` and `aside` intentionally — they often hold sticky contact
        # bars (phone numbers, hours, social links) that extraction needs. The LLM
        # is better placed than a heuristic to decide what's chrome vs content.
        excluded_tags=["nav", "footer", "script", "style", "noscript"],
        # word_count_threshold of 10 dropped short-but-important blocks like
        # "+94 11 4777888 | Hotline" (6 words). Lower to 3.
        word_count_threshold=3,
    )

    result: Any = await crawler.arun(url=url, config=run_cfg)

    if not getattr(result, "success", True):
        err = getattr(result, "error_message", "unknown error")
        raise RuntimeError(f"Crawl failed: {err}")

    status = int(getattr(result, "status_code", 0) or 0)
    # Fail fast on hard blocks so the outer retry has a chance to recover.
    if status == 403 or status == 429 or status >= 500:
        raise RuntimeError(f"HTTP {status} — likely blocked or rate-limited")

    raw_html = getattr(result, "html", "") or ""
    markdown = _extract_markdown(result)
    content_type = _header(result, "content-type") or "text/html"

    footer_text, plain_text = _extract_footer_and_plain(raw_html)

    links_dict = getattr(result, "links", None) or {}
    internal = links_dict.get("internal", []) if isinstance(links_dict, dict) else []
    raw_links = [
        link.get("href")
        for link in internal
        if isinstance(link, dict) and link.get("href")
    ]
    links = _dedupe_same_origin([link for link in raw_links if link], url)

    meta_raw = getattr(result, "metadata", None) or {}
    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
    metadata = ScrapeMetadata(
        title=meta.get("title"),
        description=meta.get("description"),
        og_image=meta.get("og:image") or meta.get("og_image"),
    )

    return ScrapeResult(
        url=getattr(result, "url", url) or url,
        status_code=status or 200,
        content_type=content_type,
        raw_html=raw_html,
        markdown=markdown or None,
        plain_text=plain_text or None,
        footer_text=footer_text or None,
        links=links,
        metadata=metadata,
    )


def _extract_markdown(result: Any) -> str:
    """Crawl4AI's markdown field moved from str → MarkdownGenerationResult across versions.

    Accept either shape. Prefer `fit_markdown` (boilerplate-filtered) over `raw_markdown`.
    """
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    for attr in ("fit_markdown", "raw_markdown"):
        value = getattr(md, attr, None)
        if value:
            return str(value).strip()
    if isinstance(md, str):
        return md.strip()
    return ""


def _header(result: Any, name: str) -> str | None:
    headers = getattr(result, "response_headers", None) or getattr(result, "headers", None)
    if isinstance(headers, dict):
        for k, v in headers.items():
            if k.lower() == name.lower():
                return str(v)
    return None


def _extract_footer_and_plain(raw_html: str) -> tuple[str, str]:
    """Pull <footer> text separately (phone/address often live there) and
    derive a plain-text body after stripping nav/aside/scripts — parity with
    the Node variant's cleanHtml output."""
    if not raw_html:
        return "", ""
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
    except Exception:
        return "", ""

    footer_text = "\n\n".join(
        (f.get_text(strip=True) for f in soup.find_all("footer"))
    ).strip()

    body = soup.body
    plain = ""
    if body is not None:
        # Don't strip <header>/<aside> here — contact bars often live in them.
        # `nav` and `footer` are still excluded: nav is noise for extraction,
        # footer is captured separately (above) so it survives any budget cuts.
        for tag in body.select(
            "script, style, noscript, iframe, canvas, video, audio, nav, footer"
        ):
            tag.decompose()
        plain = body.get_text("\n", strip=True)
        plain = re.sub(r"\n{3,}", "\n\n", plain)

    return footer_text, plain


def _dedupe_same_origin(links: list[str], base_url: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for link in links:
        if link.startswith("javascript:") or link.startswith("mailto:"):
            continue
        if not is_same_site(link, base_url):
            continue
        clean = link.split("#")[0]
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out
