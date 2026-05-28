from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import redis.asyncio as aioredis

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.models.url import URL as URLModel
from app.schemas.url import URLCreateRequest, URLResponse
from app.services.url_service import URLService
from app.services.analytics_service import AnalyticsService

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
    redis: aioredis.Redis = Depends(get_redis),
):
    service = URLService(db, redis)
    try:
        return await service.create_short_url(request)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.get(
    "/analytics/{short_code}",
    summary="Get analytics for a short URL",
    tags=["Analytics"],
)
async def get_analytics(
    short_code: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    Returns rich analytics for a short code:
    - Total click count (DB + Redis buffer merged)
    - Rolling windows: last 24h, last 7d
    - Daily breakdown for the last 30 days
    - Last 10 individual click events with metadata
    """
    service = AnalyticsService(db, redis)
    data = await service.get_analytics(short_code)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short code '{short_code}' not found.",
        )
    return data


@router.get(
    "/{short_code}",
    summary="Redirect to original URL",
    tags=["Redirects"],
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
)
async def redirect_to_url(
    short_code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    The hot path — Redis cache checked first, PostgreSQL only on miss.
    Click recorded on every successful redirect.
    """
    service = URLService(db, redis)
    url = await service.get_url_by_code(short_code)

    if url is None:
        raw = await db.execute(
            select(URLModel).where(URLModel.short_code == short_code)
        )
        raw_url = raw.scalar_one_or_none()
        if raw_url is not None and raw_url.is_expired:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"This link expired on {raw_url.expires_at.strftime('%Y-%m-%d %H:%M UTC')}.",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short code '{short_code}' not found.",
        )

    ip_address = request.headers.get(
        "X-Forwarded-For",
        request.client.host if request.client else None,
    )
    if ip_address and "," in ip_address:
        ip_address = ip_address.split(",")[0].strip()

    await service.record_click(
        url=url,
        ip_address=ip_address,
        user_agent=request.headers.get("User-Agent"),
        referer=request.headers.get("Referer"),
    )

    return RedirectResponse(url=url.original_url, status_code=status.HTTP_302_FOUND)


@router.delete(
    "/urls/{short_code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a short URL",
    tags=["URLs"],
)
async def delete_url(
    short_code: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Soft-delete a URL and evict its Redis cache entry."""
    service = URLService(db, redis)
    if not await service.delete_url(short_code):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short code '{short_code}' not found or already deleted.",
        )