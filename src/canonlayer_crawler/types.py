from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base model that serializes fields as camelCase while accepting snake_case on the Python side.

    The JSON shape must stay byte-compatible with @canonlayer/crawler's ScrapeResult /
    MapResult so the main app's provider can treat either backend as a drop-in.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ScrapeMetadata(CamelModel):
    title: str | None = None
    description: str | None = None
    og_image: str | None = None


class ScrapeResult(CamelModel):
    url: str
    status_code: int
    content_type: str = "text/html"
    raw_html: str
    markdown: str | None = None
    plain_text: str | None = None
    footer_text: str | None = None
    excerpt: str | None = None
    links: list[str] = Field(default_factory=list)
    metadata: ScrapeMetadata = Field(default_factory=ScrapeMetadata)


class ScrapeRequest(CamelModel):
    url: HttpUrl
    wait_for: int | None = Field(default=None, ge=0, le=10_000)


class MapRequest(CamelModel):
    url: HttpUrl
    max_pages: int | None = Field(default=None, ge=1, le=1_000)
    max_depth: int | None = Field(default=None, ge=0, le=10)
    max_concurrency: int | None = Field(default=None, ge=1, le=10)


class MapResult(CamelModel):
    urls: list[str]
    pages_visited: int
    # One of: "max_pages" | "queue_empty" | "timeout" | "error"
    stopped_reason: str
