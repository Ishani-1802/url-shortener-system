import time
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.config import get_settings
from app.core.redis_client import get_redis

settings = get_settings()


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter using a Redis sorted set per IP.

    Redis data structure per IP:
        Key:   ratelimit:{ip_address}
        Type:  Sorted Set
        Score: Unix timestamp (float, microsecond precision)
        Value: Unique request identifier (also the timestamp as string)
        TTL:   RATE_LIMIT_WINDOW seconds (auto-expires the whole key)

    Algorithm (4 steps, all atomic via pipeline):
        1. ZREMRANGEBYSCORE — remove entries older than window
        2. ZCARD            — count remaining entries
        3. ZADD             — add current request
        4. EXPIRE           — reset TTL on the key

    Why sorted set instead of a simple counter?
        A counter (INCR) only supports fixed windows.
        A sorted set lets us remove old entries, giving true sliding behaviour.

    Fails open: if Redis is unreachable, the request is allowed through.
    This is a deliberate availability-over-security tradeoff — a Redis
    outage shouldn't take down your whole API.
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.limit = settings.RATE_LIMIT_REQUESTS
        self.window = settings.RATE_LIMIT_WINDOW

        # Endpoints exempt from rate limiting
        # Health checks and docs must always be reachable
        self.exempt_paths = {"/health", "/docs", "/redoc", "/openapi.json", "/"}

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip rate limiting for exempt paths
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        # Get client IP — respects X-Forwarded-For behind proxies
        ip = self._get_client_ip(request)

        try:
            allowed, current_count, retry_after = await self._check_rate_limit(ip)
        except Exception:
            # Redis unavailable — fail open (allow the request)
            return await call_next(request)

        # Add rate limit headers to every response (like GitHub's API)
        # This lets clients know their current usage without hitting 429
        response_headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.limit - current_count)),
            "X-RateLimit-Window": str(self.window),
        }

        if not allowed:
            response_headers["Retry-After"] = str(retry_after)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded.",
                    "limit": self.limit,
                    "window_seconds": self.window,
                    "retry_after_seconds": retry_after,
                    "tip": f"You can make {self.limit} requests per {self.window} seconds.",
                },
                headers=response_headers,
            )

        response = await call_next(request)

        # Attach headers to the actual response too
        for key, value in response_headers.items():
            response.headers[key] = value

        return response

    async def _check_rate_limit(self, ip: str) -> tuple[bool, int, int]:
        """
        Core sliding window logic.

        Returns:
            (allowed: bool, current_count: int, retry_after: int)

        Uses a Redis pipeline to execute all commands in a single
        round-trip — critical for keeping the rate limiter fast.
        Without pipelining, we'd have 4 round-trips × ~1ms each = 4ms
        just for rate limiting. With pipelining: 1 round-trip = ~1ms.
        """
        redis = await get_redis()
        key = f"ratelimit:{ip}"
        now = time.time()  # Unix timestamp, float
        window_start = now - self.window

        # Execute all 4 Redis commands in one round-trip
        async with redis.pipeline(transaction=True) as pipe:
            # Step 1: Remove entries older than the window
            pipe.zremrangebyscore(key, 0, window_start)
            # Step 2: Count how many requests are in the current window
            pipe.zcard(key)
            # Step 3: Add this request (score = timestamp, value = timestamp string)
            # Using str(now) as value — must be unique per request
            pipe.zadd(key, {str(now): now})
            # Step 4: Set TTL so Redis auto-cleans idle keys
            pipe.expire(key, self.window)

            results = await pipe.execute()

        # results[1] is ZCARD — count BEFORE adding current request
        current_count = results[1]

        if current_count >= self.limit:
            # Find the oldest entry to calculate when the window clears
            # ZRANGE with index 0 gives the entry with the lowest score (oldest)
            oldest = await redis.zrange(key, 0, 0, withscores=True)
            if oldest:
                oldest_timestamp = oldest[0][1]
                retry_after = int(oldest_timestamp + self.window - now) + 1
            else:
                retry_after = self.window
            return False, current_count, retry_after

        return True, current_count + 1, 0

    def _get_client_ip(self, request: Request) -> str:
        """
        Extract the real client IP.

        X-Forwarded-For can contain a chain: "client, proxy1, proxy2"
        We always want the leftmost (original client) IP.
        Fall back to direct connection IP if header is absent.
        """
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"