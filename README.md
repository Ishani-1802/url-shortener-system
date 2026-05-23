# URL Shortener — Production-Ready System

A full-stack URL shortener (like bit.ly) built with **FastAPI + PostgreSQL + Redis**, designed for millions of URLs with sub-100ms redirects.

---

## Architecture

```
Client → Nginx (LB) → FastAPI × N workers
                         ├─ Redis   (cache hit  → ~1 ms redirect)
                         └─ Postgres (cache miss → ~10–30 ms redirect)
                                       └─ Background: sync click counters
```

### Layers

| Layer | Technology | Responsibility |
|---|---|---|
| API | FastAPI (async) | Request routing, validation, rate limiting |
| Service | Python classes | Business logic, dedup, code generation |
| Cache | Redis 7 | Redirect lookup cache, rate-limit counters, async click buffer |
| Database | PostgreSQL 16 | Durable storage, analytics, dedup index |

---

## Database Schema

### `urls` table
| Column | Type | Notes |
|---|---|---|
| id | SERIAL PK | Internal ID |
| short_code | VARCHAR(32) UNIQUE | The generated/custom code — **primary lookup key** |
| original_url | TEXT | Destination URL — indexed for dedup |
| is_custom_alias | BOOL | True if user supplied the alias |
| click_count | INT | Denormalized counter — updated async from Redis |
| created_at | TIMESTAMP | Auto-set by Postgres |
| expires_at | TIMESTAMP nullable | NULL = never expires |
| is_active | BOOL | Soft-delete flag |

### `clicks` table (append-only analytics)
| Column | Type | Notes |
|---|---|---|
| id | SERIAL PK | |
| url_id | FK → urls.id | CASCADE delete |
| short_code | VARCHAR(32) | Denormalized for fast per-code analytics |
| ip_address | VARCHAR(45) | IPv4 + IPv6 safe |
| user_agent | TEXT | Browser/bot identification |
| referer | TEXT | Traffic source |
| created_at | TIMESTAMP | For time-series queries |

---

## Short Code Generation (Base62)

**Alphabet**: `0-9 A-Z a-z` = 62 characters  
**Length**: 7 characters  
**Capacity**: 62⁷ ≈ **3.5 trillion** unique codes

```
generate_short_code(7)   → "aB3xK9m"
encode_base62(1_000_000) → "4c92"
```

**Collision probability** at 1 million codes: ~1.4 × 10⁻⁷ (negligible)

**Collision handling**:
1. Generate random Base62 code
2. Check DB uniqueness
3. If collision → retry (max 5 times, practically unreachable)
4. Raise error after MAX_RETRIES

**Alternative for interview discussion**: Counter-based approach (Snowflake-style) — maintain a global atomic counter in Redis, encode to Base62. Zero collisions, but requires a reliable counter service (single point of failure unless distributed).

---

## Redirect Flow (Critical Path)

```
GET /{short_code}
  │
  ├─ [Cache HIT]  Redis.get(url:redirect:{code})
  │               → 302 redirect   ← ~1ms total
  │               → Redis.incr(url:clicks:{code})  ← async, non-blocking
  │
  └─ [Cache MISS] Postgres SELECT WHERE short_code = ?
                  → check is_active, expires_at
                  → Redis.setex(url:redirect:{code}, 3600, url)  ← re-prime cache
                  → INSERT INTO clicks (...)
                  → 302 redirect   ← ~10-30ms total
```

---

## Rate Limiting (Sliding Window)

Redis sorted set per IP:
1. Remove entries older than `now - window`
2. Count remaining = requests in window
3. If ≥ limit → 429 Too Many Requests
4. Add current timestamp

Default: **60 requests / 60 seconds** per IP. Configurable via env vars.

Fails **open** — if Redis is down, traffic is not blocked (availability over security).

---

## API Endpoints

### `POST /shorten`
```json
Request:
{
  "long_url": "https://example.com/very/long/path",
  "custom_alias": "my-link",          // optional
  "expires_in_hours": 24              // optional
}

Response 201:
{
  "short_code": "my-link",
  "short_url": "http://localhost:8000/my-link",
  "original_url": "https://example.com/very/long/path",
  "is_custom_alias": true,
  "click_count": 0,
  "created_at": "2024-01-01T00:00:00",
  "expires_at": "2024-01-02T00:00:00"
}
```

### `GET /{short_code}`
Returns `302 Found` with `Location` header set to the original URL.

### `GET /analytics/{short_code}`
```json
{
  "short_code": "my-link",
  "original_url": "https://example.com/...",
  "click_count": 42,
  "created_at": "...",
  "expires_at": null,
  "is_active": true,
  "recent_clicks": [
    { "id": 1, "ip_address": "1.2.3.4", "user_agent": "...", "created_at": "..." }
  ]
}
```

### `DELETE /urls/{short_code}`
Soft-deletes the link (sets `is_active = false`) and evicts the Redis cache entry.

### `GET /health`
```json
{ "status": "ok", "db": "ok", "cache": "ok", "uptime_seconds": 123.4 }
```

---

## Running Locally

### Prerequisites
- Docker + Docker Compose
- Python 3.12+ (for running tests locally without Docker)

### 1. Clone and configure
```bash
git clone <repo>
cd url-shortener
cp .env.example .env
# Edit .env if needed (defaults work for local dev)
```

### 2. Start all services
```bash
docker compose up --build
```

Services start at:
- **API**: http://localhost:8000
- **Docs**: http://localhost:8000/docs
- **PostgreSQL**: localhost:5432
- **Redis**: localhost:6379

### 3. Test the API
```bash
# Shorten a URL
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"long_url": "https://www.google.com/search?q=fastapi"}'

# Use the returned short_code (e.g. "aB3xK9m"):
curl -L http://localhost:8000/aB3xK9m

# Analytics
curl http://localhost:8000/analytics/aB3xK9m

# Custom alias with expiry
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"long_url": "https://example.com", "custom_alias": "demo", "expires_in_hours": 1}'
```

### 4. Run tests
```bash
# Install test dependencies
pip install -r requirements.txt

# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ -v --cov=app --cov-report=term-missing
```

### 5. Stop services
```bash
docker compose down           # Stop containers
docker compose down -v        # Stop + delete volumes (wipe DB)
```

---

## Folder Structure

```
url-shortener/
├── app/
│   ├── main.py               ← FastAPI app factory, middleware
│   ├── api/
│   │   ├── urls.py           ← REST endpoints (thin router layer)
│   │   └── health.py         ← /health probe
│   ├── services/
│   │   └── url_service.py    ← All business logic
│   ├── models/
│   │   └── url.py            ← SQLAlchemy ORM models
│   ├── schemas/
│   │   └── url.py            ← Pydantic request/response schemas
│   ├── core/
│   │   ├── config.py         ← Pydantic-settings configuration
│   │   ├── database.py       ← Async engine + session factory
│   │   ├── redis_client.py   ← Shared Redis connection pool
│   │   └── shortcode.py      ← Base62 generator
│   └── middleware/
│       └── rate_limiter.py   ← Sliding window rate limiter
├── tests/
│   └── test_urls.py          ← Pytest async integration tests
├── scripts/
│   └── init.sql              ← Postgres init script
├── Dockerfile                ← Multi-stage production image
├── docker-compose.yml        ← Full stack: app + db + redis
├── requirements.txt
├── pytest.ini
└── .env.example
```

---

## Scaling Strategies (Production)

### Horizontal scaling
- Run N FastAPI containers behind Nginx/HAProxy
- Redis and Postgres are already shared services
- Stateless app layer → scale freely

### Database scaling
- **Read replicas**: redirect reads hit replicas; writes go to primary
- **Sharding**: partition `urls` by `short_code % N_shards`
- **Partitioning**: partition `clicks` by `created_at` range (monthly partitions)

### Cache scaling
- **Redis Cluster**: 3+ master nodes, auto-sharding
- Increase `CACHE_TTL_SECONDS` to reduce DB pressure
- Hot URLs stay in cache indefinitely (LRU eviction protects memory)

### Click analytics at scale
- Replace inline DB inserts with **Kafka/Redis Streams**
- Background consumer batch-inserts clicks every 5s
- This removes all write pressure from the redirect path

---

## Further Improvements

1. **Auth & ownership**: JWT-based auth so only the creator can view analytics / delete links
2. **QR code generation**: Return a QR code PNG for each short URL
3. **Click stream analytics**: Kafka → ClickHouse for real-time dashboards
4. **Geo-routing**: Redirect to different destinations based on user country
5. **Link preview**: `GET /preview/{code}` returns metadata without redirecting
6. **Abuse detection**: ML-based phishing/malware URL classifier before shortening
7. **Dashboard UI**: React frontend for managing and visualizing links
8. **Alembic migrations**: Replace `create_all` with versioned schema migrations
9. **OpenTelemetry**: Distributed tracing (Jaeger/Tempo) for latency debugging
10. **Counter-based IDs**: Snowflake ID generator for guaranteed collision-free codes at scale
