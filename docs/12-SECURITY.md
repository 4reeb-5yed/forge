# Security

Forge executes AI-generated code autonomously. This document covers the security hardening measures that limit blast radius when the AI produces destructive, exfiltrating, or out-of-scope code.

## Threat Model

| Threat | Vector | Mitigation |
|--------|--------|------------|
| AI writes destructive commands | LLM hallucination or prompt injection | Docker sandbox (no host access), scope check blocks sensitive paths |
| Credential exfiltration | Code reads env vars and sends them to external server | Network disabled (`--network none`), only `OPENROUTER_API_KEY` passed |
| Lateral movement | Code writes outside workspace | Read-only root filesystem, workspace is the only writable mount |
| Resource exhaustion | Infinite loops, fork bombs | Memory limit (2G), CPU limit (2.0), PID limit (256), timeout (5 min) |
| Privilege escalation | Code exploits container runtime | `--cap-drop ALL`, `--security-opt no-new-privileges`, non-root user (uid 1000) |
| Sensitive file modification | AI modifies CI pipelines, secrets, Docker configs | Pre-commit scope check blocks changes to protected paths |
| Workspace exhaustion | Unbounded workspace creation | Hard ceiling on concurrent workspaces (default 10) |

## Security Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ Forge Backend Process                                                │
│                                                                     │
│  ┌─────────────┐    ┌──────────────────┐    ┌──────────────────┐  │
│  │ Workflow     │───▶│ SandboxedAider   │───▶│ Docker Container │  │
│  │ (commit     │    │ Tool             │    │ (ephemeral)      │  │
│  │  node)      │    └──────────────────┘    │                  │  │
│  │             │                            │ • --network none │  │
│  │ scope_check │                            │ • --read-only    │  │
│  │ ──────────▶ │                            │ • --cap-drop ALL │  │
│  │ blocks bad  │                            │ • uid 1000       │  │
│  │ commits     │                            │ • mem/cpu/pid    │  │
│  └─────────────┘                            └──────────────────┘  │
│                                                                     │
│  Secrets boundary:                                                  │
│  ─────────────────                                                  │
│  GITHUB_TOKEN ──── stays in GitHubVCS adapter (never in sandbox)   │
│  DATABASE_URL ──── stays in DB layer (never in sandbox)            │
│  OPENROUTER_API_KEY ──── passed to sandbox (Aider needs it for AI) │
└─────────────────────────────────────────────────────────────────────┘
```

## Layer 1: Docker Sandbox (SandboxedAiderTool)

**File:** `backend/app/adapters/sandboxed_aider.py`

Every coding task runs in an ephemeral Docker container with maximum restriction:

| Security Control | Docker Flag | Purpose |
|-----------------|-------------|---------|
| Network isolation | `--network none` | No outbound connections — prevents data exfiltration |
| Non-root user | `--user 1000:1000` | Limits filesystem operations |
| Read-only rootfs | `--read-only` | Only `/workspace` and `/tmp` are writable |
| Capability drop | `--cap-drop ALL` | No special kernel capabilities |
| No privilege escalation | `--security-opt no-new-privileges` | Cannot gain privileges via setuid/setgid |
| Memory limit | `--memory 2g` | Prevents OOM of host |
| CPU limit | `--cpus 2.0` | Prevents CPU starvation |
| PID limit | `--pids-limit 256` | Prevents fork bombs |
| Auto-remove | `--rm` | Container destroyed on exit |
| Timeout backstop | `--stop-timeout` + asyncio | Killed after 5 minutes |
| Minimal writable tmpfs | `--tmpfs /tmp:rw,noexec,nosuid,size=512m` | Temp files allowed but not executable |

### Secret Isolation

The sandbox receives **only** `OPENROUTER_API_KEY` (required for AI model calls). These secrets are **never** passed:

- `GITHUB_TOKEN` — used exclusively by the `GitHubVCS` adapter on a separate code path
- `DATABASE_URL` — used exclusively by the persistence layer
- `FORGE_API_TOKEN` — used exclusively by the API auth layer
- Any other host environment variables

### Sandbox Selection

Controlled by the `FORGE_USE_SANDBOX` environment variable:

| Value | Behavior |
|-------|----------|
| `auto` (default) | Uses Docker sandbox if `docker` is on PATH, falls back to direct Aider |
| `always` | Always uses Docker sandbox (fails if Docker unavailable) |
| `never` | Always uses direct Aider subprocess (no sandbox) |

### Sandbox Image

Build the sandbox image:

```bash
cd backend
docker build -t forge-aider-sandbox:latest -f Dockerfile.sandbox .
```

The image (`Dockerfile.sandbox`) is minimal: Python 3.11-slim + git + aider-chat, running as uid 1000.

## Layer 2: Pre-Commit Scope Check

**File:** `backend/app/runtime/verification/scope_check.py`

Before the commit node accepts AI-generated changes, `check_diff_scope()` verifies the diff doesn't touch protected paths.

### Always-Blocked Paths

These paths cannot be modified by AI-generated code under any circumstances:

```
.github/workflows/    — CI/CD pipeline definitions
.github/actions/      — Custom GitHub actions
.env                  — Environment variables
.env.local            — Local environment overrides
.env.production       — Production secrets
secrets/              — Any secrets directory
.ssh/                 — SSH keys
Dockerfile            — Container build definition
docker-compose.yml    — Infrastructure definition
.kiro/                — Kiro spec files
```

### Suspicious Patterns (Also Blocked)

Files matching these patterns are blocked:

```
*.pem, *.key, *.cert  — Cryptographic materials
id_rsa, id_ed25519    — SSH private keys
.npmrc, .pypirc       — Package registry credentials
```

### Task-Scoped Paths

When `allowed_paths` is specified (per-task scope), changes outside those paths are rejected even if they aren't in the blocked list. This catches AI bugs that modify unrelated code.

### What Happens on Block

1. The commit is rejected (changes are NOT committed)
2. An `ERROR` event is emitted to the audit trail with:
   - Which files were blocked
   - Which paths were out of scope
   - The reason string
3. The workflow can retry or escalate via the policy engine

## Layer 3: Workspace Limits

**File:** `backend/app/runtime/workspace/__init__.py`

The `WorkspaceManager.create()` method enforces a hard ceiling on concurrent active workspaces:

```python
# Default: 10 concurrent workspaces maximum
await workspace_manager.create(task_id, session_id, max_concurrent=10)
# Raises WorkspaceLimitExceededError if 10 are already active
```

This prevents disk exhaustion and container sprawl when many tasks run concurrently.

## Layer 4: Diff Audit Logging

After every sandboxed execution, `SandboxedAiderTool` captures the workspace `git diff` and appends it to the `ToolResult.output`. This ensures:

- Every AI-generated change is visible in the audit trail
- Even if the scope check passes, the diff is recorded for human review
- Diffs are capped at 50KB to prevent audit bloat

The audit section appears in the output as:

```
--- WORKSPACE DIFF (post-execution) ---
<full git diff content>
--- END DIFF ---
```

## Security Checklist for Operators

- [ ] Build the sandbox image before first use: `docker build -t forge-aider-sandbox:latest -f Dockerfile.sandbox .`
- [ ] Set `FORGE_USE_SANDBOX=always` in production (fail-closed if Docker unavailable)
- [ ] Rotate `OPENROUTER_API_KEY` periodically (it's the only secret exposed to AI)
- [ ] Monitor audit trail for `commit_blocked` events (indicates AI attempted sensitive modifications)
- [ ] Set `FORGE_API_TOKEN` to a strong random value
- [ ] Never expose PostgreSQL port (5432) externally
- [ ] Use TLS termination in front of the API
- [ ] Review workspace limits (`max_concurrent`) based on available disk

## Comparison: Sandboxed vs. Unsandboxed

| Aspect | AiderTool (unsandboxed) | SandboxedAiderTool |
|--------|------------------------|-------------------|
| Host filesystem | Full access (same as backend process) | Only `/workspace` mount |
| Network | Full access | `--network none` |
| Credentials | All host env vars accessible | Only `OPENROUTER_API_KEY` |
| Resource limits | None (inherits process limits) | Memory, CPU, PID hard caps |
| User privileges | Same as backend process | uid 1000, no capabilities |
| Blast radius on compromise | Entire host | Single workspace directory |
| Recovery from fork bomb | Process/host may crash | Container killed by PID limit |
| Audit | stdout/stderr only | stdout/stderr + full git diff |
