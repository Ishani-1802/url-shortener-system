from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.shortcode import generate_short_code, MAX_RETRIES
from app.models.url import URL, Click
from app.schemas.url import URLCreateRequest, URLResponse
from app.services.cache_service import CacheService

settings = get_settings()


class URLService:
    """
    Business logic layer.
    Now accepts both a DB session and a Redis client.
    Cache is checked before every DB read on the hot redirect path.
    """

    def __init__(self, db: AsyncSession, redis: Optional[aioredis.Redis] = None):
        self.db = db
        self.cache = CacheService(redis) if redis else None

    # ─── Create short URL ─────────────────────────────────────────────────

    async def create_short_url(self, request: URLCreateRequest) -> URLResponse:
        """
        Shorten a URL. After saving to DB, prime the cache immediately
        so the first redirect is a cache hit.
        """
        original_url = str(request.long_url)

        if not request.custom_alias:
            existing = await self._find_by_original_url(original_url)
            if existing and existing.is_active and not existing.is_expired:
                return self._build_response(existing)

        if request.custom_alias:
            short_code = await self._handle_custom_alias(request.custom_alias)
        else:
            short_code = await self._generate_unique_code()

        expires_at = None
        if request.expires_in_hours:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=request.expires_in_hours)

        url_record = URL(
            short_code=short_code,
            original_url=original_url,
            is_custom_alias=bool(request.custom_alias),
            expires_at=expires_at,
            is_active=True,
            click_count=0,
        )
        self.db.add(url_record)
        await self.db.flush()
        await self.db.refresh(url_record)

        # Prime the cache immediately — first redirect will be a hit
        if self.cache:
            await self.cache.cache_url(short_code, original_url)

        return self._build_response(url_record)

    # ─── Redirect lookup (the hot path) ──────────────────────────────────

    async def get_url_by_code(self, short_code: str) -> Optional[URL]:
        """
        The most called method in the entire system.

        Order of operations:
        1. Check Redis → return immediately if found (~1ms)
        2. Miss → query PostgreSQL (~15ms)
        3. Re-prime Redis so next request hits cache
        4. Return URL object
        """
        # Step 1: Cache lookup
        if self.cache:
            cached_url = await self.cache.get_cached_url(short_code)
            if cached_url:
                # Cache hit — build a minimal URL object to return
                # We need the full object for analytics; fetch from DB but don't block redirect
                result = await self.db.execute(
                    select(URL).where(
                        URL.short_code == short_code,
                        URL.is_active == True,   # noqa: E712
                    )
                )
                url = result.scalar_one_or_none()
                return url if url and not url.is_expired else None

        # Step 2: Cache miss — query PostgreSQL
        result = await self.db.execute(
            select(URL).where(
                URL.short_code == short_code,
                URL.is_active == True,           # noqa: E712
            )
        )
        url = result.scalar_one_or_none()

        if url is None or url.is_expired:
            return None

        # Step 3: Re-prime cache for next request
        if self.cache:
            await self.cache.cache_url(short_code, url.original_url)

        return url

    # ─── Click recording ──────────────────────────────────────────────────

    async def record_click(
        self,
        url: URL,
        ip_address: Optional[str],
        user_agent: Optional[str],
        referer: Optional[str],
    ) -> None:
        """
        Record a click.

        With Redis: increment atomic counter in Redis (non-blocking, ~0.5ms).
                    Write analytics row to PostgreSQL.
        Without Redis: increment counter directly on URL row + write click row.
        """
        # Write the analytics click row to PostgreSQL always
        click = Click(
            url_id=url.id,
            short_code=url.short_code,
            ip_address=ip_address,
            user_agent=user_agent,
            referer=referer,
        )
        self.db.add(click)

        if self.cache:
            # Buffer the count in Redis — flush to PostgreSQL periodically
            await self.cache.increment_click(url.short_code)
        else:
            # Fallback: direct DB increment
            url.click_count += 1

        await self.db.flush()

    # ─── Delete URL ───────────────────────────────────────────────────────

    async def delete_url(self, short_code: str) -> bool:
        """
        Soft-delete + cache invalidation.
        Must evict Redis entry or deleted URLs keep redirecting.
        """
        result = await self.db.execute(
            select(URL).where(
                URL.short_code == short_code,
                URL.is_active == True,           # noqa: E712
            )
        )
        url = result.scalar_one_or_none()
        if not url:
            return False

        url.is_active = False
        await self.db.flush()

        # Critical: remove from cache so next request gets 404
        if self.cache:
            await self.cache.invalidate_url(short_code)

        return True

    # ─── Analytics ────────────────────────────────────────────────────────

    async def get_analytics(self, short_code: str) -> Optional[dict]:
        from sqlalchemy import select, desc

        url_result = await self.db.execute(
            select(URL).where(URL.short_code == short_code)
        )
        url = url_result.scalar_one_or_none()
        if not url:
            return None

        # Merge DB count with any buffered Redis clicks
        buffered = 0
        if self.cache:
            buffered = await self.cache.get_buffered_clicks(short_code)
        total_clicks = url.click_count + buffered

        clicks_result = await self.db.execute(
            select(Click)
            .where(Click.short_code == short_code)
            .order_by(desc(Click.created_at))
            .limit(10)
        )
        recent_clicks = clicks_result.scalars().all()

        return {
            "short_code": url.short_code,
            "original_url": url.original_url,
            "click_count": total_clicks,
            "created_at": url.created_at,
            "expires_at": url.expires_at,
            "is_active": url.is_active,
            "is_custom_alias": url.is_custom_alias,
            "recent_clicks": [
                {
                    "id": c.id,
                    "ip_address": c.ip_address,
                    "user_agent": c.user_agent,
                    "referer": c.referer,
                    "created_at": c.created_at,
                }
                for c in recent_clicks
            ],
        }

    # ─── Private helpers ──────────────────────────────────────────────────

    async def _find_by_original_url(self, original_url: str) -> Optional[URL]:
        result = await self.db.execute(
            select(URL).where(
                URL.original_url == original_url,
                URL.is_active == True,           # noqa: E712
                URL.is_custom_alias == False,
            )
        )
        return result.scalar_one_or_none()

    async def _find_by_short_code(self, short_code: str) -> Optional[URL]:
        result = await self.db.execute(
            select(URL).where(URL.short_code == short_code)
        )
        return result.scalar_one_or_none()

    async def _handle_custom_alias(self, alias: str) -> str:
        existing = await self._find_by_short_code(alias)
        if existing:
            raise ValueError(f"The alias '{alias}' is already taken.")
        return alias

    async def _generate_unique_code(self) -> str:
        for _ in range(MAX_RETRIES):
            code = generate_short_code()
            if not await self._find_by_short_code(code):
                return code
        raise RuntimeError("Could not generate unique code after max retries.")

    def _build_response(self, url: URL) -> URLResponse:
        return URLResponse(
            short_code=url.short_code,
            short_url=f"{settings.BASE_URL}/{url.short_code}",
            original_url=url.original_url,
            is_custom_alias=url.is_custom_alias,
            click_count=url.click_count,
            created_at=url.created_at,
            expires_at=url.expires_at,
        )