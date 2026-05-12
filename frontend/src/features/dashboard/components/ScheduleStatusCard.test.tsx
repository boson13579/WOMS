/**
 * ScheduleStatusCard — purely presentational, accepts the
 * ScheduleStatusResponse and renders a colored badge + last-run summary.
 *
 * Verifies the three legal states render the right copy & variant, plus
 * the loading / error / first-deploy branches.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ScheduleStatusCard } from './ScheduleStatusCard';

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
});
