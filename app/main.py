from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import urls as urls_router
from app.core.config import get_settings
from app.core.database import engine, Base

settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Production-ready URL Shortener — like bit.ly, built with FastAPI",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routers
# Important:
# Specific routes like /shorten and /analytics/{short_code}
# must exist before wildcard routes like /{short_code}
app.include_router(urls_router.router)


@app.on_event("startup")
async def startup_event():
    """
    Verify database tables exist on startup.
    """

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("Database tables verified/created.")


@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check endpoint.
    """

    db_status = "ok"

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    except Exception as e:
        db_status = f"error: {str(e)}"

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "db": db_status,
        "cache": "not configured (Phase 5)",
    }


@app.get("/", tags=["Root"])
async def root():
    """
    Root endpoint.
    """

    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "docs": "/docs",
        "health": "/health",
    }