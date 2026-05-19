/**
 * Domain types + Zod schemas for the Observability feature.
 *
 * Mirrors the backend Pydantic DTOs that back the operator-grade page:
 *   * GET /api/v1/system/red?window_seconds=…  → RedMetricsResponse
 *     (app/schemas/system.py: RedMetricsResponse / LatencyPercentiles / EndpointStat)
 *   * GET /api/v1/system/resources              → SystemResourcesResponse
 *     (app/schemas/system.py: SystemResourcesResponse / DbPoolStats / RedisStats /
 *      CeleryStats / WorkerBreakdown / WorkerStatus)
 *   * GET /api/v1/system/slo?window_hours=…     → SloComplianceResponse
 *     (app/schemas/system.py: SloComplianceResponse)
 *
 * The Zod schemas are the *runtime* contract — every hook parses through
 * them so a wire-shape drift surfaces as ``isError`` instead of corrupting
 * the UI silently. Inferred types are re-exported so presentational
 * components depend on the type layer here, never the API layer.
 */
import { z } from 'zod';

// ---------------------------------------------------------------------------
// RED — rate / errors / duration
// ---------------------------------------------------------------------------

export const latencyPercentilesSchema = z.object({
  p50: z.number().int().nonnegative(),
  p95: z.number().int().nonnegative(),
  p99: z.number().int().nonnegative(),
  max: z.number().int().nonnegative(),
});

export type LatencyPercentiles = z.infer<typeof latencyPercentilesSchema>;

export const endpointStatSchema = z.object({
  /** Synthetic ``"{METHOD} {PATH}"`` label, e.g. ``"GET /api/v1/orders"``. */
  endpoint: z.string(),
  count: z.number().int().nonnegative(),
  error_pct: z.number().min(0).max(100),
  p50_ms: z.number().int().nonnegative(),
  p95_ms: z.number().int().nonnegative(),
  p99_ms: z.number().int().nonnegative(),
});

export type EndpointStat = z.infer<typeof endpointStatSchema>;

export const redMetricsResponseSchema = z.object({
  window_seconds: z.number().int().positive(),
  total_requests: z.number().int().nonnegative(),
  rate_per_sec: z.number().nonnegative(),
  error_count: z.number().int().nonnegative(),
  error_pct: z.number().min(0).max(100),
  latency_ms: latencyPercentilesSchema,
  by_endpoint: z.array(endpointStatSchema),
});

export type RedMetricsResponse = z.infer<typeof redMetricsResponseSchema>;

// ---------------------------------------------------------------------------
// USE — utilization / saturation / errors (resources card)
// ---------------------------------------------------------------------------

export const dbPoolStatsSchema = z.object({
  size: z.number().int().nonnegative(),
  checked_out: z.number().int().nonnegative(),
  overflow: z.number().int(),
  max_overflow: z.number().int().nonnegative(),
  utilization_pct: z.number().min(0).max(100),
});

export type DbPoolStats = z.infer<typeof dbPoolStatsSchema>;

export const redisStatsSchema = z.object({
  used_memory_bytes: z.number().int().nonnegative(),
  used_memory_peak_bytes: z.number().int().nonnegative(),
  connected_clients: z.number().int().nonnegative(),
  ops_per_sec: z.number().int().nonnegative(),
  evicted_keys: z.number().int().nonnegative(),
});

export type RedisStats = z.infer<typeof redisStatsSchema>;

export const workerStatusSchema = z.enum(['active', 'idle']);

export type WorkerStatus = z.infer<typeof workerStatusSchema>;

export const workerBreakdownSchema = z.object({
  hostname: z.string(),
  active_tasks: z.number().int().nonnegative(),
  status: workerStatusSchema,
});

export type WorkerBreakdown = z.infer<typeof workerBreakdownSchema>;

export const celeryStatsSchema = z.object({
  active_tasks: z.number().int().nonnegative(),
  queue_depth: z.number().int().nonnegative(),
  registered_workers: z.number().int().nonnegative(),
  // Round-2 verifier cosmetic note: include ``workers[]`` and ``truncated``
  // so the drilldown UI can iterate without a null-check.
  workers: z.array(workerBreakdownSchema),
  truncated: z.boolean(),
});

export type CeleryStats = z.infer<typeof celeryStatsSchema>;

export const useResourcesSchema = z.object({
  db_pool: dbPoolStatsSchema.nullable(),
  redis: redisStatsSchema.nullable(),
  celery: celeryStatsSchema.nullable(),
});

export type UseResources = z.infer<typeof useResourcesSchema>;

// ---------------------------------------------------------------------------
// SLO compliance
// ---------------------------------------------------------------------------

export const sloComplianceSchema = z.object({
  window_hours: z.number().int().positive(),
  total_requests: z.number().int().nonnegative(),
  successful_requests: z.number().int().nonnegative(),
  success_pct: z.number().min(0).max(100),
  slo_target_pct: z.number().min(0).max(100),
  error_budget_pct_remaining: z.number().min(0).max(100),
  error_budget_consumed_pct: z.number().min(0).max(100),
  /**
   * Actual amount of data (in seconds) backing this response. When the
   * requested ``window_hours`` exceeds the underlying ZSET retention
   * (1 hour at the time of writing), this caps at the available slice
   * so the UI can surface "Showing Xm of data (requested Yh)" hints.
   * 0 means no samples are present at all.
   */
  data_window_seconds_actual: z.number().int().nonnegative(),
});

export type SloCompliance = z.infer<typeof sloComplianceSchema>;

// ---------------------------------------------------------------------------
// UI-only helpers
// ---------------------------------------------------------------------------

/** Allowed RED time-range window pills (seconds). */
export const RED_WINDOW_OPTIONS = [60, 300, 900, 3600] as const;

export type RedWindowSeconds = (typeof RED_WINDOW_OPTIONS)[number];
