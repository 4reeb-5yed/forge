## What changed

<!-- Describe the change itself. Be specific — which files, which behavior. -->

## Why

<!-- What problem does this solve, or what capability does it add? Link an issue if one exists. -->

## How it was tested

<!--
Paste ACTUAL command output. A description of testing ("I tested it and it works") is not
sufficient and the PR will be sent back. Show the real pytest output, curl response, or
whatever is relevant to this change.
-->

```
<paste actual command + output here>
```

## Files changed

<!-- List the files touched and, in one line each, what changed in that file. -->

-

## Checklist

- [ ] Tests pass locally (`cd backend && pytest`) — output pasted above
- [ ] No new warnings introduced by this change
- [ ] Branch is rebased onto current `main` (not merged with `main`)
- [ ] If this is a `fix:`, a regression test is included that fails on the old code and passes on the new code
- [ ] Commit messages follow [Conventional Commits](../CONTRIBUTING.md#commit-messages-conventional-commits) format
