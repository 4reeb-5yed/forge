# Forge

**Autonomous Software Engineering Runtime**

Forge is an autonomous software engineering runtime. Supply a GitHub repository URL and a plain-English goal — Forge plans, builds, reviews, verifies, and commits code, streaming every decision back in real time.

> **Production Ready** — PostgreSQL persistence, approval gates, concurrent builds, sandbox enforcement, timeouts, checkpoint recovery, and learning engine included.

## Documentation

Comprehensive documentation lives in [`docs/`](./docs/README.md):

| Doc | Description |
|-----|-------------|
| [Overview](./docs/01-OVERVIEW.md) | What Forge is, current status, tech stack |
| [Architecture](./docs/02-ARCHITECTURE.md) | 6-layer design, event bus, data flow |
| [Runtime Modules](./docs/03-RUNTIME-MODULES.md) | All 27 runtime modules with APIs and events |
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

## Production Features

| Feature | Description |
|---------|-------------|
| **PostgreSQL Persistence** | Session, audit, and checkpoint stores backed by PostgreSQL |
| **Approval Gates** | Human-in-the-loop review before commits with diff viewer |
| **Concurrent Builds** | Parallel session execution with priority-based scheduling |
| **Sandbox Enforcement** | `FORGE_USE_SANDBOX=always` for production security |
| **Build Timeouts** | Auto-stop builds exceeding configurable timeout (default: 30 min) |
| **Checkpoint Recovery** | Resume builds from last checkpoint after crashes |
| **Learning Engine** | Pattern analysis for model/provider health and recommendations |
| **Real-time Streaming** | AI token streaming to frontend via WebSocket events |

## Quick Start

### Option 1: Docker (recommended)

```bash
# Clone and configure
git clone https://github.com/4reeb-5yed/forge.git
cd forge
cp .env.docker .env.docker.local
# Edit .env.docker.local with your API keys

# Build the sandbox image (required for secure AI code execution)
cd backend
docker build -t forge-aider-sandbox:latest -f Dockerfile.sandbox .
cd ..

# Start everything (includes Docker socket mount for sandbox)
docker-compose up -d

# Verify sandbox access works inside the container
docker-compose exec forge-api docker version

# Run database migrations
cd backend
DATABASE_URL=postgresql://forge:forge@localhost:5432/forge alembic upgrade head

# The API is now running at http://localhost:8000
# Open http://localhost:3000 for the frontend Setup Wizard
```

### Option 2: Local Development

```bash
cd backend
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
cp .env.example .env  # Edit with your keys

# Run tests
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
  -H "Authorization: Bearer $FORGE_API_TOKEN" \
  -d '{"message": "Add user authentication with JWT", "session_id": "demo-1"}'
```

> All endpoints require `Authorization: Bearer <token>`. Set `FORGE_AUTH_DISABLED=true` in `.env` to skip auth during development.

## Architecture

```
┌─────────────────────────────────────────────┐
│  Frontend (Next.js + Tailwind)               │
│  • Approval Banner & Modal                    │
│  • Real-time Token Streaming                  │
├─────────────────────────────────────────────┤
│  Application (FastAPI REST + WebSocket)       │
│  • Approval API, Recovery API                 │
├─────────────────────────────────────────────┤
│  Workflow (LangGraph state machine)           │
│  • Checkpoint Middleware                      │
├─────────────────────────────────────────────┤
│  Runtime (asyncio core — 30+ runtime modules) │
│  • SessionScheduler, BuildTimeoutManager      │
│  • ApprovalManager, LearningEngine            │
│  • StreamRouter, CheckpointMiddleware         │
├─────────────────────────────────────────────┤
│  Adapters (OpenRouter, GitHub, Aider)        │
├─────────────────────────────────────────────┤
│  Infrastructure (PostgreSQL, Docker)          │
│  • Session Store, Audit Store                 │
│  • Checkpoint Store, Learning Store           │
└─────────────────────────────────────────────┘
```

## Project Structure

```
forge/
├── frontend/                   # Next.js responsive UI
│   ├── app/                   # App Router pages
│   ├── components/            # React components
│   │   ├── ApprovalBanner.tsx  # Approval gate alert
│   │   └── ApprovalModal.tsx   # Diff review modal
│   └── lib/
│       └── approval.ts         # Approval API client
├── backend/
│   ├── app/
│   │   ├── api/               # REST + WebSocket endpoints
│   │   │   ├── approval.py    # Approval gates API
│   │   │   ├── recovery.py    # Checkpoint recovery API
│   │   │   └── ...
│   │   ├── adapters/          # OpenRouter, GitHub VCS, Aider, Sandboxed Aider
│   │   ├── workflow/          # LangGraph state machine (13 nodes)
│   │   │   ├── checkpoint_middleware.py  # Auto-checkpointing
│   │   │   └── ...
│   │   ├── runtime/           # Core logic (30+ runtime modules)
│   │   │   ├── approval.py         # Human approval gates
│   │   │   ├── scheduler.py        # Concurrent builds
│   │   │   ├── build_timeout.py    # Auto-stop timeouts
│   │   │   ├── persistence.py       # PostgreSQL stores
│   │   │   ├── learning_engine.py   # Pattern analysis
│   │   │   ├── stream_router.py    # AI token streaming
│   │   │   └── ...
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
- **Checkpointing** → automatic after each node for crash recovery

## Adapters

| Adapter | Service | Purpose |
|---------|---------|---------|
| OpenRouter | openrouter.ai | AI completions (all models via single API) |
| GitHub VCS | github.com | Clone, commit, push (token auth) |
| Aider | aider CLI | Coding tool (subprocess with timeout) |
| Sandboxed Aider | Docker + aider | Coding tool in isolated container (recommended) |

## API

### Core Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/workflow/invoke` | Run a full build |
| GET | `/health` | Health check |
| POST | `/sessions` | Create session |
| GET | `/sessions` | List sessions |
| GET | `/sessions/{id}` | Get session |
| DELETE | `/sessions/{id}` | Delete session |
| POST | `/sessions/{id}/messages` | Send developer message |
| POST | `/sessions/{id}/interrupt` | Pause build |
| POST | `/sessions/{id}/resume` | Resume |
| POST | `/sessions/{id}/redirect` | Redirect paused build |
| POST | `/sessions/{id}/stop` | Stop |
| GET | `/sessions/{id}/status` | Runtime status |
| GET | `/sessions/{id}/explain` | Last decision |
| WS | `/sessions/{id}/events` | Real-time event stream |

### Approval Gates

| Method | Path | Description |
|--------|------|-------------|
| GET | `/approval/pending/{session_id}` | List pending approvals |
| GET | `/approval/{request_id}` | Get approval request |
| GET | `/approval/{request_id}/diff` | Get full diff |
| POST | `/approval/{request_id}/approve` | Approve changes |
| POST | `/approval/{request_id}/reject` | Reject changes |

### Recovery

| Method | Path | Description |
|--------|------|-------------|
| GET | `/recovery/sessions` | List recoverable sessions |
| GET | `/recovery/sessions/{id}` | Get checkpoint data |
| POST | `/recovery/sessions/{id}/resume` | Resume from checkpoint |

### Configuration

| Method | Path | Description |
|--------|------|-------------|
| GET | `/capabilities` | Registry summary |
| GET | `/config` | Current configuration (redacted) |
| PUT | `/config` | Update configuration |
| POST | `/config/test` | Test API key validity |
| GET | `/config/health` | Per-component health |
| GET | `/config/models` | Available AI models |

Auth: `Authorization: Bearer <FORGE_API_TOKEN>` on all endpoints.

## Configuration

| File | Purpose |
|------|---------|
| `config/adapters.yaml` | Adapter discovery (OpenRouter, GitHub, Aider) |
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
| `DATABASE_URL` | Production | PostgreSQL connection string |
| `FORGE_API_TOKEN` | Yes | Bearer token for API auth |
| `FORGE_AUTH_DISABLED` | No | Set `true` to disable auth (development only) |
| `AIDER_MODEL` | No | Model for Aider subprocess (default: claude-sonnet-4-20250514) |
| `FORGE_MODEL` | No | AI model for workflow (default: nvidia/nemotron-3-ultra-550b-a55b:free) |
| `FORGE_USE_SANDBOX` | No | Sandbox mode: `always` (recommended), `auto`, `never` |
| `FORGE_BUILD_TIMEOUT_SECONDS` | No | Build timeout (default: 1800 = 30 minutes) |
| `FORGE_MAX_CONCURRENT` | No | Max concurrent builds (default: 3) |
| `FORGE_CHECKPOINT_INTERVAL` | No | Checkpoint interval in seconds (default: 60) |
| `FORGE_LEARNING_WINDOW_DAYS` | No | Learning analysis window (default: 7) |
| `NEXT_PUBLIC_WS_URL` | No | WebSocket backend URL for frontend |

## Testing

```bash
cd backend
pytest                    # Full test suite
pytest -x                 # Stop on first failure
pytest tests/test_api.py  # Specific module
pytest -k "properties"    # Property-based tests only
```

## Key Design Decisions

- **Event Bus as single source of truth** — audit, WebSocket, learning, approvals are all subscribers
- **Circuit breaker per AI provider** — dead providers ejected in milliseconds
- **Secrets never persisted in plaintext** — redacted at every serialization boundary, config file uses 0600 permissions
- **Deterministic intent classification** — "stop" never depends on AI being reachable
- **Workspace sandboxing** — each task runs in a Docker container with `--network none`, no host access, resource limits, and read-only rootfs. Only `OPENROUTER_API_KEY` and `HOME` reach the sandbox.
- **Pre-commit scope check** — AI changes to CI pipelines, secrets, Docker configs, and env files are blocked before commit
- **Diff audit trail** — every AI-generated change is captured as a git diff and recorded in the audit log
- **Approval gates** — human review before commits with full diff visibility
- **Checkpoint recovery** — automatic state persistence for crash recovery
- **Setup Wizard** — first-run configuration via `/setup` page; no `.env` editing required
- **Structured error responses** — all API errors return ErrorEnvelope with code, category, suggestion
- **Real-time error surfacing** — error events flow from EventBus → WebSocket → frontend toasts + panel
- **Concurrent builds** — multiple sessions run in parallel with priority scheduling
- **Build timeouts** — auto-stop builds exceeding configurable duration
- **Learning engine** — pattern analysis for model/provider health

## License

Private — all rights reserved.
