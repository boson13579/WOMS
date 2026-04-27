# Development Guidelines

> **Audience:** every engineer working on Smart Order Management.
> **Scope:** how to add code, how to test it, how to ship it.

This document explains *how* to work day-to-day. The *constraints* — what we
will and won't accept — are defined in **[RULES.md](RULES.md)**.

---

## 0. Project rules — non-negotiable

⚠️ **[docs/RULES.md](RULES.md) is binding.** Every commit, every PR, every
review must comply with it without exception. It codifies the project's
architectural commitments (12-Factor, Bulletproof React, FastAPI Best
Practices, TDD, the exact tech stack, naming conventions, the unified
error envelope). Anything in *this* guidelines document is a *how-to*
elaboration of those rules — never a relaxation.

**If a rule in `RULES.md` conflicts with anything you read elsewhere
(stale comment, blog post, AI suggestion, even a previous commit),
`RULES.md` wins.** Open a PR against `RULES.md` itself if you genuinely
need to change a rule — never silently deviate.

Reviewers: reject any PR that violates `RULES.md` regardless of how minor
the deviation looks. Cite the section number in the review comment.

---

## 1. Layered architecture

Both `frontend/` and `backend/` enforce strict separation of concerns. Never
import "across the grain" (e.g., a model importing from `services`).

### Backend layers (top → bottom)

```
api/         HTTP routers — parse input, call services, shape responses.
services/    Business logic — transactional units of work, audit logging.
repositories/  Pure DB access (SQLAlchemy CRUD). No business rules.
models/      Domain entities (SQLAlchemy mapped classes). Includes Base.
schemas/     Pydantic models — request/response DTOs.
core/        Infrastructure — config, db engine/session, logger, security.
workers/     Celery tasks (long-running, asynchronous).
```

**Rules**

- `core/` may be imported by anyone but imports nothing from the layers above.
- `models/` defines `Base` (entity layer root). Connection management lives
  in `core/db.py` — never put `engine = create_engine(...)` in `models/`.
- `api/` never touches the ORM directly — it goes through `services/`.
- `services/` accept and return `schemas/` (Pydantic), never raw rows.

### Frontend layers (Bulletproof React)

```
src/features/<feature>/
    api/        React Query hooks + fetch wrappers + zod schemas.
    components/ Feature-specific components (LoginForm, OrderTable, ...).
    stores/     Zustand stores for client-only state.
    types/      Domain types shared within the feature.
src/components/ui/    shadcn/ui primitives — generic across features.
src/lib/        Pure helpers (cn, formatters).
src/routes/     react-router-dom route definitions.
```

**Rules**

- Server state → React Query; client state → Zustand. Never store server data
  in Zustand.
- Cross-feature imports go through `src/components/ui` or `src/lib` — never
  `features/orders` importing from `features/auth/components`.

---

## 2. SOP — adding a new feature

### Backend

1. **Schemas first.** Add request/response Pydantic models in
   `backend/app/schemas/<feature>.py`.
2. **Model.** If a new entity is needed, add `backend/app/models/<feature>.py`
   subclassing `Base`. Re-export it in `app/models/__init__.py`.
3. **Migration.** Run `make revision m="add <feature> table"`, review the
   generated diff, commit both the model and the migration in the same PR.
4. **Repository.** Add `backend/app/repositories/<feature>.py` with pure CRUD.
5. **Service.** Add `backend/app/services/<feature>.py` — business logic and
   audit-log calls live here.
6. **Router.** Add `backend/app/api/v1/<feature>.py` and register it in
   `app/api/v1/__init__.py`.
7. **Tests.** See §3 below — TDD applies before steps 4–6.

### Frontend

1. Create `src/features/<feature>/` with `api/`, `components/`, optionally
   `stores/` and `types/`.
2. **Schema first.** Define a `zod` schema in `api/<feature>.ts`. Type-narrow
   responses through `.parse()` so the rest of the codebase has compile-time
   safety.
3. **API hooks.** Wrap fetches with `useQuery` / `useMutation`. Never call
   `fetch` from a component.
4. **Components.** Compose shadcn/ui primitives + feature-specific UI.
5. **Tests.** Co-locate tests as `<file>.test.tsx`.

---

## 3. TDD workflow

Per RULES.md §5, every new endpoint or service follows the **RED → GREEN →
REFACTOR** loop. The reference example is `tests/api/test_health.py`.

### Red

Write the test first. Cover happy path *and* edge cases (validation errors,
optimistic-lock collisions, soft-delete invariants). Run pytest — it must fail.

```python
def test_create_order_rejects_negative_quantity(client):
    res = client.post("/api/v1/orders", json={"sku": "ABC", "quantity": -1})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422
```

### Green

Write the minimum code to make the test pass. No premature abstraction.

### Refactor

Improve structure once tests are green. Re-run pytest after each change.

### Test fixtures

`backend/tests/conftest.py` ships:
- `postgres_container` — session-wide real PostgreSQL via Testcontainers.
- `engine` — schema applied via `Base.metadata.create_all`.
- `db_session` — per-test transaction; rolls back automatically.
- `client` — `TestClient` with `get_db` overridden to use `db_session`.

Use `client` for API tests; use `db_session` directly for repository tests.

> **Why real Postgres and not SQLite?** SQLite silently swallows JSONB,
> server defaults, and Postgres-specific DDL. Per RULES.md, integration tests
> must mirror production.

---

## 4. Naming conventions

| Element | Convention | Example |
|---|---|---|
| Python module | `snake_case` | `order_scheduler.py` |
| Python class | `PascalCase` | `OrderScheduler` |
| Python function/var | `snake_case` | `create_order` |
| Constant | `SCREAMING_SNAKE_CASE` | `JWT_TTL_SECONDS` |
| TS file (component) | `PascalCase.tsx` | `LoginForm.tsx` |
| TS file (non-component) | `camelCase.ts` | `formatDate.ts` |
| TS type/interface | `PascalCase` | `OrderResponse` |
| URL path | `kebab-case` plurals | `/api/v1/orders/{order_id}/items` |
| Audit-log action | dotted lowercase | `order.updated` |
| Alembic revision | descriptive slug | `2026_04_27_1532-abc123_add_orders` |

---

## 5. Unified API error format

Every error response — validation, not-found, internal — must conform to:

```json
{
  "error": {
    "code": 422,
    "message": "Request validation failed.",
    "details": [
      { "loc": ["body", "quantity"], "msg": "Input should be greater than 0", "type": "greater_than" }
    ]
  }
}
```

The handlers live in `backend/app/api/errors.py` and are registered in
`app/main.py`. **Do not bypass them** — never return raw `JSONResponse` with
a different shape from a route handler.

---

## 6. Logging & audit trail

- Use `structlog.get_logger(__name__)` — never the stdlib `logging.getLogger`.
- For audit events (create/update/delete on user-visible resources), call
  `app.core.logger.audit_log(...)` with `actor_id`, `resource_type`,
  `resource_id`, and a `changes` diff dict.
- A correlation ID is automatically attached to every log line in a request
  via `correlation_id_middleware`. Forward it to Celery tasks by passing
  `task.apply_async(headers={"correlation_id": ...})`.

---

## 7. Optimistic locking

`Base` declares `version_id` and registers it via `__mapper_args__`. Every
`UPDATE` becomes `WHERE id = :id AND version_id = :v`. On a conflict,
SQLAlchemy raises `StaleDataError` — services should catch it and translate
to HTTP 409 with `error.code = 409`.

```python
try:
    db.commit()
except StaleDataError as exc:
    raise HTTPException(409, "Order was modified by another user.") from exc
```

---

## 8. Migrations

- Every model change ships with an Alembic revision in the same PR.
- Always review autogenerate output — it can miss `server_default` changes,
  enum value additions, and index renames.
- Never edit a merged migration; create a follow-up instead.
- Test downgrades locally before merging breaking changes.

---

## 9. Git & CI

- Branch naming: `<type>/<short-description>` — `feat/order-crud`,
  `fix/optimistic-lock`, `chore/bump-fastapi`.
- Conventional commit prefixes: `feat`, `fix`, `chore`, `docs`, `refactor`,
  `test`, `ci`.
- Pre-commit hooks must pass before push (`make lint` locally).
- Both CI jobs (backend, frontend) must be green before merge.

---

## 10. Troubleshooting matrix

| Symptom | Likely cause | Fix |
|---|---|---|
| `make up` fails with "POSTGRES_PASSWORD must be set" | `.env` missing | `cp .env.example .env` |
| `pytest` hangs at "Pulling postgres:15-alpine" | Docker daemon down | start Docker Desktop |
| `mypy` fails on a fresh model | Forgot to import in `app/models/__init__.py` | add re-export |
| Frontend HMR stops at random | Bind mount stale on Windows | rebuild: `docker compose up -d --force-recreate frontend` |
| `alembic revision --autogenerate` produces empty diff | Model not registered | import it in `app/models/__init__.py` |
| 500 returns `{"detail": "..."}` (legacy shape) | Route returned raw `JSONResponse` | use `HTTPException` instead |
