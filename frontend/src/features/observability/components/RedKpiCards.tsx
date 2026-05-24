/**
 * RedKpiCards — the four-tile RED row (rate, errors, duration, lag).
 *
 * Layout: ``grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4`` so the
 * four KPIs reflow to a 2×2 grid at md and a single 1×4 row at xl.
 *
 * The fourth slot was historically SLO compliance. SLO was teaching-
 * level demo content (5-min retention, single target, no alerting) with
 * limited operational signal — replaced with schedule pipeline lag,
 * which directly answers "is the worker keeping up". Lag bands:
 *   - green when P95 < 1000 ms (worker on top of the queue)
 *   - amber when 1000 ≤ P95 < 5000 ms (queue starting to back up)
 *   - red when P95 ≥ 5000 ms (operator should investigate)
 *
 * Deltas are derived from the ring buffer: ``current - previous`` of
 * each metric. We don't ask the backend for a delta because the buffer
 * is the cheapest source and matches the operator's mental model of
 * "vs the previous poll", not "vs an arbitrary window N seconds ago".
 */
import { AlertTriangle } from 'lucide-react';

import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

import { useRedHistoryStore } from '../stores/redHistoryStore';
import type { RedMetricsResponse, ScheduleLag } from '../types';

import { RedKpiCard, type KpiTone } from './RedKpiCard';

interface RedKpiCardsProps {
  red: RedMetricsResponse | undefined;
  redLoading: boolean;
  redError: boolean;
  lag: ScheduleLag | undefined;
  lagLoading: boolean;
  lagError: boolean;
}

interface DeltaResult {
  text: string;
  tone: KpiTone;
}

function formatDelta(current: number, previous: number | undefined, unit: string): DeltaResult {
  if (previous === undefined || Number.isNaN(previous)) {
    return { text: '—', tone: 'neutral' };
  }
  const diff = current - previous;
  // Treat sub-unit drift as neutral to avoid noise.
  if (Math.abs(diff) < 1e-2) {
    return { text: 'unchanged', tone: 'neutral' };
  }
  const direction = diff > 0 ? 'up' : 'down';
  return { text: `${direction} ${Math.abs(diff).toFixed(2)}${unit}`, tone: 'neutral' };
}

function lagTone(p95Ms: number): KpiTone {
  if (p95Ms >= 5_000) return 'critical';
  if (p95Ms >= 1_000) return 'warning';
  return 'positive';
}

function ErrorState({ message }: { message: string }): JSX.Element {
  return (
    <Card className="border-destructive/40">
      <CardContent className="flex items-start gap-3 p-5">
        <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
        <p className="text-sm">{message}</p>
      </CardContent>
    </Card>
  );
}

function LoadingGrid(): JSX.Element {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
      {Array.from({ length: 4 }, (_, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <Skeleton key={i} data-testid="red-kpi-skeleton" className="h-32 w-full" />
      ))}
    </div>
  );
}

export function RedKpiCards({
  red,
  redLoading,
  redError,
  lag,
  lagLoading,
  lagError,
}: RedKpiCardsProps): JSX.Element {
  const rateHistory = useRedHistoryStore((s) => s.series.rate);
  const errorPctHistory = useRedHistoryStore((s) => s.series.errorPct);
  const p95History = useRedHistoryStore((s) => s.series.p95);
  const lagP95History = useRedHistoryStore((s) => s.series.lagP95);

  if (redLoading && !red) {
    return <LoadingGrid />;
  }

  if (redError || !red) {
    return <ErrorState message="Failed to load RED metrics." />;
  }

  const rateDelta = formatDelta(red.rate_per_sec, rateHistory.at(-2), '/s');
  const errorDelta = formatDelta(red.error_pct, errorPctHistory.at(-2), '%');
  const p95Delta = formatDelta(red.latency_ms.p95, p95History.at(-2), ' ms');

  const errorTone: KpiTone = red.error_pct > 1 ? 'negative' : 'neutral';

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
      <RedKpiCard
        label="Rate"
        value={red.rate_per_sec.toFixed(2)}
        unit="/s"
        delta={rateDelta.text}
        tone={rateDelta.tone}
        sparklineData={rateHistory}
        sparklineLabel="Request rate over time"
      />
      <RedKpiCard
        label="Error rate"
        value={red.error_pct.toFixed(2)}
        unit="%"
        delta={errorDelta.text}
        tone={errorTone}
        sparklineData={errorPctHistory}
        sparklineLabel="Error rate over time"
      />
      <RedKpiCard
        label="P95 latency"
        value={`${red.latency_ms.p95}`}
        unit="ms"
        delta={p95Delta.text}
        tone={p95Delta.tone}
        sparklineData={p95History}
        sparklineLabel="P95 latency over time"
      />
      <LagCardSlot lag={lag} loading={lagLoading} error={lagError} history={lagP95History} />
    </div>
  );
}

function LagCardSlot({
  lag,
  loading,
  error,
  history,
}: {
  lag: ScheduleLag | undefined;
  loading: boolean;
  error: boolean;
  history: number[];
}): JSX.Element {
  if (loading && !lag) {
    return <Skeleton data-testid="red-kpi-skeleton" className="h-32 w-full" />;
  }
  if (error || !lag) {
    return <ErrorState message="Failed to load schedule lag." />;
  }
  // Empty window — distinguish "no traffic" from "Redis unreachable"
  // via the ``data_status`` flag so the operator doesn't read a
  // metrics-availability outage as a healthy quiet system.
  if (lag.sample_count === 0) {
    return (
      <RedKpiCard
        label="Schedule lag P95"
        value="—"
        unit="ms"
        subtitle={
          lag.data_status === 'degraded'
            ? 'Metrics unavailable (Redis unreachable)'
            : 'No compounds processed in window'
        }
        tone="neutral"
      />
    );
  }
  return (
    <RedKpiCard
      label="Schedule lag P95"
      value={`${lag.p95_ms}`}
      unit="ms"
      subtitle={`P50 ${lag.p50_ms} ms • max ${lag.max_ms} ms (${lag.sample_count} samples)`}
      tone={lagTone(lag.p95_ms)}
      sparklineData={history}
      sparklineLabel="P95 schedule lag over time"
    />
  );
}
