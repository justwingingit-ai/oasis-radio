# Git workflow (solo or team)

Make code review a habit, even alone. Mirrors how teams work.

## The flow

1. `git checkout main && git pull`
2. `git checkout -b feat/short-description`
3. Small, focused commits. Imperative-mood messages.
4. `git push -u origin <branch> && gh pr create --fill`
5. Review your own diff in the GitHub UI as if a stranger wrote it.
6. Merge via UI (squash by default). Delete branch.

## Branch naming

`<type>/<short-description>` where type is one of:
- `feat/` — new feature
- `fix/` — bug fix
- `chore/` — build/deps/tooling
- `docs/` — docs only
- `refactor/` — no behavior change
- `test/` — tests
- `security/` — security-impacting (high-priority review)

## PR description template

```
## What
<1-3 sentences>

## Why
<reason>

## How to test
<steps + expected outcome>

## Follow-ups
<anything not in this PR>
```

## Rules of the road

- Never push to `main`.
- Never force-push to `main`.
- Never `--no-verify` to skip hooks.
- Never commit `.env`.
- Never commit secrets. Leak → rotate FIRST, then clean history.
- One PR = one logical change.
- Big change? Draft PR early.

## When things go wrong

- Accidentally on main? `git branch feat/recover && git reset --hard origin/main && git checkout feat/recover && git push -u origin feat/recover`
- Committed a secret? Rotate it. Then `git filter-repo` or BFG. Treat it as compromised forever.
- Bad merge? Don't force-push shared branches. Open a fix PR.
