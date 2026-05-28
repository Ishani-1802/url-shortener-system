from datetime import datetime, timedelta, timezone
from typing import Optional
from collections import defaultdict

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.core.config import get_settings
from app.models.url import URL, Click
from app.schemas.url import AnalyticsResponse, ClickDetail, DailyClickStat
from app.services.cache_service import CacheService

settings = get_settings()


class AnalyticsService:
    """
    Responsible for assembling the full analytics response.

    Three data sources:
      1. urls table      — metadata, denormalized click_count
      2. clicks table    — individual events, time-series aggregation
      3. Redis           — buffered clicks not yet flushed to PostgreSQL

    We merge all three to produce accurate real-time numbers without
    touching the redirect critical path.
    """

    def __init__(self, db: AsyncSession, redis: Optional[aioredis.Redis] = None):
        self.db = db
        self.cache = CacheService(redis) if redis else None

    async def get_analytics(self, short_code: str) -> Optional[AnalyticsResponse]:
        """
        Main entry point. Returns None if short_code doesn't exist.
        """
        # ── 1. Fetch the URL record ────────────────────────────────────────
        url_result = await self.db.execute(
            select(URL).where(URL.short_code == short_code)
        )
        url = url_result.scalar_one_or_none()
        if not url:
            return None

        # ── 2. Fetch all click rows for this code ─────────────────────────
        # We fetch everything needed in two queries, not N+1 queries.
        now = datetime.now(timezone.utc)
        cutoff_24h = now - timedelta(hours=24)
        cutoff_7d = now - timedelta(days=7)
        cutoff_30d = now - timedelta(days=30)

        # All clicks in the last 30 days (for time-series chart)
        clicks_30d_result = await self.db.execute(
            select(Click)
            .where(
                Click.short_code == short_code,
                Click.created_at >= cutoff_30d,
            )
            .order_by(Click.created_at.desc())
        )
        clicks_30d = clicks_30d_result.scalars().all()

        # ── 3. Compute rolling window counts from in-memory data ───────────
        # We already have the 30-day data — filter in Python, not with
        # extra DB queries. This is much cheaper than 3 separate COUNT queries.
        clicks_last_24h = sum(
            1 for c in clicks_30d
            if c.created_at.replace(tzinfo=timezone.utc) >= cutoff_24h
        )
        clicks_last_7d = sum(
            1 for c in clicks_30d
            if c.created_at.replace(tzinfo=timezone.utc) >= cutoff_7d
        )

        # ── 4. Build daily breakdown (last 30 days) ────────────────────────
        clicks_by_day = self._aggregate_by_day(clicks_30d, now)

        # ── 5. Recent clicks (last 10, already sorted desc) ───────────────
        recent_clicks = [
            ClickDetail(
                id=c.id,
                ip_address=c.ip_address,
                user_agent=c.user_agent,
                referer=c.referer,
                created_at=c.created_at,
            )
            for c in clicks_30d[:10]
        ]

        # ── 6. Merge DB click_count with Redis buffer ──────────────────────
        # The DB count may lag behind reality by up to CLICK_FLUSH_INTERVAL
        # seconds. Redis holds the difference. Adding them gives the true total.
        redis_buffer = 0
        if self.cache:
            redis_buffer = await self.cache.get_buffered_clicks(short_code)
        total_clicks = url.click_count + redis_buffer

        # ── 7. Assemble response ───────────────────────────────────────────
        return AnalyticsResponse(
            short_code=url.short_code,
            original_url=url.original_url,
            short_url=f"{settings.BASE_URL}/{url.short_code}",
            is_custom_alias=url.is_custom_alias,
            is_active=url.is_active,
            click_count=total_clicks,
            clicks_last_24h=clicks_last_24h,
            clicks_last_7d=clicks_last_7d,
            clicks_by_day=clicks_by_day,
            recent_clicks=recent_clicks,
            created_at=url.created_at,
            expires_at=url.expires_at,
        )

    # ── Private helpers ────────────────────────────────────────────────────

    def _aggregate_by_day(
        self,
        clicks: list,
        now: datetime,
    ) -> list[DailyClickStat]:
        """
        Group click events by calendar day and return a sorted list.

        Why in Python rather than SQL GROUP BY?
        We already fetched the 30-day data — doing a second DB round-trip
        for aggregation would cost more than the Python computation.
        At 10,000 clicks/day × 30 days = 300,000 rows, this is still
        fast in Python. At millions of daily clicks, push to SQL or ClickHouse.

        Always returns all 30 days, even days with zero clicks
        (so the front-end chart has a complete time series).
        """
        # Build a zero-filled map for all 30 days
        day_counts: dict[str, int] = {}
        for i in range(30):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            day_counts[day] = 0

        # Count clicks per day
        for click in clicks:
            # Handle both timezone-aware and naive datetimes
            click_time = click.created_at
            if hasattr(click_time, 'tzinfo') and click_time.tzinfo is None:
                click_time = click_time.replace(tzinfo=timezone.utc)
            day_str = click_time.strftime("%Y-%m-%d")
            if day_str in day_counts:
                day_counts[day_str] += 1

        # Sort chronologically (oldest first) — natural order for charts
        return [
            DailyClickStat(date=day, click_count=count)
            for day, count in sorted(day_counts.items())
        ]