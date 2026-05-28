from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from app.core.config import get_settings

settings = get_settings()

# SSL support for Neon PostgreSQL
connect_args = {}

if (
    "neon.tech" in settings.DATABASE_URL
    or "ssl=require" in settings.DATABASE_URL
):
    connect_args["ssl"] = "require"

# Create async SQLAlchemy engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    future=True,
    connect_args=connect_args,
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Base model class
Base = declarative_base()


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session