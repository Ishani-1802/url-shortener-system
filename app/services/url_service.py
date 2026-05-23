from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.shortcode import generate_short_code, MAX_RETRIES
from app.models.url import URL, Click
from app.schemas.url import URLCreateRequest, URLResponse

settings = get_settings()


class URLService:
    """
    All URL shortening business logic lives here.
    The API layer calls these methods and returns the results.
    Keeping logic here (not in the router) makes it testable
    and reusable across different endpoints.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_short_url(self, request: URLCreateRequest) -> URLResponse:
        """
        Main method: takes a validated request, returns a shortened URL.

        Steps:
        1. Convert Pydantic HttpUrl to plain string
        2. Check for deduplication (same URL shortened before?)
        3. Handle custom alias or generate random code
        4. Resolve collisions if needed
        5. Save to DB
        6. Return response
        """

        original_url = str(request.long_url)

        # Deduplication
        if not request.custom_alias:
            existing = await self._find_by_original_url(original_url)

            if existing and existing.is_active and not existing.is_expired:
                return self._build_response(existing)

        # Determine short code
        if request.custom_alias:
            short_code = await self._handle_custom_alias(
                request.custom_alias
            )
        else:
            short_code = await self._generate_unique_code()

        # Expiry handling
        expires_at = None

        if request.expires_in_hours:
            expires_at = (
                datetime.now(timezone.utc)
                + timedelta(hours=request.expires_in_hours)
            )

        # Create DB record
        url_record = URL(
            short_code=short_code,
            original_url=original_url,
            is_custom_alias=bool(request.custom_alias),
            expires_at=expires_at,
            is_active=True,
            click_count=0,
        )

        self.db.add(url_record)

        await self.db.flush()
        await self.db.refresh(url_record)

        return self._build_response(url_record)

    async def get_url_by_code(
        self,
        short_code: str
    ) -> Optional[URL]:
        """
        Fetch active non-expired URL by short code.
        """

        result = await self.db.execute(
            select(URL).where(
                URL.short_code == short_code,
                URL.is_active == True,   # noqa: E712
            )
        )

        url = result.scalar_one_or_none()

        if not url:
            return None

        if url.is_expired:
            return None

        return url

    async def increment_click_count(self, url_id: int) -> None:
        """
        Increment click counter.
        """

        result = await self.db.execute(
            select(URL).where(URL.id == url_id)
        )

        url = result.scalar_one_or_none()

        if url:
            url.click_count += 1
            await self.db.flush()

    async def delete_url(self, short_code: str) -> bool:
        """
        Soft delete a URL.
        """

        result = await self.db.execute(
            select(URL).where(
                URL.short_code == short_code,
                URL.is_active == True,   # noqa: E712
            )
        )

        url = result.scalar_one_or_none()

        if not url:
            return False

        url.is_active = False

        await self.db.flush()

        return True

    async def record_click(
        self,
        url: URL,
        ip_address: Optional[str],
        user_agent: Optional[str],
        referer: Optional[str],
    ) -> None:
        """
        Record one click event for analytics.
        """

        click = Click(
            url_id=url.id,
            short_code=url.short_code,
            ip_address=ip_address,
            user_agent=user_agent,
            referer=referer,
        )

        self.db.add(click)

        await self.db.flush()

        # Increment denormalized counter
        url.click_count += 1

        await self.db.flush()

    async def get_analytics(
        self,
        short_code: str
    ) -> Optional[dict]:
        """
        Return analytics summary for a short code.
        """

        url_result = await self.db.execute(
            select(URL).where(
                URL.short_code == short_code
            )
        )

        url = url_result.scalar_one_or_none()

        if not url:
            return None

        clicks_result = await self.db.execute(
            select(Click)
            .where(Click.short_code == short_code)
            .order_by(desc(Click.created_at))
            .limit(10)
        )

        recent_clicks = clicks_result.scalars().all()

        return {
            "short_code": url.short_code,
            "original_url": url.original_url,
            "click_count": url.click_count,
            "created_at": url.created_at,
            "expires_at": url.expires_at,
            "is_active": url.is_active,
            "is_custom_alias": url.is_custom_alias,
            "recent_clicks": [
                {
                    "id": c.id,
                    "ip_address": c.ip_address,
                    "user_agent": c.user_agent,
                    "referer": c.referer,
                    "created_at": c.created_at,
                }
                for c in recent_clicks
            ],
        }

    # ─── Private helpers ────────────────────────────────────────────────────

    async def _find_by_original_url(
        self,
        original_url: str
    ) -> Optional[URL]:
        """
        Look up whether URL already exists.
        """

        result = await self.db.execute(
            select(URL).where(
                URL.original_url == original_url,
                URL.is_active == True,   # noqa: E712
                URL.is_custom_alias == False,
            )
        )

        return result.scalar_one_or_none()

    async def _find_by_short_code(
        self,
        short_code: str
    ) -> Optional[URL]:
        """
        Check whether short code exists.
        """

        result = await self.db.execute(
            select(URL).where(
                URL.short_code == short_code
            )
        )

        return result.scalar_one_or_none()

    async def _handle_custom_alias(self, alias: str) -> str:
        """
        Validate custom alias uniqueness.
        """

        existing = await self._find_by_short_code(alias)

        if existing:
            raise ValueError(
                f"The alias '{alias}' is already taken. "
                "Please choose another."
            )

        return alias

    async def _generate_unique_code(self) -> str:
        """
        Generate collision-free short code.
        """

        for _ in range(MAX_RETRIES):

            code = generate_short_code()

            existing = await self._find_by_short_code(code)

            if not existing:
                return code

        raise RuntimeError(
            f"Could not generate unique code after "
            f"{MAX_RETRIES} attempts."
        )

    def _build_response(self, url: URL) -> URLResponse:
        """
        Convert DB model into API response schema.
        """

        return URLResponse(
            short_code=url.short_code,
            short_url=f"{settings.BASE_URL}/{url.short_code}",
            original_url=url.original_url,
            is_custom_alias=url.is_custom_alias,
            click_count=url.click_count,
            created_at=url.created_at,
            expires_at=url.expires_at,
        )