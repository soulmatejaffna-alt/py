from __future__ import annotations

from urllib.parse import urlparse


def base_domain_of(url: str) -> str | None:
    """Approximate the registrable domain without a PSL lookup.

    Mirrors baseDomainOf() in the Node variant: strip `www.`, return the rest of the host.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def is_same_site(candidate_url: str, base_url: str) -> bool:
    base = base_domain_of(base_url)
    cand = base_domain_of(candidate_url)
    if not base or not cand:
        return False
    return cand == base or cand.endswith(f".{base}")
