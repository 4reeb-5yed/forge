# Contributing to Forge

This document defines how work moves through this repository: branching, commit messages, testing expectations, and how to get a change merged.

## Branching Model: Trunk-Based Development

`main` is always deployable. Every commit on `main` should be safe to build, test, and run.

- All work happens on **short-lived branches** cut from the latest `main`.
- Branches are deleted immediately after merge — a merged branch has no reason to keep existing.
- No long-running feature branches. If a change is too large to land in a few days, split it into smaller PRs.

### Branch naming

Prefix the branch name with the type of change, followed by a short, hyphenated description:

```
fix/<short-description>
feat/<short-description>
docs/<short-description>
```

Examples:

- `fix/health-monitor-probe-map`
- `feat/parallel-task-execution`
- `docs/adapter-reference-update`

Use `test/<short-description>` or `refactor/<short-description>` when a change is purely test coverage or a non-behavioral refactor, following the same pattern.

## Commit Messages: Conventional Commits

Every commit message starts with one of these prefixes:

| Prefix | Use for |
|--------|---------|
| `fix:` | Bug fixes |
| `feat:` | New features or capabilities |
| `docs:` | Documentation-only changes |
| `test:` | Adding or updating tests, no behavior change |
| `refactor:` | Code restructuring with no behavior change |

Format:

```
<prefix>: <short summary, imperative mood, no trailing period>

<optional body — what changed and why, wrapped at ~72 chars>
```

Example:

```
fix: wire OpenRouterProvider health_check into HealthMonitor probe_map

HealthMonitor was constructed with an empty probe_map, so the OpenRouter
capability registered at boot had no probe function. HealthMonitor treats
a missing probe as an immediate failure, deregistering the capability
after 3 consecutive cycles regardless of API key validity.
```

Keep the summary line under ~72 characters. If a change spans multiple concerns, split it into multiple commits rather than writing one commit that mixes prefixes.

## Bug Fixes Require a Regression Test

**Every bug fix must include a test that fails on the old code and passes on the new code.**

Before writing the fix:

1. Write a test that reproduces the bug — it should fail against the current (broken) code.
2. Confirm it fails, and capture that failure (this is what proves the test actually exercises the bug, not just green-lights anything).
3. Write the fix.
4. Confirm the same test now passes.

A PR that fixes a bug without a regression test will not be merged — without the test, there's nothing preventing the same bug from silently coming back in a later change.

This applies to `fix:` commits. It does not apply to `docs:`, or to `refactor:` commits that don't change behavior — but if you're not sure whether your change counts as a behavior change, treat it as one and write the test.

## Rebase, Don't Merge

**Every branch must rebase onto current `main` before opening a PR.** Do not merge `main` into your branch.

```bash
git fetch origin
git rebase origin/main
# resolve any conflicts, then:
git push --force-with-lease
```

Why: rebasing keeps history linear and makes it possible to bisect regressions cleanly. Merge commits from `main` into a feature branch create noise and make the actual diff of a PR harder to review. If your branch has drifted far enough from `main` that rebasing is painful, that's a signal the branch has been open too long — see the trunk-based development note above.

If a PR sits open long enough that `main` moves past it again after the initial rebase, rebase again before merge. Don't let a PR merge with a stale base.

## Opening a Pull Request

1. Cut a branch from the latest `main` using the naming convention above.
2. Make your change. Write the regression test first if it's a `fix:`.
3. Run the full test suite locally and confirm it passes (see below).
4. Rebase onto current `main`.
5. Open the PR using the template — fill in every section, including actual test output.
6. Once merged, delete the branch (both local and remote).

## Running Tests Locally

```bash
cd backend
pytest                    # full suite
pytest -x                 # stop on first failure
pytest tests/test_api.py  # a specific module
pytest -k "properties"    # property-based tests only
```

CI runs the same suite on every push and every PR against `main` (see `.github/workflows/ci.yml`). A PR cannot merge with a failing CI run.

## Code Ownership

See [`CODEOWNERS`](./CODEOWNERS) for who reviews changes to which parts of the repository.
