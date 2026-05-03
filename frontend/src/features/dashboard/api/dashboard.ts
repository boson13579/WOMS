/**
 * Dashboard data-access layer.
 *
 * This module is the *only* place that produces dashboard data. Components
 * call `useDashboardData()` (see `useDashboardData.ts` next door); that hook
 * calls `fetchDashboardOverview()` defined here.
 *
 * Phase 1: the body returns deterministic mock data after a 200 ms simulated
 * round-trip so loading skeletons get a chance to flash.
 *
 * Phase 2 swap: replace the body of `fetchDashboardOverview()` with
 *   const res = await fetch('/api/v1/dashboard/overview');
 *   if (!res.ok) throw new Error(...);
 *   return dashboardOverviewSchema.parse(await res.json());
 * No component or hook change required.
 */
import { subHours, subMinutes } from 'date-fns';

import type {
  ActivityItem,
  DashboardOverview,
  LatencySeriesPoint,
  OrdersSnapshot,
  RequestSeriesPoint,
  ResourceMetric,
  SeriesPoint,
  ServiceHealth,
} from '../types';

// ---------------------------------------------------------------------------
// Deterministic PRNG so re-renders don't jitter the chart shapes.
// `mulberry32` requires unsigned 32-bit bitwise math.
// ---------------------------------------------------------------------------
/* eslint-disable no-bitwise */
function mulberry32(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
/* eslint-enable no-bitwise */

function generateSeries(
  seed: number,
  points: number,
  base: number,
  jitter: number,
  now: number,
): SeriesPoint[] {
  const rand = mulberry32(seed);
  const out: SeriesPoint[] = [];
  let v = base;
  for (let i = points - 1; i >= 0; i -= 1) {
    // Mean-reverting random walk: drift toward `base` proportional to distance.
    v += (rand() - 0.5) * jitter * 2 - (v - base) * 0.05;
    v = Math.max(0, Math.min(100, v));
    out.push({ t: subMinutes(now, i).toISOString(), v: Math.round(v * 10) / 10 });
  }
  return out;
}

// ---------------------------------------------------------------------------
// Mock data builders. Each is a pure function of `now` so callers can re-fetch
// to get a fresh snapshot (used by the refresh button).
// ---------------------------------------------------------------------------
function buildServices(): ServiceHealth[] {
  return [
    {
      id: 'api',
      name: 'API',
      status: 'healthy',
      summary: 'FastAPI · v0.1.0',
      details: [
        { label: 'Uptime', value: '99.97%' },
        { label: 'Latency', value: '23 ms' },
        { label: 'Replicas', value: '2/2' },
      ],
    },
    {
      id: 'postgres',
      name: 'PostgreSQL',
      status: 'healthy',
      summary: 'postgres:15-alpine',
      details: [
        { label: 'Connections', value: '12 / 100' },
        { label: 'DB size', value: '124 MB' },
        { label: 'Replication lag', value: '< 1 s' },
      ],
    },
    {
      id: 'redis',
      name: 'Redis',
      status: 'healthy',
      summary: 'redis:7-alpine · cache + broker',
      details: [
        { label: 'Memory', value: '8.2 MB' },
        { label: 'Ops/sec', value: '142' },
        { label: 'Hit rate', value: '94%' },
      ],
    },
    {
      id: 'celery',
      name: 'Celery Worker',
      status: 'warning',
      summary: '2 workers online',
      details: [
        { label: 'Queue depth', value: '17 jobs' },
        { label: 'Failed (1h)', value: '1' },
        { label: 'Last task', value: '4 s ago' },
      ],
    },
  ];
}

function buildResources(now: number): ResourceMetric[] {
  return [
    {
      id: 'cpu',
      name: 'CPU',
      current: '23%',
      currentNumeric: 23,
      averageLabel: 'avg 18%',
      averageNumeric: 18,
      series: generateSeries(101, 60, 22, 8, now),
      accent: 'emerald',
    },
    {
      id: 'memory',
      name: 'Memory',
      current: '41%',
      currentNumeric: 41,
      averageLabel: 'avg 39%',
      averageNumeric: 39,
      series: generateSeries(202, 60, 40, 4, now),
      accent: 'sky',
    },
    {
      id: 'disk',
      name: 'Disk',
      current: '67%',
      currentNumeric: 67,
      averageLabel: 'avg 65%',
      averageNumeric: 65,
      series: generateSeries(303, 60, 65, 1.5, now),
      accent: 'violet',
    },
    {
      id: 'network',
      name: 'Network I/O',
      current: '412 kB/s',
      currentNumeric: 41,
      averageLabel: 'avg 380 kB/s',
      averageNumeric: 38,
      series: generateSeries(404, 60, 35, 18, now),
      accent: 'amber',
    },
  ];
}

function buildRequestRate(now: number): RequestSeriesPoint[] {
  const rand = mulberry32(505);
  return Array.from({ length: 60 }, (_, i) => {
    const ok = 35 + Math.round(rand() * 25);
    const clientErr = Math.round(rand() * 4);
    const serverErr = rand() < 0.08 ? Math.round(rand() * 2 + 1) : 0;
    return {
      t: subMinutes(now, 59 - i).toISOString(),
      v: ok + clientErr + serverErr,
      ok,
      clientErr,
      serverErr,
    };
  });
}

function buildLatency(now: number): LatencySeriesPoint[] {
  const rand = mulberry32(606);
  return Array.from({ length: 60 }, (_, i) => {
    const p50 = 18 + rand() * 8;
    const p95 = p50 + 30 + rand() * 25;
    const p99 = p95 + 30 + rand() * 60;
    return {
      t: subMinutes(now, 59 - i).toISOString(),
      v: p50,
      p50: Math.round(p50),
      p95: Math.round(p95),
      p99: Math.round(p99),
    };
  });
}

function buildOrders(): OrdersSnapshot {
  return {
    newToday: 184,
    pendingSchedule: 23,
    scheduled: 96,
    completed: 65,
  };
}

function buildActivity(now: number): ActivityItem[] {
  return [
    {
      id: 'a1',
      kind: 'update',
      message: 'order.updated · #ORD-2841 quantity 4 → 6',
      actor: 'alice',
      timestamp: subMinutes(now, 3).toISOString(),
    },
    {
      id: 'a2',
      kind: 'create',
      message: 'order.created · #ORD-2842 (sku DR-440)',
      actor: 'bob',
      timestamp: subMinutes(now, 7).toISOString(),
    },
    {
      id: 'a3',
      kind: 'system',
      message: 'scheduling.batch_completed · 12 orders queued',
      actor: 'system',
      timestamp: subMinutes(now, 11).toISOString(),
    },
    {
      id: 'a4',
      kind: 'update',
      message: 'order.updated · #ORD-2839 status pending → scheduled',
      actor: 'alice',
      timestamp: subMinutes(now, 18).toISOString(),
    },
    {
      id: 'a5',
      kind: 'delete',
      message: 'order.deleted · #ORD-2810 (cancelled by manager)',
      actor: 'manager',
      timestamp: subMinutes(now, 25).toISOString(),
    },
    {
      id: 'a6',
      kind: 'create',
      message: 'order.created · #ORD-2841 (sku FL-009)',
      actor: 'alice',
      timestamp: subMinutes(now, 34).toISOString(),
    },
    {
      id: 'a7',
      kind: 'system',
      message: 'celery.worker_started · worker-2',
      actor: 'system',
      timestamp: subHours(now, 1).toISOString(),
    },
    {
      id: 'a8',
      kind: 'update',
      message: 'order.updated · #ORD-2807 quantity 12 → 10',
      actor: 'bob',
      timestamp: subHours(now, 1).toISOString(),
    },
  ];
}

// ---------------------------------------------------------------------------
// Public fetcher — Phase 2 will replace the body with a real fetch().
// ---------------------------------------------------------------------------
const MOCK_DELAY_MS = 200;

export async function fetchDashboardOverview(): Promise<DashboardOverview> {
  await new Promise((resolve) => {
    setTimeout(resolve, MOCK_DELAY_MS);
  });
  const now = Date.now();
  return {
    services: buildServices(),
    resources: buildResources(now),
    requestRate: buildRequestRate(now),
    latency: buildLatency(now),
    orders: buildOrders(),
    activity: buildActivity(now),
  };
}
