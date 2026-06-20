# Pre-ship checklist

> Before saying "done" on any feature, fix, or refactor. Any unchecked box = not shipped.

## Auth & authz
- [ ] Every new/modified route has auth (or `// PUBLIC:` comment with reason).
- [ ] Admin-only routes have an extra role check.
- [ ] User-owned data routes check ownership in the service.
- [ ] No secrets in responses, logs, or client bundles.
- [ ] Payment routes: webhook signatures verified; keys env-only.

## Input validation
- [ ] Every new route has a schema (body / query / params).
- [ ] Strings min/max; numbers bounded; IDs format-checked.
- [ ] Enums are fixed sets, not free text.
- [ ] Money is integer cents.
- [ ] Invalid input → 400 structured error, not 500.

## Unhappy paths
- [ ] Empty body → 400.
- [ ] Extra fields → handled predictably.
- [ ] Wrong types → 400.
- [ ] No token → 401.
- [ ] Wrong owner → 403.
- [ ] Missing resource → 404.
- [ ] Duplicate → 409.
- [ ] DB down → clean 5xx, no crash.

## Output
- [ ] No private fields (passwordHash, stripeCustomerId) in responses.
- [ ] No stack traces in prod errors.

## Types
- [ ] No new `any` without a comment.
- [ ] External input types from schemas via `z.infer`.
- [ ] `tsc --noEmit` passes.

## Tests
- [ ] Integration test for new/changed route: happy + auth fail + validation fail.
- [ ] Full suite passes.

## Docs
- [ ] New env vars in `.env.example` with description.
- [ ] New scripts in README.
- [ ] New concepts documented.

## Git & review
- [ ] On a feature branch, not `main`.
- [ ] Small commits, imperative-mood messages.
- [ ] PR description: what / why / how to test / follow-ups.
- [ ] Re-read my own diff.

## Secrets & config
- [ ] No `.env` committed.
- [ ] No hard-coded credentials.
- [ ] App boots with only the vars in `.env.example`.

## Observability
- [ ] Request id + user id on every log line.
- [ ] No stray console.log.
- [ ] No PII or secrets in logs.

## Final gut check
- [ ] Could on-call debug at 3am from logs alone?
- [ ] Can an attacker reach data they shouldn't?
- [ ] Would I be comfortable with another feedback review?

If all three are yes, it ships.
