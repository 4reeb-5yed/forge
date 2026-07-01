# Audit: PR #7 — "Fix workflow end-to-end: clone repo, create file, commit to GitHub"

**Branch:** `feature/workflow-fixes`
**Base:** `main`
**Diverges at:** commit `11e82ea` ("Wire REAL git operations: clone → Aider → commit → push to GitHub")
**Scope:** 20 commits, 62 files changed, +4536/−148 lines
**GitHub state at time of audit:** open, unmerged, `mergeable_state: dirty`

This audit compares PR #7 against the current `main` branch, which has since gone through two additional rounds of work not present on PR #7's branch:

1. A documentation audit reconciling `README.md`/`docs/` with actual code behavior.
2. A real end-to-end bug-fix pass (branch `fix/e2e-runtime-bugs`, merged as `main` commit `7a77ad0`) that fixed, and verified via a live build against a real GitHub repo:
   - `HealthMonitor`'s empty `probe_map={}`, which silently deregistered the OpenRouter capability ~90 seconds after every boot regardless of API key validity.
   - `GitHubVCS._inject_token()` placing the token in the URL username position with no password, causing git to hang on an interactive password prompt in non-TTY environments.
   - 9 `Event.create()` call sites across `router`, `verification`, `recovery`, `interrupt`, `policies`, `learning`, `dispatcher`, `commit`, and `budget` missing the required `event_id` field, silently dropping those events from the audit trail.
   - `SandboxedAiderTool`'s default model being a raw Anthropic model name instead of an OpenRouter-routed one, causing silent authentication failures with a false-success exit code.
   - `SandboxedAiderTool` defaulting to `--network none`, which makes it structurally impossible to reach OpenRouter at all — shipped `allow_network=True` as an explicitly logged, documented stopgap.

PR #7 branched *before* any of this work (and before OpenHands was even added to `main`), then independently made its own set of changes. This audit determines what overlaps, what conflicts, what's a genuine regression, and what's worth keeping.

---

## Summary Verdict

**Do not merge PR #7 as-is.** It is already flagged `dirty`/unmergeable by GitHub, and beyond the textual conflicts, merging it would reintroduce two bugs already fixed on `main`. Recommended action: cherry-pick the specific independent fixes that are still valid, and skip everything that regresses a fixed bug or is unwired/untested scaffolding.

| Category | Verdict |
|---|---|
| Full merge | ❌ Reject — conflicts + reintroduces fixed bugs |
| Cherry-pick specific commits | ✅ Recommended — see [Recommendation](#final-recommendation) |

---

## 1. Git-Level Conflict Analysis

A trial merge (`git merge --no-commit --no-ff origin/feature/workflow-fixes` against `main`, later aborted, no files modified) produced real conflicts in:

- `README.md`
- `backend/app/workflow/bootstrap.py`
- `docs/03-RUNTIME-MODULES.md`
- `docs/05-ADAPTERS.md`
- `docs/08-DEPLOYMENT.md`

These are exactly the files both the doc-audit pass and the bug-fix pass touched most heavily on `main`, and where PR #7 made independent, incompatible edits.

---

## 2. Finding-by-Finding Audit

### 2.1 `bootstrap.py` — HealthMonitor probe_map bug: NOT fixed on PR #7

Verified directly via `git show origin/feature/workflow-fixes:backend/app/workflow/bootstrap.py`:

```
90:                probe_map={},  # No real probes during bootstrap — adapters wire later
284:        probe_map={},
```

Both call sites still construct `HealthMonitor`/discovery with an empty probe map. This is the identical bug fixed on `main` (wiring `probe_map[Capability.AI_CODER] = openrouter_provider`). PR #7 does not address it — the branch predates the fix.

**Also checked:** does `bootstrap.py` on PR #7 pass `allow_network=True` anywhere? No — both `SandboxedAiderTool(` construction sites have no such override.

### 2.2 OpenHands adapter deletion — not a deliberate regression, just a stale branch

PR #7's diff shows `backend/app/adapters/openhands.py` deleted (208 lines). Investigated whether this was an intentional removal:

```
git merge-base --is-ancestor 24ca4e0 main            → true
git merge-base --is-ancestor 24ca4e0 <PR7 merge-base> → false
```

Commit `24ca4e0` ("Add OpenHands adapter + fix Aider default model") landed on `main` *after* PR #7's divergence point (`11e82ea`). PR #7 never had this file to delete — its `_create_coding_tool()` has no OpenHands branch at all, and its `tools.yaml` disables OpenHands with a "deferred to post-MVP" comment, consistent with predating the feature rather than removing it deliberately. However, **merging PR #7 as-is would still remove OpenHands from `main`**, since PR #7's version of `bootstrap.py` and `tools.yaml` simply don't know it exists.

### 2.3 `github_vcs.py` — token-injection bug: NOT fixed on PR #7

Zero commits on PR #7 touch this file since the merge-base. The merge-base version already has the bug main fixed:

```python
# PR #7 (unfixed, inherited from merge-base):
authed = parsed._replace(netloc=f"{self._token}@{parsed.hostname}")

# main (fixed):
authed = parsed._replace(netloc=f"x-access-token:{self._token}@{parsed.hostname}")
```

Straight overlap with no contribution from PR #7 — main's fix is still required regardless of merge decision.

### 2.4 `event_id` / `Event.create()` — different architectural approach, with a regression

PR #7 takes a different approach to the same underlying bug class: `Event.create()` is changed to auto-generate `event_id` via `uuid4()` when `None` is passed (verified directly in `backend/app/runtime/events/models.py`, docstring: *"Factory method that auto-fills timestamp and event_id if not provided"*), then strips the now-redundant `event_id=...` argument from all 9 previously-broken call sites.

This is arguably a **better long-term fix** — it closes the entire bug class for any future call site, not just the 9 that were caught. However, verified via `git diff main origin/feature/workflow-fixes -- backend/app/runtime/router/__init__.py`:

```diff
-            correlation_id=self._session_id,
-            event_id=str(uuid.uuid4()),
```

PR #7's refactor drops `correlation_id=self._session_id` as collateral damage at the same call sites (confirmed on `router/__init__.py`; the same pattern appears across the other 8 modules per the diff stats). `correlation_id` is a distinct field used for tracing related events across a session — auto-generating `event_id` does not replace it. This is a real, separate regression, not an equivalent trade-off.

**Verdict:** the auto-generation mechanism is worth adopting, but not verbatim — `correlation_id` wiring from main's fix should be preserved when merging the two approaches.

### 2.5 `sandboxed_aider.py` — active regression, not a fix

Verified directly via `git show origin/feature/workflow-fixes:backend/app/adapters/sandboxed_aider.py`:

```
28:DEFAULT_MODEL = "claude-sonnet-4-20250514"
64:        allow_network: bool = False,
```

This reverts to the exact bug fixed on `main` (raw Anthropic model name, causing `litellm.AuthenticationError: Missing Anthropic API Key` since only `OPENROUTER_API_KEY` ever reaches the sandbox — Aider does not treat this as fatal, so it exits 0 with zero file changes and no error surfaced). Since `bootstrap.py` also never overrides `allow_network`, PR #7's sandbox additionally has zero path to reach OpenRouter even if the model were fixed. The explanatory "KNOWN GAP" docstring documenting this tradeoff (added on `main`) is absent. `test_sandboxed_aider.py` is untouched by PR #7, so nothing in its own test suite would catch this regression.

### 2.6 New subsystems — wiring verified via grep, not assumed

For each new file, usage was checked via `grep` across the entire PR #7 tree (all `workflow/nodes/*.py`, `bootstrap.py`, `app.py`), not inferred from file presence alone:

| File | Lines | Wired into execute/commit path? | Tests? |
|---|---|---|---|
| `backend/app/workflow/checkpoint_middleware.py` | 279 | ✅ Yes — wrapped around every node via `graph.py`'s `add_node_with_checkpoint` | No dedicated test file found |
| `backend/app/adapters/mock_coding_tool.py` | 185 | ✅ Yes — real fallback tier in `_create_coding_tool()` | No dedicated test file found |
| `backend/app/runtime/approval.py` + `backend/app/api/approval.py` + `frontend/components/Approval{Banner,Modal}.tsx` + `frontend/lib/approval.ts` | 421+226+98+207+83 | ❌ No — initialized as a singleton in `app.py`'s lifespan, REST endpoints mounted, but zero references found in any `workflow/nodes/*.py` file |
| `backend/app/runtime/scheduler.py` | 440 | ❌ No — constructed at startup, never invoked from the workflow |
| `backend/app/runtime/learning_engine.py` | 484 | ❌ No — parallel/duplicate to the existing `backend/app/runtime/learning/__init__.py` (`LearningRecorder`), not wired in, not integrated with it |
| `backend/app/runtime/build_timeout.py` | 286 | ❌ No — not referenced in `graph.py` or `bootstrap.py` |
| `backend/app/runtime/stream_router.py` | 183 | ❌ No — no references outside its own file |
| `backend/app/runtime/persistence.py` | 283 | ⚠️ Partial — only `PostgresCheckpointStore` is actually instantiated in `assemble_deps()`; `PostgresSessionStore`/`PostgresAuditStore`/`PostgresLearningStore` classes exist but are never instantiated |

**No test files exist** for any of: `approval`, `scheduler`, `learning_engine`, `build_timeout`, `stream_router`, `mock_coding_tool`, `persistence`, `checkpoint_middleware`. Roughly 1,900+ lines of new subsystem code ship with zero dedicated coverage.

### 2.7 `architect.py` — genuine, independent bug fix

Commit `367f2a3` ("Fix task ID type mismatch in architect node") coerces AI-emitted task IDs to strings before constructing `Task` objects. Verified `Task.id: str` in `backend/app/runtime/models.py` — AI models frequently emit integer IDs in JSON (`"id": 1` instead of `"id": "1"`), which would otherwise cause a type mismatch downstream. This is real and worth keeping regardless of the merge decision.

### 2.8 `execute.py` — regression relative to main

PR #7's diff to `execute.py` mostly adds logging, but also removes the `inspect.signature`-based conditional that allows tools exposing a `repo_url` parameter (i.e. `OpenHandsTool.execute()`) to receive it. Since PR #7 predates OpenHands, this isn't intentional, but it would regress the OpenHands integration path if merged into a codebase that has it.

### 2.9 `aider_tool.py` — genuine, independent improvement

Commit `7efc4d6` ("fix: enable non-sandbox Aider detection via AIDER_PATH env var") adds support for locating the Aider binary via an explicit `AIDER_PATH` environment variable rather than relying solely on PATH resolution. Independent of the sandboxed-tool bugs above, and doesn't conflict with any of main's fixes.

### 2.10 `models.yaml` / `tools.yaml`

- `models.yaml`: changes the default model away from the rate-limited free tier (`nvidia/nemotron-...:free`) toward `claude-3-haiku`-class models. Legitimate, does not conflict with `main`.
- `tools.yaml`: disables OpenHands explicitly (`enabled: false`, "deferred to post-MVP") rather than re-enabling it — consistent with predating the feature, not an active removal decision.

### 2.11 New documentation files

- `docs/REPO_ANALYSIS.md` — the PR author's own audit of the repo, dated the same day as the PR. It self-identifies the task-ID coercion bug and the rate-limiting issue as the problems it set out to fix. It does **not** mention the HealthMonitor probe_map bug, the GitHub token-injection bug, or the missing `event_id` fields — corroborating that PR #7's fix scope is narrower than, and largely non-overlapping with, main's later and more thorough end-to-end fix pass.
- `docs/13-TROUBLESHOOTING.md` — new troubleshooting guide; not evaluated for accuracy against current `main` since its content wasn't the focus of this audit, but worth a follow-up read if any part of PR #7 is merged.

---

## 3. Verification Method

All findings above were independently spot-checked (not taken solely on a sub-agent's word) via direct `git show`/`git diff` commands against `origin/feature/workflow-fixes`, with `main` remaining checked out throughout — no files were modified and no branch checkout of PR #7 occurred. Specifically re-verified firsthand:

- `sandboxed_aider.py`'s `DEFAULT_MODEL` and `allow_network` default on PR #7.
- `bootstrap.py`'s `probe_map={}` occurrences and absence of `allow_network=True`.
- `router/__init__.py`'s diff showing the dropped `correlation_id` alongside the auto-generated `event_id`.
- `events/models.py`'s `Event.create()` docstring and signature confirming auto-generation behavior.

---

## 4. Final Recommendation

**Reject the PR as a whole.** Cherry-pick only the following, onto a fresh branch off current `main`:

### Take
- `367f2a3` — architect.py task-ID type coercion fix (real, independent bug fix)
- `7efc4d6` — AIDER_PATH env var detection for non-sandboxed Aider
- `models.yaml`'s default-model change (away from the rate-limited free tier)
- `checkpoint_middleware.py` + its `graph.py` wiring (add test coverage before or immediately after merging)
- `mock_coding_tool.py` + its `_create_coding_tool()` wiring (add test coverage)

### Do Not Take
- `sandboxed_aider.py`'s version — reverts a fixed bug (raw Anthropic model name + no network override)
- `bootstrap.py`'s version — reverts the HealthMonitor probe_map fix
- `github_vcs.py`'s version — already correctly fixed on `main`; PR #7 contributes nothing here
- `execute.py`'s removal of the OpenHands `repo_url` conditional
- The `Event.create()` auto-generation refactor **verbatim** — adopt the auto-generation safety net if desired, but preserve `correlation_id` wiring from main's fix when doing so
- `approval.py`, `scheduler.py`, `learning_engine.py`, `build_timeout.py`, `stream_router.py`, and the unused portions of `persistence.py` — all disconnected from the actual workflow execution path and completely untested; treat as scaffolding for future work, not production-ready features
