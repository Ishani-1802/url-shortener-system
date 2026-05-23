from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey,
    Integer, String, Text, func
)
from sqlalchemy.orm import relationship
from app.core.database import Base


class URL(Base):
    """
    Represents a shortened URL.
    This maps directly to the 'urls' table in PostgreSQL.
    """
    __tablename__ = "urls"

    id = Column(Integer, primary_key=True, index=True)

    short_code = Column(
        String(32),
        unique=True,
        index=True,         # Critical: makes lookup by short_code O(log n)
        nullable=False,
    )

    original_url = Column(
        Text,
        nullable=False,
        index=True,         # Used for deduplication: "has this URL been shortened before?"
    )

    is_custom_alias = Column(Boolean, default=False, nullable=False)

    click_count = Column(
        Integer,
        default=0,
        nullable=False,
        comment="Denormalized counter — updated async from Redis in Phase 5"
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),  # DB sets this, not Python — avoids timezone bugs
        nullable=False,
    )

    expires_at = Column(
        DateTime(timezone=True),
        nullable=True,      # NULL means never expires
    )

    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Soft delete flag — we never physically delete rows"
    )

    # Relationship to clicks (one URL → many clicks)
    clicks = relationship(
        "Click",
        back_populates="url",
        cascade="all, delete-orphan",   # Deleting a URL deletes its clicks
        lazy="select",
    )

    def __repr__(self):
        return f"<URL id={self.id} short_code={self.short_code!r}>"

    @property
    def is_expired(self) -> bool:
        """Check if this URL has passed its expiry time."""
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at.replace(tzinfo=None)


class Click(Base):
    """
    Append-only analytics table.
    Every redirect creates one row here.
    Never update or delete individual rows — only append.
    """
    __tablename__ = "clicks"

    id = Column(Integer, primary_key=True, index=True)

    url_id = Column(
        Integer,
        ForeignKey("urls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Denormalized: also store short_code here so analytics queries
    # don't always need to JOIN with urls table
    short_code = Column(String(32), nullable=False, index=True)

    ip_address = Column(
        String(45),     # 45 chars covers full IPv6 addresses
        nullable=True,
    )

    user_agent = Column(Text, nullable=True)

    referer = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,     # Index for time-based analytics queries
    )

    # Relationship back to URL
    url = relationship("URL", back_populates="clicks")

    def __repr__(self):
        return f"<Click id={self.id} short_code={self.short_code!r}>"