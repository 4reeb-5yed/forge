# Forge

**Autonomous Software Engineering Runtime**

Forge is an autonomous software engineering runtime. Supply a GitHub repository URL and a plain-English goal — Forge plans, builds, reviews, verifies, and commits code, streaming every decision back in real time.

## Documentation

Comprehensive documentation lives in [`docs/`](./docs/README.md):

| Doc | Description |
|-----|-------------|
| [Overview](./docs/01-OVERVIEW.md) | What Forge is, current status, tech stack |
| [Architecture](./docs/02-ARCHITECTURE.md) | 6-layer design, event bus, data flow |
| [Runtime Modules](./docs/03-RUNTIME-MODULES.md) | All 27 modules with APIs and events |
| [Workflow](./docs/04-WORKFLOW.md) | LangGraph state machine, nodes, routing |
| [Adapters](./docs/05-ADAPTERS.md) | OpenRouter, GitHub VCS, Aider, Sandboxed Aider |
| [Database](./docs/06-DATABASE.md) | PostgreSQL schema, stores, migrations |
| [Frontend](./docs/07-FRONTEND.md) | Next.js UI, components, WebSocket |
| [Deployment](./docs/08-DEPLOYMENT.md) | Docker Compose, env vars, scaling |
| [Testing](./docs/09-TESTING.md) | Test strategy, property-based testing |
| [Future](./docs/10-FUTURE.md) | Roadmap, limitations, tradeoffs |
| [Security](./docs/12-SECURITY.md) | Workspace sandboxing, scope checks, secret isolation |

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

# Build the sandbox image (required for secure AI code execution)
cd backend
docker build -t forge-aider-sandbox:latest -f Dockerfile.sandbox .
cd ..

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

# Run tests (1,280+ tests, ~5 minutes)
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
│   │   ├── adapters/          # OpenRouter, GitHub VCS, Aider, Sandboxed Aider
│   │   ├── workflow/          # LangGraph state machine (13 nodes)
│   │   ├── runtime/           # Core logic (27 modules, 1,280+ tests)
│   │   │   ├── verification/  # scope_check.py (pre-commit security)
│   │   │   └── workspace/     # Isolated workspaces with hard limits
│   │   └── db/               # PostgreSQL stores (asyncpg)
│   ├── alembic/              # Database migrations
│   ├── config/               # YAML configuration
│   ├── tests/                # Unit + property-based tests
│   ├── Dockerfile            # Backend image
│   ├── Dockerfile.sandbox    # Aider sandbox image
│   └── main.py               # uvicorn entry point
├── docs/                      # 12 documentation files
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
| Sandboxed Aider | Docker + aider | Coding tool in isolated container (recommended) |

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
| `FORGE_USE_SANDBOX` | No | Sandbox mode: `auto` (default), `always`, `never` |

## Testing

```bash
cd backend
pytest                    # Full suite (1,280+ tests)
pytest -x                 # Stop on first failure
pytest tests/test_api.py  # Specific module
pytest -k "properties"    # Property-based tests only
```

## Key Design Decisions

- **Event Bus as single source of truth** — audit, WebSocket, learning are all subscribers
- **Circuit breaker per AI provider** — dead providers ejected in milliseconds
- **Secrets never persisted** — redacted at every serialization boundary
- **Deterministic intent classification** — "stop" never depends on AI being reachable
- **Workspace sandboxing** — each task runs in a Docker container with `--network none`, no host access, resource limits, and read-only rootfs. Only `OPENROUTER_API_KEY` reaches the sandbox.
- **Pre-commit scope check** — AI changes to CI pipelines, secrets, Docker configs, and env files are blocked before commit
- **Diff audit trail** — every AI-generated change is captured as a git diff and recorded in the audit log

## License

Private — all rights reserved.
