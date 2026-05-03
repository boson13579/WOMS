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
