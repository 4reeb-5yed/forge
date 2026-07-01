# Deployment

## Docker Compose Setup

Forge ships with a `docker-compose.yml` that runs the full stack:

```yaml
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
      - /var/run/docker.sock:/var/run/docker.sock
    group_add:
      - "999"  # Docker socket GID — adjust for your host

volumes:
  pgdata:
  workspaces:
```

### Services

| Service | Image | Purpose |
|---------|-------|---------|
| `postgres` | postgres:16-alpine | Database for sessions, audit, checkpoints, learning |
| `forge-api` | Built from `backend/Dockerfile` | FastAPI application server + sandbox orchestrator |

### Volumes

| Volume / Mount | Purpose |
|--------|---------|
| `pgdata` | PostgreSQL data persistence across restarts |
| `workspaces` | Isolated workspace directories for task execution |
| `/var/run/docker.sock` | Allows forge-api to spawn sandbox containers on the host daemon |

### Docker Socket Access

The forge-api container mounts the host's Docker socket so that `SandboxedAiderTool` can spawn ephemeral sandbox containers. This is **not** Docker-in-Docker — the sandbox containers run as siblings on the host daemon. The sandbox containers themselves have no socket access (`--network none`, no volume mounts except workspace).

The `group_add: ["999"]` gives the forge-api process permission to write to the socket. If your host's docker group has a different GID (check with `stat -c '%g' /var/run/docker.sock`), update this value.

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

## Production Features

### PostgreSQL Persistence

All persistent stores are backed by PostgreSQL when `DATABASE_URL` is configured:

| Store | Purpose | API |
|-------|---------|-----|
| Session Store | Session lifecycle and state | `/sessions/*` |
| Audit Store | Event stream persistence | `/sessions/{id}/events` |
| Checkpoint Store | Crash recovery snapshots | `/recovery/*` |
| Learning Store | Outcome recording | Internal |

The stores are auto-wired during bootstrap when `DATABASE_URL` is available. Falls back to in-memory for development without a database.

### Approval Gates

Human-in-the-loop approval before commits:

```
Build → Approval Required → Human Reviews Diff → Approve/Reject → Commit
```

| API Endpoint | Description |
|--------------|-------------|
| `GET /approval/pending/{session_id}` | List pending approvals |
| `GET /approval/{request_id}/diff` | Get full diff for review |
| `POST /approval/{request_id}/approve` | Approve changes |
| `POST /approval/{request_id}/reject` | Reject changes |

Frontend shows an `ApprovalBanner` when builds await review.

### Concurrent Builds

Multiple builds run in parallel with configurable limits:

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `FORGE_MAX_CONCURRENT` | `3` | Maximum concurrent sessions |

The `SessionScheduler` manages a priority queue and executes builds based on availability.

### Build Timeouts

Auto-stop builds exceeding the configured timeout:

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `FORGE_BUILD_TIMEOUT_SECONDS` | `1800` (30 min) | Build timeout |

The `BuildTimeoutManager` tracks active builds and issues stop signals on timeout.

### Checkpoint Recovery

Automatic workflow state persistence for crash recovery:

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `FORGE_CHECKPOINT_INTERVAL` | `60` | Seconds between checkpoints (0=disabled) |

Checkpoints are saved after each node execution with sensitive data redacted. Use the recovery API to resume interrupted builds.

| API Endpoint | Description |
|--------------|-------------|
| `GET /recovery/sessions` | List recoverable sessions |
| `GET /recovery/sessions/{id}` | Get checkpoint data |
| `POST /recovery/sessions/{id}/resume` | Resume from checkpoint |

### Learning Engine

Pattern analysis for model/provider health:

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `FORGE_LEARNING_WINDOW_DAYS` | `7` | Analysis window in days |

Analyzes outcomes to generate recommendations for:
- Model performance (success rates, latency)
- Provider health (uptime, circuit breaker trips)
- Task patterns (retry-heavy, unreliable)

## Environment Variables

### Complete Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Production | — | PostgreSQL connection string |
| `DATABASE_POOL_SIZE` | No | `10` | Max connection pool size |
| `OPENROUTER_API_KEY` | Yes | — | OpenRouter API key for AI completions |
| `GITHUB_TOKEN` | Yes | — | GitHub personal access token |
| `AIDER_MODEL` | No | `claude-sonnet-4-20250514` | Model for Aider coding tool |
| `FORGE_API_TOKEN` | Yes | — | Bearer token for API authentication |
| `FORGE_AUTH_DISABLED` | No | `false` | Disable auth (development only) |
| `FORGE_ENV` | No | `production` | Environment name |
| `FORGE_LOG_LEVEL` | No | `INFO` | Logging level |
| `FORGE_CONFIG_DIR` | No | `./config` | Path to YAML config directory |
| `FORGE_USE_SANDBOX` | No | `always` | Sandbox mode: `always` (recommended), `auto`, or `never` |
| `FORGE_MAX_CONCURRENT` | No | `3` | Max concurrent builds |
| `FORGE_BUILD_TIMEOUT_SECONDS` | No | `1800` | Build timeout in seconds |
| `FORGE_CHECKPOINT_INTERVAL` | No | `60` | Checkpoint interval in seconds |
| `FORGE_LEARNING_WINDOW_DAYS` | No | `7` | Learning analysis window |
| `NEXT_PUBLIC_WS_URL` | No | `ws://localhost:8000` | WebSocket URL for frontend event stream |
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

# Sandbox (always=recommended for production, auto=use if available, never=disable)
FORGE_USE_SANDBOX=always

# Concurrent Builds
FORGE_MAX_CONCURRENT=3

# Build Timeout (30 minutes default)
FORGE_BUILD_TIMEOUT_SECONDS=1800

# Checkpoint Recovery (60 seconds interval)
FORGE_CHECKPOINT_INTERVAL=60

# Learning Engine (7 day analysis window)
FORGE_LEARNING_WINDOW_DAYS=7

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
- **Build the sandbox image** — `docker build -t forge-aider-sandbox:latest -f Dockerfile.sandbox .`
- **Set `FORGE_USE_SANDBOX=always`** in production for fail-closed sandbox enforcement
- **Monitor `commit_blocked` events** in the audit trail for signs of AI attempting sensitive modifications
- **Use approval gates** — Require human review before commits in production

See [Security](./12-SECURITY.md) for the complete security model.

### Persistence

- **Backup PostgreSQL** — Regular pg_dump or WAL archiving
- **Volume durability** — Use named volumes or bind mounts on reliable storage
- **Workspace cleanup** — Implement TTL on workspace volumes (tasks complete → cleanup)

### Monitoring

- **Health endpoint:** `GET /health` returns per-component health status
- **Structured logging:** JSON logs with session_id correlation
- **Event audit:** All operations are recorded in the audit_log table
- **Approval events:** `approval.requested`, `approval.approved`, `approval.rejected`
- **Timeout events:** `build.timeout.started`, `build.timeout.exceeded`
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
- Low-concurrency workloads (1–5 concurrent sessions by default)
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
6. **Concurrent builds** — Each instance manages its own `SessionScheduler`

### Future: Worker Architecture

For high-concurrency production use:
- Separate API tier from worker tier
- Workers pull tasks from a queue (Redis, SQS)
- API tier handles HTTP/WebSocket only
- Workers execute builds in ephemeral containers
