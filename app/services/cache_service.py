from typing import Optional
import redis.asyncio as aioredis
from app.core.redis_client import url_cache_key, click_buffer_key
from app.core.config import get_settings

settings = get_settings()

# How long a URL stays cached: 1 hour
# After TTL expires, the next request is a cache miss → hits PostgreSQL → re-primes cache
CACHE_TTL_SECONDS = 3600

# Flush buffered click counts to PostgreSQL every 5 minutes
CLICK_FLUSH_INTERVAL = 300


class CacheService:
    """
    All Redis operations live here.
    Injected with a Redis client — easy to mock in tests.
    """

    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    # ─── URL redirect cache ───────────────────────────────────────────────

    async def get_cached_url(self, short_code: str) -> Optional[str]:
        """
        Cache hit: returns the original URL string.
        Cache miss: returns None → caller falls through to PostgreSQL.

        Key: url:redirect:{short_code}
        Value: the original URL (plain string)
        TTL: 3600 seconds
        """
        key = url_cache_key(short_code)
        return await self.redis.get(key)

    async def cache_url(self, short_code: str, original_url: str) -> None:
        """
        Store a URL in cache after a DB lookup (re-priming on cache miss).
        Also called proactively when a new URL is shortened.

        SETEX = SET + EXPIRE in one atomic operation.
        """
        key = url_cache_key(short_code)
        await self.redis.setex(key, CACHE_TTL_SECONDS, original_url)

    async def invalidate_url(self, short_code: str) -> None:
        """
        Remove a URL from cache when it's deleted or deactivated.
        Critical: without this, deleted URLs would still redirect for up to 1 hour.

        DEL is O(1) — instant regardless of cache size.
        """
        key = url_cache_key(short_code)
        await self.redis.delete(key)

    # ─── Click counter buffer ─────────────────────────────────────────────

    async def increment_click(self, short_code: str) -> int:
        """
        Increment the Redis click counter for a short code.
        Returns the new total.

        INCR is atomic — no race conditions even with 10,000 concurrent requests.
        This replaces the PostgreSQL UPDATE on every redirect.
        The counter is flushed to PostgreSQL in the background periodically.

        Key: url:clicks:{short_code}
        Value: integer count (stored as string — Redis has no int type)
        TTL: none (we flush manually)
        """
        key = click_buffer_key(short_code)
        count = await self.redis.incr(key)
        return count

    async def get_buffered_clicks(self, short_code: str) -> int:
        """Get current buffered click count without modifying it."""
        key = click_buffer_key(short_code)
        val = await self.redis.get(key)
        return int(val) if val else 0

    async def reset_click_buffer(self, short_code: str) -> int:
        """
        Atomically read AND reset the click counter.
        Used when flushing to PostgreSQL.

        GETDEL = GET + DEL in one atomic operation.
        This prevents the race condition where clicks arrive between
        reading the counter and deleting it.
        """
        key = click_buffer_key(short_code)
        val = await self.redis.getdel(key)
        return int(val) if val else 0

    # ─── Health check ─────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """Returns True if Redis is reachable."""
        try:
            return await self.redis.ping()
        except Exception:
            return False