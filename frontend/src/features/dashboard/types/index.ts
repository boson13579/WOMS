/**
 * Domain types for the dashboard feature.
 *
 * Mirrors the backend Pydantic schemas the dashboard pulls from:
 *   * GET /system/health        → SystemHealthResponse (app/schemas/system.py)
 *   * GET /schedule/status      → ScheduleStatusResponse (app/schemas/schedule.py)
 *   * GET /schedule/capacity    → ScheduleCapacityResponse
 *   * GET /schedule/pending-ops → PendingOpsEntry[]
 *   * GET /orders ×4 per status → OrdersSnapshotCounts (derived client-side)
 *
 * Components import only from here — never from the API modules — so the
 * presentation layer stays decoupled from React Query plumbing.
 */

export type ServiceStatus = 'healthy' | 'warning' | 'error';

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
 * The four mirror the live row count a user sees on the Orders page.
 * ``cancelled`` is absent: cancelled orders are soft-deleted and filtered
 * out of `GET /orders` by default; surfacing the count would mislead.
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
