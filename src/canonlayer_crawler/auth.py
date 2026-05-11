from __future__ import annotations

import os

from fastapi import Header, HTTPException, status


async def bearer_auth(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token dependency. Requires `Authorization: Bearer <CRAWLER_API_KEY>`.

    Fails closed when the env var is missing so prod can't run open by accident —
    same posture as the Node variant's bearerAuth middleware.
    """
    expected = os.environ.get("CRAWLER_API_KEY")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CRAWLER_API_KEY not configured on server",
        )

    provided = ""
    if authorization and authorization.startswith("Bearer "):
        provided = authorization[len("Bearer ") :]

    if provided != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )
