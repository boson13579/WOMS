/**
 * RedKpiCards — four KPI tiles (Rate / Error % / P95 / SLO).
 *
 * The interesting test surface is the SLO colour-band logic per the
 * plan:
 *   - green (positive) when ``success_pct >= slo_target_pct``
 *   - amber (warning) when ``budget_consumed >= 50`` AND
 *     ``budget_remaining >= 10``
 *   - red (critical) when ``budget_remaining < 10``
 */
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { useRedHistoryStore } from '../stores/redHistoryStore';
import type { RedMetricsResponse, SloCompliance } from '../types';

import { RedKpiCards } from './RedKpiCards';

function makeRed(overrides: Partial<RedMetricsResponse> = {}): RedMetricsResponse {
  return {
    window_seconds: 60,
    total_requests: 744,
    rate_per_sec: 12.4,
    error_count: 3,
    error_pct: 0.4,
    latency_ms: { p50: 12, p95: 45, p99: 95, max: 320 },
    by_endpoint: [],
    ...overrides,
  };
}

function makeSlo(overrides: Partial<SloCompliance> = {}): SloCompliance {
  return {
    window_hours: 24,
    total_requests: 12_000,
    successful_requests: 11_990,
    success_pct: 99.92,
    slo_target_pct: 99.5,
    error_budget_pct_remaining: 84,
    error_budget_consumed_pct: 16,
    // Default: actual matches the full 24h window so the "data: last Xm"
    // hint is hidden. Individual tests override this when they care.
    data_window_seconds_actual: 24 * 3600,
    ...overrides,
  };
}

describe('RedKpiCards', () => {
  afterEach(() => {
    useRedHistoryStore.getState().reset();
  });

  it('renders the four KPI labels and headline values', () => {
    render(
      <RedKpiCards
        red={makeRed()}
        redLoading={false}
        redError={false}
        slo={makeSlo()}
        sloLoading={false}
        sloError={false}
      />,
    );
    expect(screen.getByText('Rate')).toBeInTheDocument();
    expect(screen.getByText('Error rate')).toBeInTheDocument();
    expect(screen.getByText('P95 latency')).toBeInTheDocument();
    expect(screen.getByText('SLO compliance')).toBeInTheDocument();
    expect(screen.getByText('12.40')).toBeInTheDocument();
    expect(screen.getByText('0.40')).toBeInTheDocument();
    expect(screen.getByText('45')).toBeInTheDocument();
    expect(screen.getByText('99.92')).toBeInTheDocument();
  });

  it('renders the SLO subtitle with target + budget remaining', () => {
    render(
      <RedKpiCards
        red={makeRed()}
        redLoading={false}
        redError={false}
        slo={makeSlo()}
        sloLoading={false}
        sloError={false}
      />,
    );
    expect(screen.getByText(/target: 99.5%/i)).toBeInTheDocument();
    expect(screen.getByText(/budget remaining: 84.0%/i)).toBeInTheDocument();
  });

  it('renders skeletons while red is loading', () => {
    render(
      <RedKpiCards
        red={undefined}
        redLoading
        redError={false}
        slo={undefined}
        sloLoading
        sloError={false}
      />,
    );
    expect(screen.getAllByTestId('red-kpi-skeleton').length).toBeGreaterThanOrEqual(4);
  });

  it('renders an error card when red metrics fetch fails', () => {
    render(
      <RedKpiCards
        red={undefined}
        redLoading={false}
        redError
        slo={undefined}
        sloLoading={false}
        sloError={false}
      />,
    );
    expect(screen.getByText(/failed to load red metrics/i)).toBeInTheDocument();
  });

  it('uses the positive (green) tone when SLO success >= target', () => {
    // success 99.92 >= target 99.5 AND budget_remaining 84 >= 10 →
    // success branch wins → emerald colour on the value.
    render(
      <RedKpiCards
        red={makeRed()}
        redLoading={false}
        redError={false}
        slo={makeSlo({
          success_pct: 99.92,
          slo_target_pct: 99.5,
          error_budget_pct_remaining: 84,
          error_budget_consumed_pct: 16,
        })}
        sloLoading={false}
        sloError={false}
      />,
    );
    const value = screen.getByText('99.92');
    expect(value.className).toContain('text-emerald-600');
  });

  it('uses the warning (amber) tone when budget consumed >= 50%', () => {
    render(
      <RedKpiCards
        red={makeRed()}
        redLoading={false}
        redError={false}
        slo={makeSlo({
          success_pct: 99.0,
          slo_target_pct: 99.5,
          error_budget_consumed_pct: 60,
          error_budget_pct_remaining: 40,
        })}
        sloLoading={false}
        sloError={false}
      />,
    );
    const value = screen.getByText('99.00');
    expect(value.className).toContain('text-amber-600');
  });

  it('uses the critical (red) tone when budget remaining < 10%', () => {
    render(
      <RedKpiCards
        red={makeRed()}
        redLoading={false}
        redError={false}
        slo={makeSlo({
          success_pct: 98.0,
          slo_target_pct: 99.5,
          error_budget_consumed_pct: 95,
          error_budget_pct_remaining: 5,
        })}
        sloLoading={false}
        sloError={false}
      />,
    );
    const value = screen.getByText('98.00');
    expect(value.className).toContain('text-destructive');
  });

  it('renders "data: last Xm" when the actual sample window < requested', () => {
    // Requested 24h, but the ZSET only retains 1h (3600s). Frontend
    // should surface a muted hint so operators know they're looking at
    // a smaller slice than they asked for.
    render(
      <RedKpiCards
        red={makeRed()}
        redLoading={false}
        redError={false}
        slo={makeSlo({ window_hours: 24, data_window_seconds_actual: 3600 })}
        sloLoading={false}
        sloError={false}
      />,
    );
    expect(screen.getByTestId('red-kpi-footnote')).toHaveTextContent('data: last 60m');
  });

  it('does NOT render the "data:" hint when the actual window matches the requested', () => {
    // Requested 1h and got 1h — no truncation, hint suppressed.
    render(
      <RedKpiCards
        red={makeRed()}
        redLoading={false}
        redError={false}
        slo={makeSlo({ window_hours: 1, data_window_seconds_actual: 3600 })}
        sloLoading={false}
        sloError={false}
      />,
    );
    expect(screen.queryByTestId('red-kpi-footnote')).not.toBeInTheDocument();
  });
});
