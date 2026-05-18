# WOMS Architecture Map

> **Purpose of this document.** A single-page mental model of the project — what
> it is, where the pieces live, and how they connect. Read this first when
> picking up the codebase after a break, when onboarding a teammate, or before
> writing a feature that touches more than one layer. For deep-dives, this doc
> points at the right specialized doc / file.
>
> Last reviewed: 2026-05-18.

---

## 1. What WOMS is

**Wafer Order Management System** — internal tool that ingests customer orders
for wafer production, runs an automated scheduler to slot them onto a finite
daily production capacity, and exposes the resulting plan + status to ops staff
through a web UI.

The two halves of the product:

1. **Order management** — CRUD over orders (customer, quantity, requested
   delivery date), audit trail, role-based access.
2. **Automated scheduling** — when orders change, a Celery worker re-runs an
   EDF (Earliest Deadline First) scheduler over current state; results are
   materialised into a per-day breakdown on each order and pushed back to the
   UI live via WebSocket.

Roles (highest → lowest): **root**, **scheduler**, **order_manager**, **viewer**.

---

## 2. Top-level layout

```
WOMS/
├── backend/          FastAPI + SQLAlchemy + Celery
├── frontend/         React + Vite + TanStack Query + Zustand
├── docs/             you are here
├── docker-compose.yml  full stack (db, redis, backend, worker, frontend)
└── .github/workflows/  CI (pytest, vitest, lint, typecheck)
```

### Other docs in this folder

| File | Read when… |
|---|---|
| `RULES.md` | choosing patterns — short, opinionated team rules |
| `DEVELOPMENT_GUIDELINES.md` | setting up dev env, lint/format/pre-commit |
| `FRONTEND_SPEC.md` | building a new React feature — patterns & structure |
| `HOW_TO_TEST.md` | writing tests (pytest + vitest conventions) |
| `GITHUB_SETUP.md` | configuring CI / GitHub workflows |
| `scheduling.md` | **deep-dive** into the scheduler algorithm & Redis state machine (178 KB; treat as reference) |
| `scheduling-integration.md` | wiring the scheduler into producer endpoints |

---

## 3. Backend — `backend/`

Stack: Python 3.11 · FastAPI · SQLAlchemy 2.0 · Alembic · Celery + Redis ·
PostgreSQL 15 · structlog · Pydantic v2 · PyJWT · bcrypt. Managed by `uv`.

### 3.1 API surface (`backend/app/api/v1/`)

All routes are prefixed `/api/v1`. Auth is via JWT in an httpOnly cookie
(`access_token`); WebSocket also accepts `?token=` query for cases where the
browser can't attach the cookie.

| Router | Endpoints | Roles |
|---|---|---|
| `auth.py` | `POST /auth/login`, `POST /auth/register`, `POST /auth/logout`, `GET /auth/me` | public for login/register, authed for me/logout |
| `users.py` | `GET /users`, `GET /users/{id}`, `PATCH /users/me`, `PATCH /users/{id}`, `DELETE /users/{id}` | list/edit = root only; self-edit = all |
| `system.py` | `GET /system/usernames?ids=…`, health bits | **all authed roles** — used by feature pages to resolve UUIDs without granting root |
| `orders.py` | `POST /orders`, `GET /orders`, `GET /orders/{id}`, `PATCH /orders/{id}`, `DELETE /orders/{id}`, `PATCH /orders/batch-update`, `GET /orders/{id}/audit-log` | read = viewer+; write = order_manager+ (with row-level checks) |
| `schedule.py` | `POST /schedule/trigger`, `POST /schedule/operations`, `DELETE /schedule/operations/{compound_id}`, `GET /schedule/status`, `GET /schedule/result`, `GET /schedule/pending-ops`, `GET /schedule/capacity`, `POST /schedule/rebuild` | read = order_manager+; trigger/rebuild/operations = scheduler+ |
| `websocket.py` | `WS /ws` | authed; single global channel, see §6 |
| `health.py` | `GET /health` | public |

Role gating uses `_READ_ROLES` / `_WRITE_ROLES` dependencies declared at the top
of each router. Adding a new endpoint? Pick the right dependency rather than
re-implementing the check.

### 3.2 Domain models (`backend/app/models/`)

- **`User`** — `id, username, email (unique), password_hash, role, is_active`
- **`Order`** — full schema in `backend/app/schemas/order.py`. Notable columns:
  - `version_id` — optimistic-lock counter; clients must echo it on PATCH and
    we 409 on mismatch.
  - `is_processing_locked` — true while a compound for this order is in
    flight; producers respect it and the UI disables row actions.
  - `is_pinned` / `pinned_production_date` — admin override that locks the
    order to a specific production date.
  - `daily_breakdown` (JSONB) — materialised per-day wafer split written by
    the materializer task (see §3.5). The DB column is the source of truth
    the API serves; Redis is the algorithm cache, **not** the read path.
- **`AuditLog`** — generic table; one row per business action, JSONB diff.

### 3.3 Services (`backend/app/services/`)

Pure business logic. Routers delegate; services orchestrate; repositories do
SQL. Order in which to look when chasing a bug: router → service →
repository → model.

- `auth.py` — login, hash compare, token mint
- `user.py` — user CRUD, role transitions
- `order.py` — order CRUD with optimistic lock, audit log emission, and
  enqueuing the right compound into the scheduler queue on every write
- `scheduling.py` — the algorithm: segment tree + priority queue + EDF
  admission; serialises `SchedulerState` to / from Redis
- `schedule_queue.py` — Redis sorted-set wrapper for the pending-ops queue
  (`shrink` group sorts before `grow`; FIFO within each group via a sequence
  counter)
- `websocket.py` — Redis pub/sub bridge that fans broadcasts out to live
  sockets

### 3.4 Schemas (`backend/app/schemas/`)

Pydantic v2. One file per domain (`order.py`, `user.py`, `schedule.py`,
`auth.py`). Frontend Zod schemas mirror these exactly — see §4.3.

The scheduling schemas are the most intricate; key ones in `schedule.py`:

- `ScheduleCompoundRequest` — what `POST /schedule/operations` accepts. A
  *compound* is N leaf ops (add / remove / pin / unpin) that the worker
  applies atomically with snapshot-rollback on any failure. The frontend
  generates `compound_id` (UUID) and uses it to correlate WebSocket events.
- `ScheduleCompoundFailedDetail` — the failure event payload the worker
  emits over WebSocket; documented as a schema so the frontend has a typed
  contract.

### 3.5 Workers (`backend/app/workers/`)

Celery tasks. Broker + result backend = Redis.

| Task | Fires when | What it does |
|---|---|---|
| `run_scheduling_task` | `POST /schedule/trigger`, after every compound enqueue (if idle), and recursively at the end of itself if more ops are queued | Drains `schedule:pending_ops`, applies each compound atomically inside the in-memory `SchedulerState`, broadcasts `schedule.compound_accepted` / `_failed`, then `schedule.updated`. Hands off DB writes to the materializer. |
| `materialize_schedule_task` | After every accepted compound | Writes `daily_breakdown` JSONB into `orders` for the affected users, emits per-user `schedule.materialized` events. Self-coalescing — multiple accepted compounds in flight collapse into one DB write batch. |
| `advance_day_task` | Celery Beat at 00:00 UTC daily | Rolls the planning horizon forward one day, advances order statuses, triggers a fresh scheduling run. |
| `rebuild_schedule_task` | `POST /schedule/rebuild` | Discards Redis `schedule:state`, walks Postgres to reconstruct it, notifies any orders that got skipped, re-triggers scheduling. |

### 3.6 Redis keys

| Key | Type | Owner | Purpose |
|---|---|---|---|
| `schedule:state` | string (serialised) | scheduling worker | in-memory `SchedulerState` cache; algorithm only |
| `schedule:pending_ops` | sorted set | producers + worker | compound queue (score = group + seq) |
| `schedule:pending_ops:by_compound_id` | hash | producers + worker | secondary index for `DELETE /schedule/operations/{id}` |
| `schedule:pending_ops:seq` | counter | producers | atomic INCR for FIFO ordering |
| `schedule:status` | string (json) | worker | `idle` / `running` / `failed` + metadata for dashboards |
| `schedule:ws:events` | pub/sub channel | worker → fastapi | the bus the websocket service consumes |
| `materialize:notify_pending` | set | worker → materializer | users awaiting a `schedule.materialized` |

### 3.7 Migrations (`backend/alembic/versions/`)

Standard Alembic. Most recent files give you the schema-evolution timeline:
users → orders + audit_logs → pinning fields → `daily_breakdown` JSONB →
email-as-identifier refinements.

---

## 4. Frontend — `frontend/`

Stack: React 18 · TypeScript 5.6 · Vite · Tailwind 3 + shadcn/ui · React
Router 6 · TanStack Query 5 · Zustand 5 · React Hook Form + Zod · sonner
toasts · Recharts. Tooling: pnpm · Vitest · ESLint (Airbnb-ish) · Prettier.

### 4.1 Folder map (`frontend/src/`)

```
src/
├── main.tsx              QueryClientProvider + ThemeProvider mount
├── App.tsx               RouterProvider
├── routes/
│   └── router.tsx        the route table (protected vs public)
├── components/
│   ├── layout/           AppShell, Header, MobileNav, ProtectedRoute
│   ├── ui/               shadcn primitives (button, dialog, table, …)
│   └── ThemeProvider.tsx
├── features/             Bulletproof React — one folder per feature
│   ├── auth/             login/register, authStore (Zustand)
│   ├── users/            admin /users page (root only)
│   ├── dashboard/        landing page; schedule status, capacity, pending ops
│   └── orders/           order list, modal, filters
├── lib/
│   ├── auth.ts           role hooks (see §4.4)
│   └── utils.ts          cn() + small helpers
└── stores/
    └── themeStore.ts     theme (light/dark) Zustand store
```

**Bulletproof boundary**: a feature folder owns its own `api/`, `components/`,
`hooks/`, `stores/`, `types/`. Features should not import each other's
internals — go through `@/lib/*` or `@/components/*` for shared code.

> `useUsernames` now lives in `features/users/api/useUsernames.ts` (canonical)
> and is re-exported by `dashboard/api/useUsernames.ts` for backward compat.
> `apiFetch` was promoted to `@/lib/apiFetch.ts`; `dashboard/api/apiFetch.ts`
> is now a thin re-export.

### 4.2 Routes (`frontend/src/routes/router.tsx`)

```
/             → DashboardPage         (protected)
/orders       → OrdersPage            (protected)
/users        → AdminUsersPage        (protected; root-only inside the page)
/login        → AuthPage              (public)
/register     → AuthPage              (public)
```

The protected subtree is wrapped in `<ProtectedRoute />` which redirects to
`/login` when the auth store has no live session, and then in `<AppShell />`
which provides the header + nav.

### 4.3 API conventions

- **All HTTP through `apiFetch<T>()`** — `frontend/src/features/dashboard/api/apiFetch.ts`.
  Adds default credentials: include, parses the unified error envelope
  (`{ error: { code, message } }`), throws an `ApiError` (with `.status`) on
  non-2xx, and uses an `AbortController` for a 5 s timeout.
- **All responses Zod-validated** before they leave the API layer. Zod
  schemas in each feature's `api/` file mirror the backend Pydantic schema.
- **TanStack Query for everything server-state**. Query keys are co-located
  with the hook: `orderKeys.all`, `orderKeys.list(params)`, etc. Mutations
  call `qc.invalidateQueries({ queryKey: orderKeys.all })` on success.
- **Zustand for client-only state** — auth identity, theme, filter UI state.
- **Forms** — React Hook Form with `zodResolver` on the same Zod schema the
  API layer uses (or a form-specific superset).

### 4.4 Auth hooks (`frontend/src/lib/auth.ts`)

These are the only auth-aware helpers the rest of the app should touch.

```
useCurrentUser()    → { username, role } | null
useCurrentUserId()  → string | null
useCurrentRole()    → 'root' | 'scheduler' | 'order_manager' | 'viewer' | null
useCanWrite()       → true for root / scheduler / order_manager
useCanSchedule()    → true for root / scheduler
```

Backed by `features/auth/stores/authStore.ts`. The store decodes the JWT
payload (sub / role / exp) when `setSession(token)` is called after login
and persists `{ user, expiresAt }` in `localStorage` under
`smart-order.auth`. The access token itself stays in the httpOnly cookie.

### 4.5 Where features call which endpoint

Quick reverse-index so you don't have to grep:

| Endpoint | Frontend caller |
|---|---|
| `POST /auth/login` | `features/auth/api/auth.ts` |
| `GET /auth/me` | `features/auth/api/auth.ts` (session bootstrap) |
| `GET /orders` | `features/orders/api/orders.ts → useOrders` |
| `POST/PATCH/DELETE /orders/...` | `useCreateOrder` / `useUpdateOrder` / `useDeleteOrder` |
| `POST /schedule/trigger` | `features/dashboard/components/ScheduleControlBar.tsx` |
| `POST /schedule/operations` | _frontend does not call this directly_ — backend service layer enqueues compounds on order CRUD |
| `POST /schedule/rebuild` | same component |
| `GET /schedule/status` | `features/dashboard/api/useScheduleStatus.ts` |
| `GET /schedule/capacity` | `features/dashboard/api/useScheduleCapacity.ts` |
| `GET /schedule/pending-ops` | `features/dashboard/api/usePendingOps.ts` |
| `GET /system/usernames` | `features/dashboard/api/useUsernames.ts` (used by OrderTable too) |
| `GET /users` | `features/auth/api/users.ts → useUsers` (root only — gated internally) |
| `WS /ws` | `features/orders/hooks/useScheduleWs.ts` |

---

## 5. Authentication flow

```
Browser                       FastAPI                    Postgres
  │                              │                          │
  │  POST /auth/login            │                          │
  ├─────────────────────────────►│                          │
  │                              │  bcrypt.checkpw(...)     │
  │                              ├─────────────────────────►│
  │                              │◄─────────────────────────┤
  │                              │  mint JWT (sub, role, exp)
  │  Set-Cookie: access_token=…  │                          │
  │◄─────────────────────────────┤                          │
  │  + { token } in body         │                          │
  │                              │                          │
  │  authStore.setSession(token) │  (decode payload for UI) │
  │                              │                          │
  │  GET /orders (cookie sent)   │                          │
  ├─────────────────────────────►│  decode_access_token()   │
  │                              │  → get_current_user()    │
```

The cookie is `HttpOnly + SameSite=Lax + Secure (prod)`; JS can't read it,
which is why we keep the decoded `user` + `expiresAt` separately in the
store for route guards. On logout we call `POST /auth/logout` to clear the
cookie server-side, then drop the store. **TODO**: `queryClient.clear()`
should also fire on logout to drop the previous user's cached data — owned
by the auth team.

---

## 6. WebSocket pipeline

Single global channel: **`/api/v1/ws`**. One connection per browser tab;
events arrive as JSON envelopes:

```json
{ "type": "schedule.compound_accepted", "compound_id": "..." }
{ "type": "schedule.compound_failed", "compound_id": "...", "failed_op_index": 0, "reason": "...", "rolled_back": true, ... }
{ "type": "schedule.updated" }                       // broadcast, no ids
{ "type": "schedule.materialized" }                  // per-user, no ids
{ "type": "schedule.compound_cancelled", "compound_id": "..." }
```

Flow:

1. Producer (FastAPI route or Celery task) calls
   `app.services.websocket.broadcast()` or `notify_user(user_id, …)`.
2. That writes to Redis pub/sub channel `schedule:ws:events`.
3. The FastAPI side runs an async consumer that pulls events off the
   channel and fans them out to in-process sockets via `ConnectionManager`.
4. Frontend `useScheduleWs()` is a passive listener — on any `schedule.*`
   event it invalidates the orders query cache so the table refreshes once
   the worker finishes. It does not track compound IDs or show per-operation
   toasts.

Authentication on WS: same JWT as REST. The browser sends the cookie
automatically; the worker / server-to-server case can also pass `?token=`.

---

## 7. Scheduling at a glance

> Read `docs/scheduling.md` for the full algorithm — this section is just
> orientation.

**Compound** = one atomic business action containing 1..N leaf ops:

- `add` — push an order onto the priority queue
- `remove` — pop an order off
- `pin` — fix to a specific date (`fake_deadline ≤ real deadline`)
- `unpin` — release a pin

A compound has a single `group` (`shrink` if it can only free capacity,
`grow` if it might consume it); shrinks always sort ahead of grows so we
never reject an op for lack of capacity that's about to be freed up.

**Compounds are produced by the backend, never the frontend.** Order CRUD
endpoints (`create_order`, `update_order`, `delete_order`) enqueue the
right `add` / `remove` / `pin` / `unpin` compound inside the service
layer. The frontend just calls the order endpoints — the scheduling
pipeline picks up the work automatically.

**Manual scheduler kick** — the dashboard "Trigger scheduling" button
calls `POST /schedule/trigger`, which kicks `run_scheduling_task` without
modifying the op queue. The endpoint returns a Celery `task_id` that
isn't currently surfaced beyond a confirmation toast.

If a "reschedule just this one order" semantic is ever needed, ask the
backend for a dedicated endpoint (e.g. `POST /orders/{id}/reschedule`)
rather than driving the compound API from the client — the service layer
needs to own the lock / audit / rollback machinery.

**Worker invariants worth knowing:**

- Worker drains the queue greedily within one task body; uses binary
  search to find the largest accept-able prefix when capacity is tight.
- Any failure rolls `SchedulerState` back to a snapshot taken at the start
  of the failed compound, then continues with the next compound.
- DB writes are deferred to `materialize_schedule_task` so the algorithm
  cache (Redis) stays consistent even if Postgres is slow.

---

## 8. RBAC matrix

| Capability | viewer | order_manager | scheduler | root |
|---|:-:|:-:|:-:|:-:|
| Read orders (list / detail / audit-log) | ✓ | ✓ | ✓ | ✓ |
| Read schedule / dashboard data (`/schedule/status`, `/result`, `/pending-ops`, `/capacity`) | | ✓ | ✓ | ✓ |
| Create / edit own orders | | ✓ | ✓ | ✓ |
| Edit any order | | | ✓ | ✓ |
| Trigger schedule (global) | | | ✓ | ✓ |
| Rebuild schedule from DB | | | ✓ | ✓ |
| List all users (`/users`) | | | | ✓ |
| Resolve UUID → username (`/system/usernames`) | ✓ | ✓ | ✓ | ✓ |
| Edit users / change roles | | | | ✓ |

Row-level note for `order_manager`: can only edit / delete orders where
`created_by == self`. Enforced both in the service layer and surfaced in
the UI by `canEditOrder(order)` in OrderTable.

---

## 9. Local dev quick reference

```bash
# Full stack via Docker Compose
docker compose up -d

# Backend only (uv-managed venv, hot reload)
cd backend && uv run uvicorn app.main:app --reload

# Frontend only (Vite dev server on 5173, proxies /api/v1 to backend)
cd frontend && pnpm dev

# Run the Celery worker
cd backend && uv run celery -A app.celery_app worker -l info

# Tests
cd backend  && uv run pytest
cd frontend && pnpm test
```

Lint / typecheck before pushing:

```bash
cd frontend && pnpm lint && pnpm typecheck
cd backend  && uv run ruff check . && uv run mypy app
```

---

## 10. Known active threads / follow-ups

Things that are partially landed or scheduled — useful to know before
duplicating work.

- **Backend `/api/v1/users/assignable`** (not yet built) — would unblock
  non-root users assigning order ownership through `OrderModal`. Today the
  `assigned_to` field is disabled for non-root sessions.
- **`useUsernames` cross-feature import** — currently lives in
  `dashboard/api/`; should move to a shared layer.
- **Order filter array params** — frontend filter UI for `assigned_to` /
  `created_by` is disabled because the backend `list_orders` query only
  binds a single `uuid.UUID`. Re-enable once the schema accepts an array.
- **`queryClient.clear()` on logout** — required to prevent cross-session
  cache leakage of `/users` etc. Lives in the auth feature's scope.
- **Browser WS reconnection** — `useScheduleWs` doesn't auto-reconnect on
  network blip; relies on the next compound trigger to re-open. Fine for
  the current short-lived use, may want to revisit when other features
  start watching the channel.
