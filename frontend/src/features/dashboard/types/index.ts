/**
 * Domain types for the dashboard feature.
 *
 * These types describe the *contract* the backend will eventually fulfil
 * (Phase 2). Components depend only on these — never on the mock module —
 * so swapping in a real API requires zero changes here.
 */

export type ServiceStatus = 'healthy' | 'warning' | 'error';

export interface ServiceHealth {
  id: string;
  name: string;
  status: ServiceStatus;
  /** Single-line summary, e.g. "v0.1.0 · 99.9% uptime". */
  summary: string;
  /** 2–3 short metric pairs displayed under the status pill. */
  details: { label: string; value: string }[];
}

export interface SeriesPoint {
  /** ISO timestamp. */
  t: string;
  /** Primary value (0..100 for percentages, raw kB/s for network). */
  v: number;
}

export interface ResourceMetric {
  id: string;
  name: string;
  /** Current value, formatted (e.g. "23%" or "412 kB/s"). */
  current: string;
  /** Numeric value used for trend math (current vs avg). */
  currentNumeric: number;
  /** "Average over window" formatted. */
  averageLabel: string;
  averageNumeric: number;
  /** Last `n` values for the sparkline. */
  series: SeriesPoint[];
  /** Color token applied to the sparkline + accent dot. */
  accent: 'emerald' | 'sky' | 'violet' | 'amber';
}

export type RequestSeriesPoint = SeriesPoint & {
  ok: number;
  clientErr: number;
  serverErr: number;
};

export type LatencySeriesPoint = SeriesPoint & {
  p50: number;
  p95: number;
  p99: number;
};

export interface OrdersSnapshot {
  newToday: number;
  pendingSchedule: number;
  scheduled: number;
  completed: number;
}

export type ActivityKind = 'create' | 'update' | 'delete' | 'system';

export interface ActivityItem {
  id: string;
  kind: ActivityKind;
  /** Plain-text description; component renders kind icon. */
  message: string;
  actor: string;
  /** ISO timestamp. */
  timestamp: string;
}

/**
 * Aggregated dashboard payload returned by `GET /api/v1/dashboard/overview`
 * (Phase 2). Phase 1 the same shape is fulfilled by an in-process mock.
 */
export interface DashboardOverview {
  services: ServiceHealth[];
  resources: ResourceMetric[];
  requestRate: RequestSeriesPoint[];
  latency: LatencySeriesPoint[];
  orders: OrdersSnapshot;
  activity: ActivityItem[];
}

// ---------------------------------------------------------------------------
// Phase 2 — real-API DTOs
// ---------------------------------------------------------------------------
//
// Mirrors the backend Pydantic schemas:
//   * /system/health           → SystemHealthResponse (app/schemas/system.py)
//   * /schedule/status         → ScheduleStatusResponse (app/schemas/schedule.py)
//   * /schedule/capacity       → ScheduleCapacityResponse
//   * /schedule/pending-ops    → list[PendingOpsEntry]
//   * /orders ×4 (per status)  → OrdersSnapshotCounts (derived client-side)
//
// Phase 1 mock types above (ServiceHealth, ResourceMetric, …) will be
// retired in the cleanup step once the new DashboardPage stops importing
// them.

/** One label/value pair under a Service Health card's status pill. */
export interface ServiceHealthDetail {
  label: string;
  value: string;
}

/** Health snapshot of one tracked dependency (api / postgres / redis / celery). */
export interface ServiceHealthEntry {
  id: 'api' | 'postgres' | 'redis' | 'celery';
  name: string;
  status: ServiceStatus;
  summary: string;
  details: ServiceHealthDetail[];
}

/** Response shape of `GET /api/v1/system/health`. */
export interface SystemHealthResponse {
  services: ServiceHealthEntry[];
}

/** Lifecycle state of the scheduler worker. */
export type ScheduleStatusState = 'idle' | 'running' | 'failed';

/** Response shape of `GET /api/v1/schedule/status`. */
export interface ScheduleStatusResponse {
  state: ScheduleStatusState;
  started_at: string | null;
  finished_at: string | null;
  task_id: string | null;
  error: string | null;
  /** Set when there is no Redis status doc (first deploy). */
  message: string | null;
}

/** One day's cumulative remaining wafer capacity (prefix sum). */
export interface CapacityPrefixEntry {
  date: string;
  cumulative_remaining: number;
}

/** Response shape of `GET /api/v1/schedule/capacity`. */
export interface ScheduleCapacityResponse {
  base_date: string;
  daily_capacity: number;
  entries: CapacityPrefixEntry[];
}

/** One leaf op surfaced inside a pending compound row. */
export interface PendingOpsOpView {
  op: 'add' | 'remove' | 'pin' | 'unpin';
  order_id: string;
  order_number: string;
}

/** One queued compound's drain-position snapshot. */
export interface PendingOpsEntry {
  compound_id: string;
  /** 1-indexed; rank=1 is the next compound the worker will pop. */
  rank: number;
  group: 'shrink' | 'grow';
  op_count: number;
  ops: PendingOpsOpView[];
  requested_by: string;
}

/**
 * Orders count per status — derived client-side from 4 parallel
 * `GET /orders?status=X&page=1&page_size=1` calls.
 *
 * Distinct from {@link OrdersSnapshot} (Phase 1 mock) — that one used
 * a "newToday/pendingSchedule" segmentation; this one mirrors the real
 * `OrderStatus` enum verbatim so the dashboard's numbers match what a
 * user sees on the Orders page. ``cancelled`` is intentionally absent:
 * orders carry that status only as a side effect of soft-delete, and
 * `GET /orders` filters soft-deleted rows out by default.
 */
export interface OrdersSnapshotCounts {
  pending: number;
  scheduled: number;
  in_production: number;
  completed: number;
}

export const ORDERS_SNAPSHOT_STATUSES = [
  'pending',
  'scheduled',
  'in_production',
  'completed',
] as const satisfies readonly (keyof OrdersSnapshotCounts)[];

export type OrdersSnapshotStatus = (typeof ORDERS_SNAPSHOT_STATUSES)[number];
