# Forge

**Autonomous Software Engineering Runtime**

Forge is an autonomous software engineering runtime. Supply a GitHub repository URL and a plain-English goal — Forge plans, builds, reviews, verifies, and commits code, streaming every decision back in real time.

## What It Does

```
Developer: "Add user authentication with JWT"
    ↓
Forge: clarify → architect → plan → execute → verify → commit → push
    ↓
Result: Working code committed to your repo with full audit trail
```

## Quick Start

### Option 1: Docker (recommended)

```bash
# Clone and configure
git clone https://github.com/4reeb-5yed/forge.git
cd forge
cp .env.docker .env.docker.local
# Edit .env.docker.local with your API keys (OPENROUTER_API_KEY, GITHUB_TOKEN)

# Start everything
docker-compose up -d

# Run database migrations
cd backend
DATABASE_URL=postgresql://forge:forge@localhost:5432/forge alembic upgrade head

# The API is now running at http://localhost:8000
```

### Option 2: Local Development

```bash
cd backend
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
cp .env.example .env  # Edit with your keys

# Run tests (1,240+ tests, ~5 minutes)
pytest

# Start the server
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Option 3: Frontend

```bash
cd frontend
npm install
npm run dev
# Open http://localhost:3000
```

### Invoke a Build

```bash
curl -X POST http://localhost:8000/workflow/invoke \
  -H "Content-Type: application/json" \
  -d '{"message": "Add user authentication with JWT", "session_id": "demo-1"}'
```

## Architecture

```
┌─────────────────────────────────────────────┐
│  Frontend (Next.js + Tailwind)               │
├─────────────────────────────────────────────┤
│  Application (FastAPI REST + WebSocket)       │
├─────────────────────────────────────────────┤
│  Workflow (LangGraph state machine)           │
├─────────────────────────────────────────────┤
│  Runtime (asyncio core — 27 modules)         │
├─────────────────────────────────────────────┤
│  Adapters (OpenRouter, GitHub, Aider)        │
├─────────────────────────────────────────────┤
│  Infrastructure (PostgreSQL, Docker)          │
└─────────────────────────────────────────────┘
```

## Project Structure

```
forge/
├── frontend/                   # Next.js responsive UI
│   ├── app/                   # App Router pages
│   ├── components/            # React components
│   └── package.json
├── backend/
│   ├── app/
│   │   ├── api/               # REST + WebSocket endpoints + auth
│   │   ├── adapters/          # OpenRouter, GitHub VCS, Aider tool
│   │   ├── workflow/          # LangGraph state machine (13 nodes)
│   │   ├── runtime/           # Core logic (27 modules, 1,240+ tests)
│   │   └── db/               # PostgreSQL stores (asyncpg)
│   ├── alembic/              # Database migrations
│   ├── config/               # YAML configuration
│   ├── tests/                # Unit + property-based tests
│   ├── Dockerfile
│   └── main.py               # uvicorn entry point
├── docker-compose.yml         # PostgreSQL + Forge API
└── .env.docker               # Docker environment template
```

## Workflow

The LangGraph state machine drives builds through:

```
intake → classify → clarify → architect → plan → [execute → verify → commit]* → doc_update → finalize
```

Each node delegates to an existing runtime component. Conditional routing handles:
- **Status queries** → answered from inspector without AI
- **Interrupts** → pause/resume/redirect/stop within 2 seconds
- **Verification failures** → policy engine decides retry/escalate/skip

## Adapters

| Adapter | Service | Purpose |
|---------|---------|---------|
| OpenRouter | openrouter.ai | AI completions (all models via single API) |
| GitHub VCS | github.com | Clone, commit, push (token auth) |
| Aider | aider CLI | Coding tool (subprocess with timeout) |

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/workflow/invoke` | Run a full build |
| GET | `/health` | Health check |
| POST | `/sessions` | Create session |
| GET | `/sessions` | List sessions |
| GET | `/sessions/{id}` | Get session |
| DELETE | `/sessions/{id}` | Delete session |
| POST | `/sessions/{id}/interrupt` | Pause build |
| POST | `/sessions/{id}/resume` | Resume |
| POST | `/sessions/{id}/stop` | Stop |
| GET | `/sessions/{id}/status` | Runtime status |
| GET | `/sessions/{id}/explain` | Last decision |
| WS | `/sessions/{id}/events` | Real-time event stream |

Auth: `Authorization: Bearer <FORGE_API_TOKEN>` on all endpoints.

## Configuration

| File | Purpose |
|------|---------|
| `config/models.yaml` | AI provider fallback chains per role |
| `config/policies.yaml` | Retry budget + escalation rules |
| `config/rate_limits.yaml` | Per-session token/cost limits |
| `config/tools.yaml` | Coding tool enablement |
| `config/verification.yaml` | Verifier stage definitions |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key for AI completions |
| `GITHUB_TOKEN` | Yes | GitHub personal access token |
| `DATABASE_URL` | For Docker | PostgreSQL connection string |
| `FORGE_API_TOKEN` | Yes | Bearer token for API auth |
| `AIDER_MODEL` | No | Model for Aider (default: claude-sonnet-4-20250514) |

## Testing

```bash
cd backend
pytest                    # Full suite (1,240+ tests)
pytest -x                 # Stop on first failure
pytest tests/test_api.py  # Specific module
pytest -k "properties"    # Property-based tests only
```

## Key Design Decisions

- **Event Bus as single source of truth** — audit, WebSocket, learning are all subscribers
- **Circuit breaker per AI provider** — dead providers ejected in milliseconds
- **Secrets never persisted** — redacted at every serialization boundary
- **Deterministic intent classification** — "stop" never depends on AI being reachable
- **Workspace isolation** — each task runs in a sandboxed copy, never touches canonical repo

## License

Private — all rights reserved.
