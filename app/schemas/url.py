from datetime import datetime
from typing import Optional
from pydantic import BaseModel, HttpUrl, Field, field_validator


class URLCreateRequest(BaseModel):
    """
    What the client sends to POST /shorten.
    Every field is validated by Pydantic before our code runs.
    """
    long_url: HttpUrl = Field(
        ...,
        description="The URL to shorten",
        examples=["https://www.google.com/search?q=fastapi"]
    )

    custom_alias: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=32,
        description="Optional custom short code",
        examples=["my-link"]
    )

    expires_in_hours: Optional[int] = Field(
        default=None,
        ge=1,        # Greater than or equal to 1
        le=8760,     # Less than or equal to 8760 (1 year)
        description="Hours until the link expires. Null means never.",
        examples=[24]
    )

    @field_validator("custom_alias")
    @classmethod
    def validate_alias(cls, v: Optional[str]) -> Optional[str]:
        """
        Custom aliases must be URL-safe.
        Reject spaces, slashes, and special characters.
        """
        if v is None:
            return v
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not all(c in allowed for c in v):
            raise ValueError("Alias can only contain letters, numbers, hyphens, and underscores")
        return v

    @field_validator("long_url", mode="before")
    @classmethod
    def validate_url_scheme(cls, v) -> str:
        """Ensure URL uses http or https — reject ftp://, mailto:, etc."""
        url_str = str(v)
        if not url_str.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return url_str


class URLResponse(BaseModel):
    """
    What we return after successfully shortening a URL.
    """
    short_code: str
    short_url: str
    original_url: str
    is_custom_alias: bool
    click_count: int
    created_at: datetime
    expires_at: Optional[datetime] = None

    model_config = {"from_attributes": True}  # Allows creating from ORM model


class URLAnalyticsResponse(BaseModel):
    """
    Full analytics response for GET /analytics/{short_code}.
    """
    short_code: str
    original_url: str
    click_count: int
    created_at: datetime
    expires_at: Optional[datetime] = None
    is_active: bool
    is_custom_alias: bool

    model_config = {"from_attributes": True}