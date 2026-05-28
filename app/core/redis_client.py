from typing import Optional

import redis.asyncio as aioredis

from app.core.config import get_settings

settings = get_settings()

# Shared Redis client (connection pool)
_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """
    Return the shared Redis client.
    Create it lazily on first use.
    """
    global _redis_client

    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )

    return _redis_client


async def close_redis() -> None:
    """
    Gracefully close Redis connections on app shutdown.
    """
    global _redis_client

    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


# ─── Redis key helpers ──────────────────────────────────────────────

def url_cache_key(short_code: str) -> str:
    """
    Key for cached redirect URLs.
    """
    return f"url:redirect:{short_code}"


def click_buffer_key(short_code: str) -> str:
    """
    Key for buffered click counters.
    """
    return f"url:clicks:{short_code}"