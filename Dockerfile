# Playwright Python base — Chromium + system deps preinstalled.
# Keep this tag in lockstep with whatever `playwright` version crawl4ai resolves
# (it ships unpinned in requirements.txt). If they drift, Playwright can't find
# its browser ("Executable doesn't exist at /ms-playwright/chromium-XXXX/...")
# and every scrape 500s. The `playwright install chromium` below re-pins the
# browser binary to the installed package version, so a minor crawl4ai bump
# won't break the image even before this base tag is bumped.
FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---------- deps layer ----------
FROM base AS deps
COPY requirements.txt ./
RUN pip install -r requirements.txt \
 && playwright install chromium

# ---------- runtime layer ----------
FROM deps AS runtime
ENV PORT=8080
ENV PYTHONPATH=/app/src

COPY src ./src

EXPOSE 8080

CMD ["uvicorn", "canonlayer_crawler.main:app", "--host", "0.0.0.0", "--port", "8080"]
