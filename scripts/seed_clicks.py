import asyncio
import sys
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.url import URL, Click


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "curl/8.1.2",
    "python-httpx/0.26.0",
]

REFERERS = [
    "https://twitter.com",
    "https://linkedin.com",
    "https://reddit.com",
    None,
    None,
    None,
]

IPS = [f"192.168.1.{i}" for i in range(1, 50)]


async def seed(short_code: str, num_clicks: int = 120):
    async with AsyncSessionLocal() as db:

        result = await db.execute(
            select(URL).where(URL.short_code == short_code)
        )

        url = result.scalar_one_or_none()

        if not url:
            print(f"Error: short_code '{short_code}' not found.")
            return

        now = datetime.now(timezone.utc)

        clicks = []

        for i in range(num_clicks):

            days_ago = random.choices(
                range(30),
                weights=[30 - d for d in range(30)]
            )[0]

            hours_ago = random.randint(0, 23)

            created_at = now - timedelta(
                days=days_ago,
                hours=hours_ago
            )

            clicks.append(
                Click(
                    url_id=url.id,
                    short_code=short_code,
                    ip_address=random.choice(IPS),
                    user_agent=random.choice(USER_AGENTS),
                    referer=random.choice(REFERERS),
                    created_at=created_at,
                )
            )

        db.add_all(clicks)

        url.click_count = num_clicks

        await db.commit()

        print(f"✓ Seeded {num_clicks} clicks for '{short_code}'")
        print(f"→ Open: http://localhost:8000/analytics/{short_code}")


if __name__ == "__main__":

    code = sys.argv[1] if len(sys.argv) > 1 else None

    if not code:
        print("Usage: python scripts/seed_clicks.py <short_code>")
        sys.exit(1)

    asyncio.run(seed(code))