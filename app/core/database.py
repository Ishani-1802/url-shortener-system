from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import get_settings

settings = get_settings()

# The async engine is the core connection pool.
# pool_pre_ping=True: before using a connection, ping the DB.
# This prevents errors from stale connections after a DB restart.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,       # Logs every SQL query in debug mode
    pool_pre_ping=True,
    pool_size=10,              # Max persistent connections in pool
    max_overflow=20,           # Extra connections allowed under load
)

# Session factory — each request gets its own session
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,    # Don't expire objects after commit
)


class Base(DeclarativeBase):
    """
    All ORM models inherit from this.
    SQLAlchemy uses it to track all tables and generate CREATE TABLE statements.
    """
    pass


async def get_db() -> AsyncSession:
    """
    FastAPI dependency — injects a DB session into each endpoint.
    The 'async with' ensures the session is always closed,
    even if an exception is raised mid-request.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()