from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import urls as urls_router
from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.redis_client import close_redis, get_redis
from app.middleware.rate_limiter import RateLimiterMiddleware

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):

    # Startup
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        print("✓ Database tables verified.")

    except Exception as e:
        print(f"Database startup error: {e}")

    try:
        redis = await get_redis()
        await redis.ping()

        print("✓ Redis connection verified.")

    except Exception as e:
        print(f"Redis startup error: {e}")

    yield

    # Shutdown
    try:
        await close_redis()
        await engine.dispose()

        print("✓ Connections closed.")

    except Exception as e:
        print(f"Shutdown error: {e}")

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Production-ready URL Shortener — like bit.ly, built with FastAPI",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Middleware order matters
# Last added runs first

app.add_middleware(
    RateLimiterMiddleware
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["Health"])
async def health_check():
    db_status = "ok"
    cache_status = "ok"

    # Database check
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {str(e)}"

    # Redis check
    try:
        redis = await get_redis()
        await redis.ping()
    except Exception as e:
        cache_status = f"error: {str(e)}"

    overall_status = (
        "ok"
        if db_status == "ok" and cache_status == "ok"
        else "degraded"
    )

    return {
        "status": overall_status,
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "db": db_status,
        "cache": cache_status,
    }


@app.get("/", tags=["Root"])
async def root():
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "docs": "/docs",
        "health": "/health",
    }


# Include router LAST
app.include_router(urls_router.router)