# Troubleshooting Guide

This guide covers common issues, their root causes, and solutions for Forge.

## Table of Contents

1. [OpenRouter Rate Limiting](#openrouter-rate-limiting)
2. [Model Configuration](#model-configuration)
3. [GitHub Push Failures](#github-push-failures)
4. [Workflow Execution Errors](#workflow-execution-errors)
5. [Circuit Breaker Issues](#circuit-breaker-issues)

---

## OpenRouter Rate Limiting

### Symptom

API calls fail with 429 errors:
```
Transient error from provider 'openrouter': OpenRouter error: Rate limit exceeded: free-models-per-day
```

### Root Cause

The default model `nvidia/nemotron-3-ultra-550b-a55b:free` is a free-tier model with daily limits that get exhausted quickly.

### Solution

Set the `FORGE_MODEL` environment variable to a paid model:

```bash
# Option 1: Environment variable
export FORGE_MODEL="anthropic/claude-3-haiku"

# Option 2: In .env file
FORGE_MODEL=anthropic/claude-3-haiku
```

### Alternative Free Models

If you must use free models, these are currently available on OpenRouter:
- `google/gemma-4-31b-it:free`
- `cohere/nemotron-3-ultra-550b-a55b:free`

Note: Free models have daily rate limits. For production use, use paid models.

---

## Model Configuration

### Symptom

Forge uses a different model than specified in `config/models.yaml`.

### Root Cause

The ConfigService loads AFTER bootstrap and overwrites the model chain with the `FORGE_MODEL` environment variable. This means `config/models.yaml` is only used if:
1. The config file doesn't exist
2. Parsing fails

### Resolution Order

1. `FORGE_MODEL` environment variable (highest priority)
2. `config/models.yaml` (loaded by bootstrap, then overwritten by ConfigService)
3. Hardcoded default `nvidia/nemotron-3-ultra-550b-a55b:free`

### Best Practice

Always set `FORGE_MODEL` explicitly in your environment:

```bash
FORGE_MODEL=anthropic/claude-3-haiku
```

---

## GitHub Push Failures

### Symptom

Commits succeed but push fails:
```
Commit/push failed: git push failed: remote: Permission to user/repo.git denied to forge-runtime.
fatal: unable to access 'https://github.com/user/repo/': The requested URL returned error 403
```

### Root Cause

The GitHub token provided doesn't have write permissions to the target repository.

### Solutions

1. **Use a GitHub PAT with repo scope:**
   ```bash
   GITHUB_TOKEN=ghp_your_token_with_repo_scope
   ```

2. **Fork the repository and use the fork URL:**
   - Fork the repo to your GitHub account
   - Use your fork URL in the workflow request

3. **Check repository access:**
   - Ensure the token has `repo` scope for private repositories
   - Ensure the token has access to the organization if using org repos

---

## Workflow Execution Errors

### `'int' object is not subscriptable`

#### Symptom

Workflow fails during execute phase:
```
Workflow execution error: 'int' object is not subscriptable
```

#### Root Cause

The AI model generates task IDs as integers (1, 2, 3) in JSON responses, but the `Task` model expects string IDs. This causes a runtime error when the execute node tries to look up tasks.

#### Solution

This issue has been fixed in the architect node. The fix ensures all task IDs and dependencies are converted to strings before creating Task objects.

If you're on an older version, the fix is in `app/workflow/nodes/architect.py`:

```python
# Ensure all task IDs and dependencies are strings (required by Task model)
for t in task_list:
    if "id" in t and not isinstance(t["id"], str):
        t["id"] = str(t["id"])
    if "depends_on" in t:
        t["depends_on"] = [str(x) for x in t["depends_on"]]
```

---

## Circuit Breaker Issues

### Symptom

Circuit breaker shows "closed" with 0 failures despite seeing 429 errors.

### Root Cause

The circuit breaker counts CONSECUTIVE failures, not total failures. If a successful call occurs between rate limit errors, the counter resets.

### Circuit Breaker Configuration

In `app/runtime/router/__init__.py`:

```python
CircuitBreaker(
    failure_threshold=5,      # Open after 5 consecutive failures
    recovery_timeout=60.0,   # Try again after 60 seconds
    expected_exceptions=(RateLimitError, TimeoutError),
)
```

### Monitoring

Check breaker state via logs:
```
[ROUTER] Provider 'openrouter' breaker state: open (failures: 5)
```

### Recovery

The circuit breaker automatically closes after the recovery timeout. If you need immediate recovery during testing:

1. Restart the Forge server
2. Wait for the recovery timeout (60 seconds)

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `FORGE_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` | AI model for workflow |
| `OPENROUTER_API_KEY` | (required) | OpenRouter API key |
| `GITHUB_TOKEN` | (required) | GitHub personal access token |
| `FORGE_API_TOKEN` | (required) | Bearer token for API auth |
| `FORGE_USE_SANDBOX` | `auto` | Sandbox mode: always, auto, never |
| `FORGE_BUILD_TIMEOUT_SECONDS` | 1800 | Build timeout (30 minutes) |
| `FORGE_AUTH_DISABLED` | `false` | Set `true` to disable auth |
| `DATABASE_URL` | (production) | PostgreSQL connection string |

---

## Getting Help

If you encounter an issue not covered here:

1. Check the server logs: `tail -f backend/server.log`
2. Enable debug logging by setting `LOG_LEVEL=DEBUG`
3. Check the [GitHub Issues](https://github.com/4reeb-5yed/forge/issues)
4. Review the [Architecture Documentation](./02-ARCHITECTURE.md)
