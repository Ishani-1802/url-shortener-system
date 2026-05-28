from typing import Optional

import redis.asyncio as aioredis

from app.core.config import get_settings

settings = get_settings()

_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis_client

    if _redis_client is None:

        # Upstash / cloud Redis (TLS)
        if settings.REDIS_URL.startswith("rediss://"):
            _redis_client = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )

        # Local Redis
        else:
            _redis_client = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                max_connections=20,
            )

    return _redis_client


async def close_redis() -> None:
    global _redis_client

    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None


def url_cache_key(short_code: str) -> str:
    return f"url:redirect:{short_code}"


def click_buffer_key(short_code: str) -> str:
    return f"url:clicks:{short_code}"