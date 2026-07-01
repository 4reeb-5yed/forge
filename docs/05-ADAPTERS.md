# Adapters

Adapters live in `backend/app/adapters/` and translate protocol calls from the runtime layer into concrete infrastructure calls. They contain zero business logic.

```mermaid
graph LR
    RT[Runtime Layer] -->|Protocol Interface| AD[Adapter]
    AD -->|HTTP/CLI/SDK| INF[Infrastructure]
```

---

## OpenRouter AI Provider

**File:** `backend/app/adapters/openrouter.py`

### What It Does

Provides chat completions via the [OpenRouter](https://openrouter.ai) API, which gives access to all major AI models (Claude, GPT-4, Llama, etc.) through a single API key.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | — | Required. OpenRouter API key |
| Timeout | 60s | Per-request timeout |

### Class: `OpenRouterProvider`

```python
class OpenRouterProvider:
    name = "openrouter"
    
    async def complete(messages: list, model: str, **kwargs) -> str
    async def stream(messages: list, model: str, **kwargs) -> AsyncIterator[str]
    async def health_check() -> Health
    def as_call_adapter() -> Callable  # Returns ModelCallAdapter signature
    async def close() -> None
```

### Error Classification

The adapter classifies HTTP errors into retryable and permanent, via `_classify_error()`:

| HTTP Status | Classification | Behavior |
|-------------|---------------|----------|
| 200 | Success | Return content |
| 401, 403 | `PermanentError` | Circuit breaker opens, no retry |
| 429 | Transient (rate limit) | RuntimeError, will be retried |
| 5xx | Transient (server) | RuntimeError, will be retried |

**Embedded errors in HTTP 200 responses:** OpenRouter sometimes returns HTTP 200 with an `error` key embedded in the JSON body instead of a non-200 status. `complete()` checks for this after a successful response and re-classifies based on `error.code`:

| `error.code` | Classification | Behavior |
|---------------|---------------|----------|
| 401, 403 | `PermanentError` | Circuit breaker opens, no retry |
| other | Transient | RuntimeError, will be retried |

If the embedded error message contains `"ResourceExhausted"` or the word `"limit"` (case-insensitive) — indicating model capacity exhaustion or a rate-limit-like condition — the adapter `await asyncio.sleep(5)` before raising the `RuntimeError`, giving upstream retry logic a better chance of succeeding on the next attempt.

### Streaming

The `stream()` method uses SSE (Server-Sent Events):

```python
async for token in provider.stream(messages, model="anthropic/claude-sonnet-4-20250514"):
    print(token, end="")
```

Parses `data: {...}` lines, skips `[DONE]`, extracts `choices[0].delta.content`.

### Integration with Model Router

The `as_call_adapter()` method returns a closure matching the `ModelCallAdapter` signature expected by the `ModelRouter`:

```python
async def adapter(provider: str, model: str, messages: list, **kwargs) -> str
```

Before forwarding `**kwargs` to `complete()`, the closure filters them through an `_ALLOWED_PARAMS` allow-list (`temperature`, `top_p`, `max_tokens`, `stop`, `presence_penalty`, `frequency_penalty`, `logit_bias`, `n`, `stream`, `response_format`, `seed`, `tools`, `tool_choice`). Any keys not in this list — such as internal routing params like `estimated_tokens` — are silently stripped and never reach the OpenRouter API.

---

## GitHub VCS

**File:** `backend/app/adapters/github_vcs.py`

### What It Does

Implements VCS operations (clone, commit, push) using `git` subprocess calls. Injects `GITHUB_TOKEN` into clone URLs for authentication.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | — | Required. GitHub personal access token |

### Class: `GitHubVCS`

```python
class GitHubVCS:
    name = "github"
    
    async def clone(url: str, ref: str, dest_path: str) -> None
    async def commit(workspace_path: str, message: str) -> str  # Returns SHA
    async def push(workspace_path: str) -> None
    async def health_check() -> Health
```

### Token Handling & Security

1. **Token injection:** Transforms `https://github.com/owner/repo` into `https://x-access-token:{token}@github.com/owner/repo`. The token must go in the *password* position (using GitHub's documented `x-access-token` placeholder username), not the username position — a username-only userinfo (`{token}@host`) leaves git without a password, and git's http backend then tries to prompt for one interactively, which fails with `could not read Password ... No such device or address` in any non-interactive/non-TTY environment (e.g. inside a Docker container).
2. **Never logged:** The `_run_git()` method never logs arguments that might contain the token
3. **Sanitized errors (clone/push only):** Error messages from `clone()` and `push()` pass through `_sanitize()`, which replaces the token with `***`. Error messages from `commit()` (`git add failed`, `git commit failed`, `git rev-parse failed`) are raised unsanitized — low risk since the token is not part of the commit command, but be aware these paths do not call `_sanitize()`
4. **Memory-only:** Token lives only in the adapter instance, never serialized

### Clone Strategy

```bash
git clone --depth=1 --branch {ref} https://x-access-token:{token}@github.com/owner/repo {dest}
```

- Shallow clone (`--depth=1`) for speed
- Specific ref for determinism
- Destination path is an isolated workspace

### Commit Flow

```python
await vcs.clone(url, "main", workspace_path)       # 1. Clone
# ... Aider modifies files ...
sha = await vcs.commit(workspace_path, "feat: X")  # 2. git add -A && git commit
await vcs.push(workspace_path)                      # 3. git push
```

---

## OpenHands Cloud Coding Tool

**File:** `backend/app/adapters/openhands.py`

### What It Does

Uses the [OpenHands Cloud](https://app.all-hands.dev) API to execute coding tasks against a GitHub repository. Unlike the Aider variants, OpenHands is a hosted service — it selects its own AI model internally (including a free tier), runs the sandboxed execution on OpenHands' infrastructure, and commits results to the repo itself. Forge only creates the conversation and polls for completion.

### Priority in coding tool selection

`bootstrap.py`'s `_create_coding_tool()` picks the coding tool with this priority order:

1. **OpenHandsTool** — used automatically whenever `OPENHANDS_API_KEY` is set, regardless of Docker availability
2. **SandboxedAiderTool** — used if Docker is available and OpenHands is not configured
3. **AiderTool** (direct, unsandboxed) — fallback when neither OpenHands nor Docker is available

OpenHands is checked **first**, ahead of both Aider variants — if `OPENHANDS_API_KEY` is set, Aider is never used.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENHANDS_API_KEY` | — | Required, no fallback. If unset, `execute()` returns a failed `ToolResult` and `health_check()` reports unhealthy |
| Timeout | 300s (5 min) | Total time allowed for the conversation to reach `STOPPED` status |
| Poll interval | 5s | Time between status polls |

### Class: `OpenHandsTool`

```python
class OpenHandsTool:
    name = "openhands"

    async def execute(task_description: str, workspace_path: str, repo_url: str = "") -> ToolResult
    async def health_check() -> Health
```

### API Flow

1. **Extract repo id:** `repo_url` (e.g. `https://github.com/owner/repo.git`) is reduced to `owner/repo` via `_extract_repo_id()`.
2. **Create conversation:** `POST https://app.all-hands.dev/api/conversations` with body:
   ```json
   {
     "repository": "owner/repo",
     "initial_user_msg": "<task_description>"
   }
   ```
   A non-200 response returns a failed `ToolResult` immediately with the status code and response body (truncated to 200 chars).
3. **Poll for completion:** `GET https://app.all-hands.dev/api/conversations/{conversation_id}` every `POLL_INTERVAL` (5s), until either:
   - `status == "STOPPED"` → returns a successful `ToolResult`
   - the overall `timeout` (default 300s) elapses → returns a failed `ToolResult` with a timeout error
   - `STARTING` / `RUNNING` statuses (or any other unrecognized status, or a poll request error) simply continue polling

### Authentication

Unlike OpenRouter and GitHub, OpenHands does **not** use an `Authorization: Bearer` header. It uses a custom header instead:

```python
{
    "X-Session-API-Key": self._api_key,
    "Content-Type": "application/json",
}
```

### Health Check

`health_check()` calls `GET /conversations?limit=1`. If `OPENHANDS_API_KEY` is not set, it short-circuits to unhealthy without making a request. A `200` response means healthy; any other status or exception is reported unhealthy.

---

## Aider Coding Tool

**File:** `backend/app/adapters/aider_tool.py`

### What It Does

Spawns [Aider](https://aider.chat) as a subprocess to execute coding tasks. Aider is an AI-powered coding assistant that makes changes directly to files.

> **Note:** In production, prefer the **SandboxedAiderTool** (see below) which runs Aider inside a Docker container with full isolation. The direct `AiderTool` is a fallback for environments without Docker.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AIDER_MODEL` | `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` | Model for Aider to use |
| Timeout | 300s (5 min) | Process kill on timeout |

> **Different default from SandboxedAiderTool:** `AiderTool`'s hardcoded `DEFAULT_MODEL` (`openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`, a free-tier model) is **not the same** as `SandboxedAiderTool`'s hardcoded `DEFAULT_MODEL` (`openrouter/anthropic/claude-3-haiku`, see below). Both read from the same `AIDER_MODEL` env var if set, but if unset they fall back to different values. Don't assume the two tools share configuration — check the source of the tool actually in use. Both defaults are, correctly, OpenRouter-routed model names — do not set `AIDER_MODEL` to a raw provider model name (e.g. `claude-sonnet-4-20250514`), as that will cause silent, zero-effect failures in the sandboxed tool (see the Sandboxed Aider Tool section below).

### Class: `AiderTool`

```python
class AiderTool:
    name = "aider"
    
    async def execute(task_description: str, workspace_path: str) -> ToolResult
    async def health_check() -> Health
```

### Subprocess Execution

```bash
aider --yes --no-git --model {model} --message "{task_description}"
```

Flags:
- `--yes` — Auto-confirm all changes
- `--no-git` — Don't make git commits (Forge handles commits separately)
- `--model` — Which AI model Aider should use
- `--message` — The coding task in natural language

### Timeout Handling

```python
try:
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
except asyncio.TimeoutError:
    proc.kill()
    await proc.wait()
    return ToolResult(success=False, error="Aider timed out after 300s")
```

### ToolResult

```python
@dataclass
class ToolResult:
    success: bool      # True if exit code == 0
    output: str        # stdout
    error: str         # stderr (only populated on failure)
```

---

## Sandboxed Aider Tool (Recommended)

**File:** `backend/app/adapters/sandboxed_aider.py`

### What It Does

Drop-in replacement for `AiderTool` that runs every coding task inside an ephemeral Docker container with maximum security restrictions. This is the **recommended** coding tool for production use.

### Why Use It

The direct `AiderTool` runs with the same privileges as the Forge backend process — any AI-generated command has full host access. The sandboxed version isolates execution so that even malicious or buggy AI output cannot escape the workspace.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `FORGE_USE_SANDBOX` | `auto` | `auto` / `always` / `never` — controls which tool is used |
| `AIDER_MODEL` | `openrouter/anthropic/claude-3-haiku` | Model for Aider. Must be an OpenRouter-routed name — see note below |
| Timeout | 300s | Container killed on timeout |
| Image | `forge-aider-sandbox:latest` | Docker image to use |
| Memory | `2g` | Container memory limit |
| CPU | `2.0` | Container CPU limit |
| PIDs | `256` | Max processes inside container |

> **Model must be OpenRouter-routed:** the sandbox only ever receives `OPENROUTER_API_KEY` (never `ANTHROPIC_API_KEY` or any other provider key, by design). A raw provider model name like `claude-sonnet-4-20250514` makes Aider try to call Anthropic directly, which fails with `litellm.AuthenticationError: Missing Anthropic API Key` — a failure Aider does not treat as fatal, so it exits 0 with zero file changes and no error surfaced to Forge. Always use the `openrouter/<provider>/<model>` form. Also be mindful of account credit limits: pricier models (e.g. `openrouter/anthropic/claude-sonnet-4`) can fail with a 402 "requires more credits" error on accounts with limited balance — this is likewise not surfaced as a tool error and silently produces zero file changes. `claude-3-haiku` is used as the default specifically because it's low-cost.

### Class: `SandboxedAiderTool`

```python
class SandboxedAiderTool:
    name = "aider-sandboxed"
    
    async def execute(task_description: str, workspace_path: str) -> ToolResult
    async def health_check() -> Health
```

### Security Properties

> **Known gap:** despite the table below, network isolation is **not currently enforced**. `bootstrap.py` constructs this tool with `allow_network=True` as a functional stopgap, because `--network none` makes it impossible for Aider to reach OpenRouter at all. Full details, rationale, and the intended fix are in [docs/12-SECURITY.md](./12-SECURITY.md#known-gap-network-egress-is-not-scoped-to-openrouter).

| Control | Implementation |
|---------|---------------|
| Network isolation | `--network none` when `allow_network=False` (default) — **currently overridden to `allow_network=True` in bootstrap.py; see gap above** |
| Non-root execution | `--user 1000:1000` |
| Read-only root filesystem | `--read-only` |
| All capabilities dropped | `--cap-drop ALL` |
| No privilege escalation | `--security-opt no-new-privileges` |
| Resource limits | `--memory`, `--cpus`, `--pids-limit` |
| Workspace-only mount | `-v {workspace}:/workspace:rw` |
| Secret isolation | Only `OPENROUTER_API_KEY` passed |
| Diff audit logging | Captures `git diff` after execution |

### Diff Audit Logging

After Aider completes, the tool captures the workspace `git diff` and appends it to the `ToolResult.output`. This ensures every AI-generated change is recorded in the audit trail regardless of whether the commit succeeds:

```
--- WORKSPACE DIFF (post-execution) ---
+added line
-removed line
--- END DIFF ---
```

Diffs are capped at 50KB to prevent bloat.

### Building the Sandbox Image

```bash
cd backend
docker build -t forge-aider-sandbox:latest -f Dockerfile.sandbox .
```

### Health Check

The health check verifies:
1. Docker daemon is running
2. The sandbox image (`forge-aider-sandbox:latest`) exists locally

```python
health = await tool.health_check()
# Health.unhealthy("Sandbox image 'forge-aider-sandbox:latest' not found...")
```

---

## Adding a New Adapter

### Step 1: Define the Protocol (if new)

In `backend/app/runtime/protocols.py`:

```python
from typing import Protocol

class NewServiceProtocol(Protocol):
    name: str
    
    async def do_thing(self, input: str) -> str: ...
    async def health_check(self) -> Health: ...
```

### Step 2: Implement the Adapter

Create `backend/app/adapters/new_service.py`:

```python
from app.shared import Health  # Canonical source for shared types

class NewServiceAdapter:
    name = "new_service"
    
    def __init__(self, *, api_key: str | None = None):
        self._api_key = api_key or os.environ.get("NEW_SERVICE_KEY", "")
    
    async def do_thing(self, input: str) -> str:
        # Make the infrastructure call
        ...
    
    async def health_check(self) -> Health:
        if not self._api_key:
            return Health.unhealthy("NEW_SERVICE_KEY not set")
        # Probe the service
        return Health.healthy()
```

### Step 3: Register in Discovery

Add a probe function and wire it into the discovery probe_map so the adapter is discovered at boot time.

### Step 4: Add Health Check

The HealthMonitor will periodically call `health_check()` to maintain registry accuracy.

### Rules for Adapters

1. **No business logic** — An adapter translates one call to one infrastructure call
2. **No back-imports** — Import shared types from `app.shared` (Health, ToolResult, PermanentError). These are the canonical sources. Never import from `app.runtime` directly.
3. **Error classification** — Distinguish permanent errors (auth) from transient (rate limit, timeout)
4. **Secret safety** — Never log tokens, sanitize error messages
5. **Health check** — Every adapter must implement `health_check() -> Health`
