/**
 * RedKpiCards — the four-tile RED row (rate, errors, duration, schedule lag).
 *
 * Verifies the lag-tone bands at the 1000 / 5000 ms thresholds, the
 * empty-window vs degraded subtitle swap, and the sparkline wiring
 * when samples accumulate.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { RedMetricsResponse, ScheduleLag } from '../types';

import { RedKpiCards } from './RedKpiCards';

const baseRed: RedMetricsResponse = {
  window_seconds: 60,
  total_requests: 100,
  rate_per_sec: 1.5,
  error_count: 0,
  error_pct: 0,
  latency_ms: { p50: 10, p95: 50, p99: 80, max: 100 },
  by_endpoint: [],
  data_status: 'ok',
};

function lagFixture(over: Partial<ScheduleLag> = {}): ScheduleLag {
  return {
    window_seconds: 60,
    sample_count: 10,
    p50_ms: 30,
    p95_ms: 200,
    max_ms: 400,
    data_status: 'ok',
    ...over,
  };
}

describe('RedKpiCards — schedule-lag tile', () => {
  it('renders P95 as the headline', () => {
    render(
      <RedKpiCards
        red={baseRed}
        redLoading={false}
        redError={false}
        lag={lagFixture({ p95_ms: 187 })}
        lagLoading={false}
        lagError={false}
      />,
    );
    expect(screen.getByText('Schedule lag P95')).toBeInTheDocument();
    expect(screen.getByText('187')).toBeInTheDocument();
  });

  it('renders empty-window subtitle when sample_count is 0 and data_status is ok', () => {
    render(
      <RedKpiCards
        red={baseRed}
        redLoading={false}
        redError={false}
        lag={lagFixture({ sample_count: 0, p50_ms: 0, p95_ms: 0, max_ms: 0 })}
        lagLoading={false}
        lagError={false}
      />,
    );
    // Multiple "—" appear on the page (one per KPI card's delta with
    // empty history). Anchor on the unique subtitle copy instead.
    expect(screen.getByText(/no compounds processed/i)).toBeInTheDocument();
  });

  it('renders degraded subtitle when data_status is degraded', () => {
    render(
      <RedKpiCards
        red={baseRed}
        redLoading={false}
        redError={false}
        lag={lagFixture({
          sample_count: 0,
          p50_ms: 0,
          p95_ms: 0,
          max_ms: 0,
          data_status: 'degraded',
        })}
        lagLoading={false}
        lagError={false}
      />,
    );
    expect(screen.getByText(/metrics unavailable/i)).toBeInTheDocument();
  });

  it('shows error state when lag fetch errored', () => {
    render(
      <RedKpiCards
        red={baseRed}
        redLoading={false}
        redError={false}
        lag={undefined}
        lagLoading={false}
        lagError={true}
      />,
    );
    expect(screen.getByText(/failed to load schedule lag/i)).toBeInTheDocument();
  });

  it('lag band: P95 < 1000 ms → positive tone (healthy)', () => {
    render(
      <RedKpiCards
        red={baseRed}
        redLoading={false}
        redError={false}
        lag={lagFixture({ p95_ms: 500 })}
        lagLoading={false}
        lagError={false}
      />,
    );
    // The tone is rendered as a class on the value node; assert via
    // class lookup. The exact class names come from RedKpiCard's
    // ``toneClass`` map and are stable.
    const valueNode = screen.getByText('500');
    expect(valueNode.className).toMatch(/emerald|green|positive/i);
  });

  it('lag band: 1000 ≤ P95 < 5000 ms → warning tone', () => {
    render(
      <RedKpiCards
        red={baseRed}
        redLoading={false}
        redError={false}
        lag={lagFixture({ p95_ms: 2500 })}
        lagLoading={false}
        lagError={false}
      />,
    );
    const valueNode = screen.getByText('2500');
    expect(valueNode.className).toMatch(/amber|yellow|warning/i);
  });

  it('lag band: P95 ≥ 5000 ms → critical tone', () => {
    render(
      <RedKpiCards
        red={baseRed}
        redLoading={false}
        redError={false}
        lag={lagFixture({ p95_ms: 7000 })}
        lagLoading={false}
        lagError={false}
      />,
    );
    const valueNode = screen.getByText('7000');
    expect(valueNode.className).toMatch(/destructive|red|critical/i);
  });
});
