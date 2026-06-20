# CLAUDE.md — Engineering Rules (Company-Grade)

> **Read this file in full before writing or modifying any code in this repo.**
> These rules are binding. If you believe a rule should be broken for a specific case, STOP and ask the user for an explicit override using the syntax at the bottom of this file.

---

## 0. Non-negotiables (the "never let this ship" list)

1. **Every HTTP route is authenticated by default.** Public routes must be explicitly whitelisted in code with a comment explaining why.
2. **Every route that accepts input validates that input server-side.**
3. **Secrets never live in source code or in the repo.** They live in env vars; `.env` is gitignored.
4. **Payment, billing, secrets, and admin routes get an extra role/permission check on top of authentication.**
5. **Error paths are implemented, not TODO'd.**
6. **No committing to `main` directly.**
7. **If I (Claude) am uncertain about a security-relevant decision, I stop and ask.**

---

## 1. Project shape

- Frontend: no large single-file HTML with inline scripts. Component-based (React + Vite + TS as default).
- Backend: thin routes → services → db. Default stack: Node + Express + TS + Zod + Prisma + Postgres.
- Root README.md, .env.example, and a sensible .gitignore are required.

---

## 2. Auth & authz

- Every route is private until proven public. Apply auth middleware globally; mark public routes explicitly.
- Admin-only routes get an extra role check.
- Routes acting on user-owned data check ownership in the service.
- Stripe/payment: webhook signatures verified; secret keys env-only; admin role required for config routes.
- JWTs: short expiry, secret in env, no sensitive data in payload.
- Passwords: bcrypt (cost ≥ 12) or argon2id. Min length 12. Never logged or emailed.

---

## 3. Input validation (Zod)

- Every input route has a schema. Validate before doing anything. Invalid → 400 with structured error.
- Strings: min and max. Numbers: bounds. IDs: format. Enums: fixed set. Money: integer cents.
- Never return raw user objects — explicitly select fields.

---

## 4. Error handling & unhappy paths

- Standard failure modes: 401, 403, 400, 404, 409, 502/503, 500 (sanitized).
- Central error handler. Stack traces never leak to clients in prod.
- Before declaring an endpoint done: try to break it yourself (empty body, extra fields, wrong types, no auth, wrong owner, DB down, 100 reqs/sec).

---

## 5. Documentation

- Root README must contain: what, tech stack, quick start, env vars, scripts, architecture, testing, deployment.
- Service functions get a one-line doc comment. Schemas with non-obvious rules get a why-comment. No commented-out code committed.

---

## 6. TypeScript

- Strict mode. No `any` without an explanation comment. Types from Zod via `z.infer`.

---

## 7. Testing

- New features ship with at least one test. Priority: integration tests for routes (one per failure mode), unit tests for non-trivial services, E2E for critical flows.
- CI runs tests on every PR. Failing tests = no merge.

---

## 8. Git workflow

- No direct commits to `main`. Feature branches + PRs. Imperative-mood commit messages. Review your own diff.
- See `GIT_WORKFLOW.md` for details.

---

## 9. Secrets & env

- `.env.example` committed; `.env` gitignored.
- Validate env at app boot; refuse to start if required vars are missing.
- Never log env vars. Never put them in client-side bundles. Production secrets in a secrets manager.

---

## 10. Logging

- Structured logger (e.g., pino), not console.log in prod.
- Request id on every log line. Never log passwords, tokens, API keys, full PANs, full SSNs.

---

## 11. Dependencies

- Maintained, widely used, no high CVEs. Pin versions. `npm audit` before every release.

---

## 12. AI-assisted coding (rules for Claude)

1. Don't claim done until §4.3 unhappy paths are handled.
2. Don't add a new route without schema + auth in the same change.
3. Don't edit auth/config/payment files without re-reading this file.
4. No commits to `main`.
5. Read existing code before inventing new patterns.
6. Surface security/data-integrity risk explicitly.
7. Prefer small, reviewable PRs (split if >5 files or >300 lines).

---

## 13. Override syntax

If told to skip a rule:
1. Restate the rule and the concrete risk.
2. Ask: *"To confirm: you want me to ship this without <rule>, accepting <risk>. Proceed?"*
3. If confirmed: add a `RULE-OVERRIDE` comment at the site (date, risk, follow-up) and a `TODO.md` entry.

Overrides are never silent.

---

## 14. When in doubt

- Secrets, money, PII? Assume worse. Ask first.
- Feature works but feels wrong? Re-walk §4.3.

End of rules.
