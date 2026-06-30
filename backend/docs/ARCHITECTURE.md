# Architecture

## Overview

Forge is a 5-layer asyncio/FastAPI/LangGraph system. Each layer communicates only with its adjacent layer through defined interfaces.

## Layers

### Layer 1: Presentation (Future)
- Next.js web UI
- Renders chat, spec, task checklist, verification stages, commit log from streamed events
- Knows nothing about models, tools, or orchestration

### Layer 2: Application (`app/api/`)
- FastAPI HTTP/WebSocket boundary
- Creates sessions, forwards messages, broadcasts events
- Contains zero engineering logic — pure transport translation
- Depends on: Runtime layer (session, inspector, interrupt handler)

### Layer 3: Runtime (`app/runtime/`)
- The autonomous orchestration core
- Owns all state, emits all events, makes and records all decisions
- Depends on: Adapter layer (via Protocol interfaces only)
- Contains zero HTTP/transport logic

### Layer 4: Adapter (`app/adapters/`)
- Concrete implementations of plugin protocols
- One file per adapter (AI provider, coding tool, VCS, store, verifier)
- Translates one Protocol call into one infrastructure call
- Contains zero business logic

### Layer 5: Infrastructure
- PostgreSQL, ChromaDB, Cloudflare R2, GitHub, Aider, AI providers
- External services — not controlled by Forge code

## Component Responsibilities

| Component | Role | Writes | Reads |
|-----------|------|--------|-------|
| EventBus | Ordered typed delivery | — (transport) | — |
| CapabilityRegistry | What's available now | itself | — |
| Discovery | Probe at boot | Registry (once) | config |
| HealthMonitor | Continuous re-check | Registry | adapters |
| ModelRouter | Resolve role → provider | events | Registry |
| TaskDispatcher | Assign tasks + workspaces | ForgeState | Policy |
| WorkspaceManager | Create/destroy sandboxes | filesystem | Policy |
| VerificationPipeline | Run verifier stages | VerificationResult | Registry |
| PolicyEngine | Decide retry/escalate/skip | — | policies.yaml |
| AuditTrail | Persisted event projection | audit_log | EventBus |
| RuntimeInspector | Query-only "what/why" | — | Audit, Registry |
| SessionBudget | Token/cost governance | budget state | — |
| SecretHolder | Redact at boundaries | memory only | — |
| LearningRecorder | Record outcomes | learning store | EventBus |

## Event Flow

```
Component → emit Event → EventBus → [Audit Trail, WS Broadcaster, Learning Engine]
                                          ↓
                                   RuntimeInspector (reads)
```

The EventBus is the single source of truth. All projections (audit, WebSocket, learning) are subscribers.

## Concurrency Model

- Single-process asyncio core (v1)
- All blocking work (git, subprocess) runs in thread/process executors
- Sequential task execution behind a parallelism seam
- Per-session locks for event ordering
- Circuit breakers with cancellable backoff for transient retries

## Secret Handling

- VCS tokens and API keys stored only in per-session `SecretHolder` (memory)
- State snapshots redacted before checkpoint persistence
- Token references (not raw values) stored in session state
- `redact_or_raise()` aborts if any raw secret survives redaction
