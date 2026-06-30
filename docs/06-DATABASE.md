# Database — PostgreSQL Persistence

Forge uses PostgreSQL 16 for durable persistence of sessions, audit logs, checkpoints, and learning outcomes. The database layer lives in `backend/app/db/`.

## Schema Overview

```mermaid
erDiagram
    sessions ||--o{ audit_log : "has"
    sessions ||--o{ checkpoints : "has"
    sessions ||--o{ learning_outcomes : "has"

    sessions {
        UUID id PK
        TEXT repo_url
        TEXT goal
        VARCHAR(32) build_mode
        VARCHAR(32) status
        TIMESTAMP created_at
        TIMESTAMP updated_at
        JSONB context_json
    }

    audit_log {
        UUID id PK
        UUID session_id FK
        INTEGER seq
        VARCHAR(64) event_type
        VARCHAR(128) source
        JSONB payload
        TIMESTAMP created_at
        VARCHAR(128) causation_id
        VARCHAR(128) correlation_id
        VARCHAR(128) event_id UK
    }

    checkpoints {
        INTEGER id PK
        UUID session_id FK
        VARCHAR(128) node_id
        INTEGER highest_seq
        JSONB state_json
        TIMESTAMP created_at
    }

    learning_outcomes {
        UUID id PK
        UUID session_id FK
        VARCHAR(128) task_id
        VARCHAR(32) outcome_status
        JSONB data_json
        TIMESTAMP created_at
    }
```

## Table Details

### `sessions`

Primary table for build sessions.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | UUID | PK | Session identifier |
| `repo_url` | TEXT | NOT NULL | Target repository URL |
| `goal` | TEXT | NOT NULL | User's build goal |
| `build_mode` | VARCHAR(32) | NOT NULL | new, extend, analyze, document |
| `status` | VARCHAR(32) | NOT NULL, default 'pending' | Session lifecycle status |
| `created_at` | TIMESTAMP(tz) | NOT NULL, default now() | Creation time |
| `updated_at` | TIMESTAMP(tz) | NOT NULL, default now() | Last update time |
| `context_json` | JSONB | nullable | SessionContext snapshot |

### `audit_log`

Every event emitted during a session, in order.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | UUID | PK | Row identifier |
| `session_id` | UUID | FK → sessions.id (CASCADE) | Owning session |
| `seq` | INTEGER | NOT NULL, UNIQUE(session_id, seq) | Monotonic sequence number |
| `event_type` | VARCHAR(64) | NOT NULL | Event type (e.g., TASK_START) |
| `source` | VARCHAR(128) | nullable | Emitting component |
| `payload` | JSONB | nullable | Event-specific data |
| `created_at` | TIMESTAMP(tz) | NOT NULL, default now() | Event time |
| `causation_id` | VARCHAR(128) | nullable | What caused this event |
| `correlation_id` | VARCHAR(128) | nullable | Trace across events |
| `event_id` | VARCHAR(128) | UNIQUE | Deduplicated event ID |

**Key constraint:** `UNIQUE(session_id, seq)` ensures event ordering integrity.

### `checkpoints`

Workflow state snapshots for crash recovery.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PK, autoincrement | Row identifier |
| `session_id` | UUID | FK → sessions.id (CASCADE), indexed | Owning session |
| `node_id` | VARCHAR(128) | NOT NULL | Node where checkpoint was taken |
| `highest_seq` | INTEGER | NOT NULL | Highest event seq at checkpoint |
| `state_json` | JSONB | nullable | Redacted ForgeState snapshot |
| `created_at` | TIMESTAMP(tz) | NOT NULL, default now() | Checkpoint time |

**Index:** `ix_checkpoints_session_id` on `session_id` for fast lookup.

### `learning_outcomes`

Records of task outcomes for future improvement.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | UUID | PK | Outcome identifier |
| `session_id` | UUID | FK → sessions.id (CASCADE) | Owning session |
| `task_id` | VARCHAR(128) | NOT NULL | Which task this outcome is for |
| `outcome_status` | VARCHAR(32) | NOT NULL | success, failure, skipped, etc. |
| `data_json` | JSONB | nullable | Outcome details |
| `created_at` | TIMESTAMP(tz) | NOT NULL, default now() | Recording time |

## Alembic Migrations

Migrations live in `backend/alembic/versions/`.

### Running Migrations

```bash
cd backend

# Apply all migrations
DATABASE_URL=postgresql://forge:forge@localhost:5432/forge alembic upgrade head

# Check current revision
alembic current

# Generate a new migration after model changes
alembic revision --autogenerate -m "description"

# Rollback one step
alembic downgrade -1
```

### Current Migrations

| Revision | Description |
|----------|-------------|
| `001` | Initial schema — sessions, audit_log, checkpoints, learning_outcomes |

### Alembic Configuration

The `alembic/env.py` reads `DATABASE_URL` from the environment. The DSN is normalized (strips `+asyncpg` suffix) for compatibility with both sync Alembic and async asyncpg.

## Connection Pool

**File:** `backend/app/db/pool.py`

```python
async def create_pool() -> asyncpg.Pool:
    """Create connection pool from DATABASE_URL env var."""
    # Reads DATABASE_URL, DATABASE_POOL_SIZE (default 10)
    # min_size=2, max_size=pool_size

async def get_pool() -> asyncpg.Pool:
    """Get existing pool. Raises if not initialized."""

async def close_pool() -> None:
    """Close pool on shutdown."""
```

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | — | PostgreSQL connection string |
| `DATABASE_POOL_SIZE` | 10 | Maximum pool connections |

**Lifecycle:**
- `create_pool()` called during application startup (lifespan)
- `close_pool()` called during application shutdown
- All store modules accept the pool as a parameter

## Store Modules

Each store provides async CRUD operations using the connection pool.

### `session_store.py`

```python
async def create_session(pool, session_data: dict) -> dict
async def get_session(pool, session_id: str) -> dict | None
async def list_sessions(pool) -> list[dict]
async def update_session(pool, session_id: str, updates: dict) -> dict
async def delete_session(pool, session_id: str) -> bool
```

### `audit_store.py`

```python
async def record_event(pool, session_id: str, event_data: dict) -> None
async def get_events(pool, session_id: str, since_seq: int = 0) -> list[dict]
async def get_decisions(pool, session_id: str) -> list[dict]
```

### `checkpoint_store.py`

```python
async def write_checkpoint(pool, session_id, node_id, highest_seq, state_json) -> None
async def get_latest_checkpoint(pool, session_id: str) -> dict | None
async def list_non_terminal_sessions(pool) -> list[dict]
```

### `learning_store.py`

```python
async def record_outcome(pool, session_id, task_id, outcome_status, data) -> None
async def get_outcomes(pool, session_id: str) -> list[dict]
```

## How Stores Replace In-Memory Implementations

The runtime currently uses in-memory implementations (defined in `bootstrap.py`). The PostgreSQL stores are drop-in replacements:

```python
# In-memory (current default in bootstrap.py)
class _InMemoryCheckpointStore:
    async def write_checkpoint(...): self._checkpoints[session_id] = {...}
    async def get_latest_checkpoint(...): return self._checkpoints.get(session_id)

# PostgreSQL (drop-in replacement)
from app.db.pool import get_pool
from app.db import checkpoint_store

pool = await get_pool()
await checkpoint_store.write_checkpoint(pool, session_id, node_id, seq, state)
latest = await checkpoint_store.get_latest_checkpoint(pool, session_id)
```

The swap happens at the `RuntimeDeps` assembly level — change which implementation is injected and all consumers work unchanged.

## Docker Setup

The `docker-compose.yml` starts PostgreSQL automatically:

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
```

After starting, run migrations:

```bash
DATABASE_URL=postgresql://forge:forge@localhost:5432/forge alembic upgrade head
```
