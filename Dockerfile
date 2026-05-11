# Playwright Python base — Chromium + system deps preinstalled.
# Matches the version pin used by the Node variant so both images ship the same Chromium.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---------- deps layer ----------
FROM base AS deps
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ---------- runtime layer ----------
FROM deps AS runtime
ENV PORT=8080
ENV PYTHONPATH=/app/src

COPY src ./src

EXPOSE 8080

CMD ["uvicorn", "canonlayer_crawler.main:app", "--host", "0.0.0.0", "--port", "8080"]
