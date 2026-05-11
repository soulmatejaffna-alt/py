from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request

# Crawl4AI's rich logger prints unicode (arrows, check marks). Windows consoles
# default to cp1252, which chokes on those — force stdout/stderr to UTF-8.
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")

# Windows: uvicorn's --reload mode installs SelectorEventLoopPolicy, which
# cannot spawn subprocesses → Playwright fails to launch Chromium with
# NotImplementedError. Force the Proactor policy before the event loop spins up.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Local dev: pick up CRAWLER_API_KEY from apps/crawler-py/.env. No-op in Docker/Fly
# where env vars are injected by the platform.
load_dotenv()

from .auth import bearer_auth
from .map import map_site
from .scrape import close_crawler, scrape_page
from .types import MapRequest, MapResult, ScrapeRequest, ScrapeResult

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("crawler-py")

_started_at = time.monotonic()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Release Chromium cleanly on shutdown — matters on platforms that send SIGTERM
    # during deploys (Fly.io, Railway).
    yield
    await close_crawler()


app = FastAPI(title="canonlayer-crawler-py", lifespan=lifespan)


@app.middleware("http")
async def request_logger(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    ms = int((time.monotonic() - started) * 1000)
    log.info(
        "[crawler-py] %s %s \u2192 %d (%dms)",
        request.method,
        request.url.path,
        response.status_code,
        ms,
    )
    return response


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "service": "canonlayer-crawler-py",
        "uptimeSeconds": int(time.monotonic() - _started_at),
    }


@app.get("/")
async def root() -> dict:
    return {"service": "canonlayer-crawler-py"}


@app.post(
    "/crawl",
    response_model=ScrapeResult,
    response_model_by_alias=True,
    dependencies=[Depends(bearer_auth)],
)
async def crawl(body: ScrapeRequest) -> ScrapeResult:
    log.info("[scrape] %s", body.url)
    result = await scrape_page(str(body.url), wait_for=body.wait_for)
    log.info(
        "[scrape] %s \u2192 %d (%d links, %d bytes)",
        body.url,
        result.status_code,
        len(result.links),
        len(result.raw_html),
    )
    return result


@app.post(
    "/map",
    response_model=MapResult,
    response_model_by_alias=True,
    dependencies=[Depends(bearer_auth)],
)
async def map_endpoint(body: MapRequest) -> MapResult:
    log.info(
        "[map] %s (maxPages=%s, depth=%s)",
        body.url,
        body.max_pages or 100,
        body.max_depth or 5,
    )
    result = await map_site(
        str(body.url),
        max_pages=body.max_pages,
        max_depth=body.max_depth,
        max_concurrency=body.max_concurrency,
    )
    log.info(
        "[map] %s \u2192 %d URLs (%s)",
        body.url,
        len(result.urls),
        result.stopped_reason,
    )
    return result
