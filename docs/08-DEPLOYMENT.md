# Deployment

## Docker Compose Setup

Forge ships with a `docker-compose.yml` that runs the full stack:

```yaml
version: "3.9"

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: forge
      POSTGRES_PASSWORD: forge
      POSTGRES_DB: forge
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U forge"]
      interval: 5s
      timeout: 3s
      retries: 5

  forge-api:
    build:
      context: ./backend
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    env_file:
      - .env.docker
    depends_on:
      postgres:
        condition: service_healthy
    volumes:
      - workspaces:/tmp/forge-workspaces

volumes:
  pgdata:
  workspaces:
```

### Services

| Service | Image | Purpose |
|---------|-------|---------|
| `postgres` | postgres:16-alpine | Database for sessions, audit, checkpoints, learning |
| `forge-api` | Built from `backend/Dockerfile` | FastAPI application server |

### Volumes

| Volume | Purpose |
|--------|---------|
| `pgdata` | PostgreSQL data persistence across restarts |
| `workspaces` | Isolated workspace directories for task execution |

### Health Checks

PostgreSQL has a health check that the API service depends on:
- **Test:** `pg_isready -U forge`
- **Interval:** 5 seconds
- **Timeout:** 3 seconds
- **Retries:** 5

The API service only starts after PostgreSQL reports healthy.

## Quick Start

```bash
# 1. Configure environment
cp .env.docker .env.docker.local
# Edit .env.docker.local with your actual API keys

# 2. Start services
docker-compose up -d

# 3. Run database migrations
cd backend
DATABASE_URL=postgresql://forge:forge@localhost:5432/forge alembic upgrade head

# 4. Verify
curl http://localhost:8000/health
# {"status": "ok"}
```

## Environment Variables

### Complete Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes (Docker) | — | PostgreSQL connection string |
| `DATABASE_POOL_SIZE` | No | `10` | Max connection pool size |
| `OPENROUTER_API_KEY` | Yes | — | OpenRouter API key for AI completions |
| `GITHUB_TOKEN` | Yes | — | GitHub personal access token |
| `AIDER_MODEL` | No | `claude-sonnet-4-20250514` | Model for Aider coding tool |
| `FORGE_API_TOKEN` | Yes | — | Bearer token for API authentication |
| `FORGE_AUTH_DISABLED` | No | `false` | Disable auth (development only) |
| `FORGE_ENV` | No | `production` | Environment name |
| `FORGE_LOG_LEVEL` | No | `INFO` | Logging level |
| `FORGE_CONFIG_DIR` | No | `./config` | Path to YAML config directory |
| `SESSION_MAX_TOKENS` | No | `1000000` | Max tokens per session budget |
| `HEALTH_MONITOR_INTERVAL_S` | No | `30` | Health check interval in seconds |

### Template File (`.env.docker`)

```env
# Database
DATABASE_URL=postgresql+asyncpg://forge:forge@postgres:5432/forge
DATABASE_POOL_SIZE=10

# AI Provider (OpenRouter)
OPENROUTER_API_KEY=sk-or-your-key-here

# VCS (GitHub)
GITHUB_TOKEN=ghp_your-github-token

# Coding Tool
AIDER_MODEL=claude-sonnet-4-20250514

# Authentication
FORGE_API_TOKEN=your-secret-api-token
FORGE_AUTH_DISABLED=false

# Runtime
FORGE_ENV=production
FORGE_LOG_LEVEL=INFO
FORGE_CONFIG_DIR=./config

# Session defaults
SESSION_MAX_TOKENS=1000000

# Health monitor
HEALTH_MONITOR_INTERVAL_S=30
```

## Local Development (Without Docker)

```bash
# Backend
cd backend
python -m venv .venv
source .venv/bin/activate  # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
cp .env.example .env
# Edit .env with your keys
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
# Open http://localhost:3000
```

## Production Considerations

### Security

- **Change all default passwords** — especially `POSTGRES_PASSWORD` and `FORGE_API_TOKEN`
- **Use secrets management** — Docker secrets, Vault, or cloud-native secret stores
- **Network isolation** — Don't expose PostgreSQL port (5432) to the internet
- **TLS termination** — Put a reverse proxy (nginx, Caddy, ALB) in front of the API
- **Token rotation** — Rotate `GITHUB_TOKEN` and `OPENROUTER_API_KEY` periodically

### Persistence

- **Backup PostgreSQL** — Regular pg_dump or WAL archiving
- **Volume durability** — Use named volumes or bind mounts on reliable storage
- **Workspace cleanup** — Implement TTL on workspace volumes (tasks complete → cleanup)

### Monitoring

- **Health endpoint:** `GET /health` returns `{"status": "ok"}`
- **Structured logging:** JSON logs with session_id correlation
- **Event audit:** All operations are recorded in the audit_log table
- **Metrics:** Export from the health monitor (future: Prometheus endpoint)

### Resource Limits

```yaml
# Add to forge-api service in docker-compose.yml
deploy:
  resources:
    limits:
      cpus: "2.0"
      memory: 2G
    reservations:
      cpus: "0.5"
      memory: 512M
```

## How to Scale

### Current Architecture (Single Process)

Forge v1 runs as a single asyncio process. This is sufficient for:
- Single-tenant use
- Low-concurrency workloads (1–5 concurrent sessions)
- Development and evaluation

### Scaling Strategy

```mermaid
graph TB
    LB[Load Balancer / Reverse Proxy]
    LB --> API1[Forge API Instance 1]
    LB --> API2[Forge API Instance 2]
    API1 --> PG[(PostgreSQL)]
    API2 --> PG
    API1 --> WS1[Workspace Volume 1]
    API2 --> WS2[Workspace Volume 2]
```

**Horizontal scaling checklist:**

1. **Session affinity** — WebSocket connections require sticky sessions (or use Redis pub/sub)
2. **Shared database** — PostgreSQL handles concurrent access
3. **Workspace isolation** — Each instance needs its own workspace volume
4. **Event ordering** — Per-session event sequences must remain ordered (handled by DB constraint)
5. **Health monitor** — Each instance runs its own; registry state is local

### Future: Worker Architecture

For high-concurrency production use:
- Separate API tier from worker tier
- Workers pull tasks from a queue (Redis, SQS)
- API tier handles HTTP/WebSocket only
- Workers execute builds in ephemeral containers
