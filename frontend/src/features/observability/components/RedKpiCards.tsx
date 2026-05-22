/**
 * RedKpiCards — the four-tile RED + SLO row.
 *
 * Layout: ``grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4`` so the
 * four KPIs reflow to a 2×2 grid at md and a single 1×4 row at xl.
 *
 * SLO colour bands (per plan):
 *   - green when ``success_pct >= slo_target_pct``
 *   - amber when ``error_budget_consumed_pct >= 50`` AND
 *     ``error_budget_pct_remaining >= 10``
 *   - red when ``error_budget_pct_remaining < 10``
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
import type { RedMetricsResponse, SloCompliance } from '../types';

import { RedKpiCard, type KpiTone } from './RedKpiCard';

interface RedKpiCardsProps {
  red: RedMetricsResponse | undefined;
  redLoading: boolean;
  redError: boolean;
  slo: SloCompliance | undefined;
  sloLoading: boolean;
  sloError: boolean;
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

function sloTone(s: SloCompliance): KpiTone {
  if (s.error_budget_pct_remaining < 10) return 'critical';
  if (s.error_budget_consumed_pct >= 50) return 'warning';
  if (s.success_pct >= s.slo_target_pct) return 'positive';
  return 'neutral';
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
  slo,
  sloLoading,
  sloError,
}: RedKpiCardsProps): JSX.Element {
  const rateHistory = useRedHistoryStore((s) => s.series.rate);
  const errorPctHistory = useRedHistoryStore((s) => s.series.errorPct);
  const p95History = useRedHistoryStore((s) => s.series.p95);

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
      <SloCardSlot slo={slo} loading={sloLoading} error={sloError} />
    </div>
  );
}

/**
 * Format the "data: last Xm" hint surfaced below the SLO subtitle when the
 * actual data window is shorter than the requested ``window_hours`` (e.g.
 * the user asked for 24h but the ZSET only retains 1h).
 *
 * Returned in minutes (rounded) to match the operator's mental model;
 * sub-minute slivers round down to 0m which we still show so the empty-
 * data case is visible rather than silently hidden.
 */
function formatDataWindowHint(actualSeconds: number): string {
  const minutes = Math.max(0, Math.round(actualSeconds / 60));
  return `data: last ${minutes}m`;
}

function SloCardSlot({
  slo,
  loading,
  error,
}: {
  slo: SloCompliance | undefined;
  loading: boolean;
  error: boolean;
}): JSX.Element {
  if (loading && !slo) {
    return <Skeleton data-testid="red-kpi-skeleton" className="h-32 w-full" />;
  }
  if (error || !slo) {
    return <ErrorState message="Failed to load SLO." />;
  }
  // Surface "data: last Xm" only when the actual sample window is shorter
  // than requested — when they match (>= requested seconds) the card stays
  // clean rather than restating the obvious. The +slo.window_hours hour
  // → seconds conversion mirrors the backend's ``data_window_seconds_actual``
  // definition so the comparison is exact.
  const requestedSeconds = slo.window_hours * 3600;
  const showDataHint = slo.data_window_seconds_actual < requestedSeconds;
  return (
    <RedKpiCard
      label="SLO compliance"
      value={slo.success_pct.toFixed(2)}
      unit="%"
      subtitle={`Target: ${slo.slo_target_pct.toFixed(1)}% • Budget remaining: ${slo.error_budget_pct_remaining.toFixed(1)}%`}
      tone={sloTone(slo)}
      footnote={showDataHint ? formatDataWindowHint(slo.data_window_seconds_actual) : undefined}
    />
  );
}
