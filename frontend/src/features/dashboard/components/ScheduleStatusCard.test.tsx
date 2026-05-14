/**
 * ScheduleStatusCard — purely presentational, accepts the
 * ScheduleStatusResponse and renders a colored badge + last-run summary.
 *
 * Verifies the three legal states render the right copy & variant, plus
 * the loading / error / first-deploy branches.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ScheduleStatusCard, deriveScheduleDisplay } from './ScheduleStatusCard';

describe('ScheduleStatusCard', () => {
  it('renders idle state with success styling and task id', () => {
    render(
      <ScheduleStatusCard
        data={{
          state: 'idle',
          started_at: '2026-05-12T00:13:42+00:00',
          finished_at: '2026-05-12T00:14:01+00:00',
          task_id: 'celery-task-uuid',
          error: null,
          message: null,
        }}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByText(/idle/i)).toBeInTheDocument();
    expect(screen.getByText(/celery-task-uuid/i)).toBeInTheDocument();
  });

  it('renders running state with info styling', () => {
    render(
      <ScheduleStatusCard
        data={{
          state: 'running',
          started_at: '2026-05-12T00:13:42+00:00',
          finished_at: null,
          task_id: 'celery-task-uuid',
          error: null,
          message: null,
        }}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByText(/running/i)).toBeInTheDocument();
  });

  it('renders failed state with destructive styling and surfaces the error string', () => {
    render(
      <ScheduleStatusCard
        data={{
          state: 'failed',
          started_at: '2026-05-12T00:13:42+00:00',
          finished_at: '2026-05-12T00:13:55+00:00',
          task_id: 'celery-task-uuid',
          error: 'capacity_exceeded for order abc',
          message: null,
        }}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
    expect(screen.getByText(/capacity_exceeded for order abc/)).toBeInTheDocument();
  });

  it('renders the first-deploy message when state has no data history', () => {
    render(
      <ScheduleStatusCard
        data={{
          state: 'idle',
          started_at: null,
          finished_at: null,
          task_id: null,
          error: null,
          message: 'No scheduling has been run yet',
        }}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByText(/No scheduling has been run yet/)).toBeInTheDocument();
  });

  it('renders a skeleton while loading', () => {
    render(<ScheduleStatusCard data={undefined} isLoading isError={false} />);
    expect(screen.getByTestId('schedule-status-skeleton')).toBeInTheDocument();
  });

  it('renders an error message on isError', () => {
    render(<ScheduleStatusCard data={undefined} isLoading={false} isError />);
    expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Derived display logic — covers the queue-aware status combinations.
  // The pure ``deriveScheduleDisplay`` function lets us assert each branch
  // without rendering the whole card.
  // -------------------------------------------------------------------------
  describe('deriveScheduleDisplay — queue-aware status', () => {
    const idleAt = (finishedAt: string | null) => ({
      state: 'idle' as const,
      started_at: '2026-05-12T16:00:00+00:00',
      finished_at: finishedAt,
      task_id: 't1',
      error: null,
      message: null,
    });

    it('queue=0 → Idle (raw state passes through)', () => {
      const r = deriveScheduleDisplay(idleAt('2026-05-12T16:00:00+00:00'), 0);
      expect(r.label).toBe('Idle');
      expect(r.variant).toBe('success');
    });

    it('queue>0 + finished within 30s → Working (between-task gap)', () => {
      // 5 seconds ago
      const now = Date.parse('2026-05-12T16:00:05+00:00');
      const r = deriveScheduleDisplay(idleAt('2026-05-12T16:00:00+00:00'), 50, now);
      expect(r.label).toBe('Working');
      expect(r.variant).toBe('info');
    });

    it('queue>0 + finished_at >= 30s ago → Stalled (worker likely dead)', () => {
      const now = Date.parse('2026-05-12T16:00:45+00:00'); // 45s after finish
      const r = deriveScheduleDisplay(idleAt('2026-05-12T16:00:00+00:00'), 50, now);
      expect(r.label).toBe('Stalled');
      expect(r.variant).toBe('warning');
      expect(r.hint).toMatch(/45s/);
    });

    it('queue>0 + no finished_at → Stalled (no signal at all)', () => {
      // finished_at = null means we've never seen a task finish; if there's
      // still queue depth we treat it as stalled rather than silently OK.
      const r = deriveScheduleDisplay(idleAt(null), 50, Date.now());
      expect(r.label).toBe('Stalled');
    });

    it('running always Running regardless of queue depth', () => {
      const r = deriveScheduleDisplay(
        {
          state: 'running',
          started_at: '2026-05-12T16:00:00+00:00',
          finished_at: null,
          task_id: 't1',
          error: null,
          message: null,
        },
        500,
      );
      expect(r.label).toBe('Running');
      expect(r.variant).toBe('info');
    });

    it('failed always Failed', () => {
      const r = deriveScheduleDisplay(
        {
          state: 'failed',
          started_at: '2026-05-12T16:00:00+00:00',
          finished_at: '2026-05-12T16:00:05+00:00',
          task_id: 't1',
          error: 'boom',
          message: null,
        },
        0,
      );
      expect(r.label).toBe('Failed');
      expect(r.variant).toBe('destructive');
    });
  });

  // -------------------------------------------------------------------------
  // Card-level rendering of the queue depth badge + stall hint.
  // -------------------------------------------------------------------------
  it('renders the "queue: N" pill when queueDepth > 0', () => {
    render(
      <ScheduleStatusCard
        data={{
          state: 'running',
          started_at: '2026-05-12T16:00:00+00:00',
          finished_at: null,
          task_id: 't1',
          error: null,
          message: null,
        }}
        queueDepth={42}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByText(/queue:\s*42/i)).toBeInTheDocument();
  });

  it('does NOT render the queue pill when queueDepth=0', () => {
    render(
      <ScheduleStatusCard
        data={{
          state: 'idle',
          started_at: '2026-05-12T16:00:00+00:00',
          finished_at: '2026-05-12T16:00:01+00:00',
          task_id: 't1',
          error: null,
          message: null,
        }}
        queueDepth={0}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.queryByText(/queue:/i)).not.toBeInTheDocument();
  });
});
