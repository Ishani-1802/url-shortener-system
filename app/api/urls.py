from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.url import URLCreateRequest, URLResponse
from app.services.url_service import URLService

router = APIRouter()


@router.post(
    "/shorten",
    response_model=URLResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Shorten a URL",
    tags=["URLs"],
)
async def shorten_url(
    request: URLCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Shorten a long URL into a compact short code.
    Deduplicates identical URLs. Supports custom aliases and expiry.
    """
    service = URLService(db)
    try:
        return await service.create_short_url(request)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.get(
    "/{short_code}",
    summary="Redirect to original URL",
    tags=["Redirects"],
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
)
async def redirect_to_url(
    short_code: str,
    request: Request,                       # FastAPI injects the raw HTTP request
    db: AsyncSession = Depends(get_db),
):
    """
    The most critical endpoint in the system.
    Every millisecond here matters at scale.

    Flow:
      1. Look up short_code in DB (Redis cache in Phase 5)
      2. Check expiry
      3. Record the click
      4. Return 302 redirect
    """
    service = URLService(db)
    url = await service.get_url_by_code(short_code)

    if url is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short URL '{short_code}' not found.",
        )

    # Separately check if the URL was found but is expired
    # (get_url_by_code returns None for both, so we need a raw lookup for 410)
    # Re-query to distinguish 404 vs 410
    from app.models.url import URL as URLModel
    from sqlalchemy import select

    raw = await db.execute(
        select(URLModel).where(URLModel.short_code == short_code)
    )
    raw_url = raw.scalar_one_or_none()

    if raw_url is not None and raw_url.is_expired:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"This short URL expired on {raw_url.expires_at.strftime('%Y-%m-%d %H:%M UTC')}.",
        )

    # Extract client metadata for analytics
    # X-Forwarded-For is the real IP when behind a load balancer / Nginx
    ip_address = request.headers.get(
        "X-Forwarded-For",
        request.client.host if request.client else None,
    )
    # Only take the first IP if the header contains a chain like "1.2.3.4, 5.6.7.8"
    if ip_address and "," in ip_address:
        ip_address = ip_address.split(",")[0].strip()

    user_agent = request.headers.get("User-Agent")
    referer = request.headers.get("Referer")

    # Record the click — analytics write happens before redirect
    await service.record_click(
        url=url,
        ip_address=ip_address,
        user_agent=user_agent,
        referer=referer,
    )

    # FastAPI's RedirectResponse sends the 302 with Location header
    return RedirectResponse(
        url=url.original_url,
        status_code=status.HTTP_302_FOUND,
    )


@router.get(
    "/analytics/{short_code}",
    summary="Get analytics for a short URL",
    tags=["Analytics"],
)
async def get_analytics(
    short_code: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns click count, recent clicks, and metadata for a short code.
    """
    service = URLService(db)
    data = await service.get_analytics(short_code)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short code '{short_code}' not found.",
        )
    return data


@router.delete(
    "/urls/{short_code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a short URL",
    tags=["URLs"],
)
async def delete_url(
    short_code: str,
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a short URL. The record stays in the database."""
    service = URLService(db)
    deleted = await service.delete_url(short_code)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short code '{short_code}' not found or already deleted.",
        )